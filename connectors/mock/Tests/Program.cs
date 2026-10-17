using OmniVec.Mock;
using Newtonsoft.Json.Linq;
using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.Logging.Abstractions;
using System.Buffers.Binary;
using System.Net;
using System.Text;

Environment.SetEnvironmentVariable("OMNIVEC_ADMIN_TOKEN", "local-test-only");
Environment.SetEnvironmentVariable("OMNIVEC_MOCK_CONFIG_REFRESH_SECONDS", "1");
Environment.SetEnvironmentVariable("OMNIVEC_MOCK_RUNNER_SHARD", "0");
Environment.SetEnvironmentVariable("OMNIVEC_MOCK_RECEIVER_SHARD", "0");
int passed = 0;
void Check(bool value, string description)
{
    if (!value) throw new InvalidOperationException(description);
    passed++;
    Console.WriteLine("PASS " + description);
}
byte[] Payload(int count, int dimensions, float value = 0.5f) => Wire.Pack(
    new JArray(Enumerable.Range(0, count).Select(_ => new JArray(new JArray(Enumerable.Repeat(value, dimensions))))),
    count, dimensions);
var data = Payload(2, 4);
const string generationTimestamp = "2026-10-07T13:11:47.153336";
using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(
    "{\"reset_at\":\"" + generationTimestamp + "\"}")))
{
    var document = new Metadata.MetadataSerializer().FromStream<JObject>(stream);
    Check(document["reset_at"]!.Type == JTokenType.String && (string?)document["reset_at"] == generationTimestamp,
        "Metadata preserves exact reset timestamp bytes for cross-language run identity");
}
Check(Wire.Validate(data, 4, 2) == 2 && data.Length == 48, "Full FP32 body count/dimensions/length");
Check(Wire.Text(42, 4096, 5).Length == 4096, "Source exact document bytes");
foreach (var kind in new[] { "header", "count", "dimensions", "length", "nan" })
{
    byte[] invalid = (byte[])data.Clone();
    if (kind == "header") invalid[0] = 0;
    if (kind == "count") invalid[8] = 0;
    if (kind == "dimensions") invalid[12] = 3;
    if (kind == "length") invalid = invalid[..^1];
    if (kind == "nan") BinaryPrimitives.WriteSingleLittleEndian(invalid.AsSpan(16), float.NaN);
    bool rejected = false;
    try { Wire.Validate(invalid, 4); }
    catch (ArgumentException) { rejected = true; }
    Check(rejected, "Reject invalid FP32 " + kind);
}
var store = new MemoryMetadata();
store.Put(new JObject { ["id"] = "dst", ["doc_type"] = "destination", ["type"] = "mock", ["enabled"] = true,
    ["config"] = new JObject { ["embedding_dimensions"] = 4, ["receiver_shard"] = 0,
        ["accepted_documents_per_second"] = 2, ["burst_documents"] = 2 } });
var receiver = new Receiver(store);
Check(receiver.Authorized("Bearer local-test-only") && !receiver.Authorized("Bearer wrong"), "Destination authentication");
var receipt = await receiver.Accept("dst", "run", "0", data, default);
Check((int)receipt["accepted"]! == 2 && (string?)receipt["sha256"] == Wire.Digest(data), "Destination full-vector receipt");
Check((bool)(await receiver.Accept("dst", "run", "0", data, default))["dedup"]!, "Identical replay dedup");
try { await receiver.Accept("dst", "run", "0", Payload(2, 4, 0.25f), default); throw new Exception("Replay was accepted"); }
catch (SinkError error) { Check(error.Status == 409, "Conflicting replay rejected"); }
try { await receiver.Accept("dst", "run", "2", data, default); throw new Exception("Over-budget request was accepted"); }
catch (SinkError error) { Check(error.Status == 429 && error.RetryAfter is not null, "Token budget returns 429/Retry-After"); }
Check(store.Reads.GetValueOrDefault("destination") == 1, "Metadata cached rather than read for every batch");
await Task.Delay(1100);
await receiver.Accept("dst", "run", "2", data, default);
Check(store.Reads["destination"] == 2, "Expired metadata is refreshed");
store.FailReads = true;
await Task.Delay(1100);
try { await receiver.Accept("dst", "run", "3", data, default); throw new Exception("Stale configuration was accepted"); }
catch (IOException) { Check(true, "Failed refresh never silently falls back to stale metadata"); }
store.FailReads = false;
var cfg = (JObject)store.Documents["destination:dst"]["config"]!;
cfg["accepted_documents_per_second"] = 0;
var unlimited = new Receiver(store);
var builder = WebApplication.CreateBuilder();
builder.Logging.ClearProviders();
builder.WebHost.UseUrls("http://127.0.0.1:0");
var app = builder.Build();
bool invalidModel = false;
bool blockModel = false;
bool failWithBlockedSibling = false;
var siblingStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
int modelCalls = 0;
app.MapPost("/embed/batch", async (HttpRequest request, CancellationToken token) =>
{
    Interlocked.Increment(ref modelCalls);
    using var reader = new StreamReader(request.Body);
    var body = JObject.Parse(await reader.ReadToEndAsync(token));
    var texts = (JArray)body["texts"]!;
    if (texts.Any(text => ((string)text!).Length != 8)) throw new Exception("Wrong source text bytes");
    if (failWithBlockedSibling)
    {
        if ((string?)texts[0] == "00000000")
        {
            await siblingStarted.Task.WaitAsync(token);
            return Results.StatusCode(500);
        }
        siblingStarted.TrySetResult();
        await Task.Delay(30_000, token);
    }
    if (blockModel) await Task.Delay(30_000, token);
    var vectors = Payload(texts.Count, 4);
    if (invalidModel) vectors[0] = 0;
    if (request.Headers.Accept.ToString() == Wire.Media) return Results.Bytes(vectors, Wire.Media);
    return Results.Text(new JObject { ["outputs"] = new JArray(Enumerable.Range(0, texts.Count)
        .Select(_ => new JArray(new JArray(Enumerable.Repeat(0.5f, 4))))) }
        .ToString(Newtonsoft.Json.Formatting.None), "application/json");
});
app.MapPost("/api/mock-sinks/{id}/accept", async (string id, string run_id, string batch_id, HttpRequest request) =>
{
    using var stream = new MemoryStream();
    await request.Body.CopyToAsync(stream);
    if (!unlimited.Authorized(request.Headers.Authorization.ToString())) return Results.Unauthorized();
    var result = await unlimited.Accept(id, run_id, batch_id, stream.ToArray(), default);
    return Results.Text(result.ToString(Newtonsoft.Json.Formatting.None), "application/json");
});
await app.StartAsync();
string endpoint = app.Urls.Single();
Environment.SetEnvironmentVariable("DOCGROK_URL", endpoint);
Environment.SetEnvironmentVariable("OMNIVEC_MOCK_SINK_URLS", endpoint);
store.Put(new JObject { ["id"] = "src", ["doc_type"] = "source", ["type"] = "mock", ["enabled"] = true,
    ["config"] = new JObject { ["document_count"] = 3, ["document_size_bytes"] = 8,
        ["batch_size"] = 2, ["documents_per_second"] = 0, ["seed"] = 0,
        ["embedding_transport"] = "fp32", ["runner_shard"] = 0 } });
store.Put(new JObject { ["id"] = "mdl", ["doc_type"] = "docgrok_model", ["type"] = "mock-embedding", ["embedding_dim"] = 4 });
store.Put(new JObject { ["id"] = "pip", ["doc_type"] = "pipeline", ["status"] = "active",
    ["sources"] = new JArray(new JObject { ["source_id"] = "src" }),
    ["destination_id"] = "dst", ["docgrok_pipeline"] = "mdl",
    ["resource_policy"] = new JObject { ["max_concurrency_per_worker"] = 2 } });
async Task<JObject> WaitRun(string status, int expectedRuns = 1)
{
    using var timeout = new CancellationTokenSource(15_000);
    while (true)
    {
        var runs = await store.List("mock_run", timeout.Token);
        if (runs.Count >= expectedRuns && runs.Last()["status"]?.ToString() == status) return runs.Last();
        await Task.Delay(20, timeout.Token);
    }
}
using (var producer = new Producer(store, NullLogger<Producer>.Instance))
{
    await producer.StartAsync(default);
    var run = await WaitRun("completed");
    Check((int)run["accepted"]! == 3 && (int)run["payload_bytes"]! == 80, "Leased source processes all JSON inputs and full FP32 bodies");
    Check((string?)run["source_implementation"] == "dotnet" && (string?)run["model_implementation"] == "rust",
        "Receipt identifies separate runtime implementations");
    var pipeline = store.Documents["pipeline:pip"];
    pipeline["reset_at"] = "new-generation";
    store.Documents["docgrok_model:mdl"]["type"] = "openai";
    store.Documents["source:src"]["config"]!["embedding_transport"] = "json";
    var reset = await WaitRun("completed", 2);
    Check((string?)reset["generation"] == "new-generation" && (int)reset["accepted"]! == 3, "Reset creates a separate durable run");
    Check((bool)reset["real_embeddings"]! && (string?)reset["model_implementation"] == "external",
        "Registered real model is routed and labelled without claiming persistence");
    await producer.StopAsync(default);
}
invalidModel = true;
store.Documents["pipeline:pip"]["reset_at"] = "invalid-vectors";
using (var producer = new Producer(store, NullLogger<Producer>.Instance))
{
    store.Documents["source:src"]["config"]!["embedding_transport"] = "fp32";
    await producer.StartAsync(default);
    var run = await WaitRun("failed", 3);
    Check((int)run["accepted"]! == 0 && run["error"] is not null, "Malformed model body fails durably without advancing progress");
    await producer.StopAsync(default);
}
invalidModel = false;
failWithBlockedSibling = true;
store.Documents["pipeline:pip"]["reset_at"] = "fail-fast-test";
using (var producer = new Producer(store, NullLogger<Producer>.Instance))
{
    var clock = System.Diagnostics.Stopwatch.StartNew();
    await producer.StartAsync(default);
    var run = await WaitRun("failed", 4);
    Check(clock.Elapsed < TimeSpan.FromSeconds(5) && (int)run["accepted"]! == 0,
        "A failed worker immediately cancels a blocked sibling and saves failure");
    await producer.StopAsync(default);
}
failWithBlockedSibling = false;
blockModel = true;
store.Documents["pipeline:pip"]["reset_at"] = "pause-test";
int before = modelCalls;
using (var producer = new Producer(store, NullLogger<Producer>.Instance))
{
    await producer.StartAsync(default);
    await WaitRun("running", 5);
    while (modelCalls == before) await Task.Delay(10);
    await producer.StopAsync(default);
    var run = await WaitRun("paused", 5);
    Check((int)run["accepted"]! == 0 && (double)run["lease_until"]! == 0, "Cancellation saves resumable progress and releases lease");
    run["lease_until"] = Wire.Epoch() + 30;
    run["owner"] = "another-live-worker";
    store.Put(run);
}
using (var producer = new Producer(store, NullLogger<Producer>.Instance))
{
    before = modelCalls;
    await producer.StartAsync(default);
    await Task.Delay(250);
    Check(modelCalls == before, "A second source cannot steal a live run lease");
    await producer.StopAsync(default);
}
await app.StopAsync();
await app.DisposeAsync();
Console.WriteLine($"MOCK COMPONENTS: {passed} passed");

sealed class MemoryMetadata : IMetadata
{
    public Dictionary<string, JObject> Documents = new();
    public Dictionary<string, int> Reads = new();
    public bool FailReads;
    private readonly object gate = new();
    private int revision;
    public void Put(JObject doc)
    {
        lock (gate)
        {
            var copy = (JObject)doc.DeepClone();
            copy["_etag"] = (++revision).ToString();
            Documents[(string)copy["doc_type"]! + ":" + (string)copy["id"]!] = copy;
        }
    }
    public Task Ready(CancellationToken token) => Task.CompletedTask;
    public Task<JObject?> Read(string id, string kind, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (FailReads) throw new IOException("Injected metadata outage");
            Reads[kind] = Reads.GetValueOrDefault(kind) + 1;
            return Task.FromResult(Documents.TryGetValue(kind + ":" + id, out var doc) ? (JObject)doc.DeepClone() : null);
        }
    }
    public Task<List<JObject>> List(string kind, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        lock (gate) return Task.FromResult(Documents.Values.Where(doc => (string?)doc["doc_type"] == kind)
            .Select(doc => (JObject)doc.DeepClone()).ToList());
    }
    public Task<JObject> Create(JObject doc, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        lock (gate)
        {
            string key = (string)doc["doc_type"]! + ":" + (string)doc["id"]!;
            if (Documents.ContainsKey(key)) throw new CosmosException("Exists", HttpStatusCode.Conflict, 0, "", 0);
            Put(doc);
            return Task.FromResult((JObject)Documents[key].DeepClone());
        }
    }
    public Task<JObject> Replace(JObject doc, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        lock (gate)
        {
            string key = (string)doc["doc_type"]! + ":" + (string)doc["id"]!;
            if (!Documents.TryGetValue(key, out var stored) || (string?)stored["_etag"] != (string?)doc["_etag"])
                throw new CosmosException("Fenced", HttpStatusCode.PreconditionFailed, 0, "", 0);
            Put(doc);
            return Task.FromResult((JObject)Documents[key].DeepClone());
        }
    }
}
