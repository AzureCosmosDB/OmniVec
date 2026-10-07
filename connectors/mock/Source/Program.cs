using OmniVec.Mock;
using Microsoft.Azure.Cosmos;
using Newtonsoft.Json.Linq;
using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
builder.Services.AddSingleton<Metadata>();
builder.Services.AddSingleton<IMetadata>(services => services.GetRequiredService<Metadata>());
builder.Services.AddHostedService<Producer>();
var app = builder.Build();
using (var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(30)))
    await app.Services.GetRequiredService<Metadata>().Ready(deadline.Token);
app.MapGet("/health", () => Results.Json(new { status = "healthy", component = "mock-source", implementation = "dotnet" }));
app.MapPost("/preview", async (HttpRequest request, CancellationToken token) =>
{
    string auth = Environment.GetEnvironmentVariable("OMNIVEC_ADMIN_TOKEN") ?? "";
    if (string.IsNullOrWhiteSpace(auth) || !CryptographicOperations.FixedTimeEquals(
        Encoding.UTF8.GetBytes("Bearer " + auth), Encoding.UTF8.GetBytes(request.Headers.Authorization.ToString())))
        return Results.Unauthorized();
    using var reader = new StreamReader(request.Body);
    JObject body = JObject.Parse(await reader.ReadToEndAsync(token));
    int size = (int)body["document_size_bytes"]!;
    int count = (int)body["count"]!;
    int seed = (int?)body["seed"] ?? 0;
    if (size is < 1 or > 1_048_576 || count is < 1 or > 25 || seed < 0) return Results.BadRequest();
    return Results.Json(new { items = Enumerable.Range(0, count)
        .Select(index => new { id = index.ToString(), content = Wire.Text(index, size, seed) }), mock = true });
});
app.MapGet("/ready", async (Metadata metadata, CancellationToken token) =>
{
    using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
    deadline.CancelAfter(TimeSpan.FromSeconds(4));
    try { await metadata.Ready(deadline.Token); return Results.Ok(new { status = "ready" }); }
    catch (Exception error) { app.Logger.LogError(error, "Mock source readiness failed"); return Results.StatusCode(503); }
});
await app.RunAsync();

public sealed class Producer(IMetadata metadata, ILogger<Producer> logger) : BackgroundService
{
    private readonly HttpClient http = new(new SocketsHttpHandler { MaxConnectionsPerServer = 64 })
        { Timeout = TimeSpan.FromSeconds(120) };
    private readonly string router = (Environment.GetEnvironmentVariable("DOCGROK_URL") ?? "http://docgrok:80").TrimEnd('/');
    private readonly string[] sinks = (Environment.GetEnvironmentVariable("OMNIVEC_MOCK_SINK_URLS")
        ?? "http://omnivec-mock-sink:8080").Split(',').Select(url => url.Trim().TrimEnd('/')).ToArray();
    private readonly string owner = (Environment.GetEnvironmentVariable("HOSTNAME") ?? "source") + ":" + Guid.NewGuid();
    private readonly int shard = int.Parse(Environment.GetEnvironmentVariable("OMNIVEC_MOCK_RUNNER_SHARD") ?? "0");
    private readonly double refresh = Wire.RefreshSeconds();
    private readonly Dictionary<string, (double Expires, JObject? Source)> sourceCache = new();
    private readonly Dictionary<string, (string Generation, Task Task, CancellationTokenSource Stop)> tasks = new();

    protected override async Task ExecuteAsync(CancellationToken token)
    {
        if (shard is < 0 or > 31) throw new InvalidOperationException("Invalid mock runner shard");
        string auth = Environment.GetEnvironmentVariable("OMNIVEC_ADMIN_TOKEN") ?? "";
        if (string.IsNullOrWhiteSpace(auth)) throw new InvalidOperationException("Mock source requires OMNIVEC_ADMIN_TOKEN");
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", auth);
        try
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    var desired = new Dictionary<string, (JObject Pipeline, JObject Source, string Generation)>();
                    foreach (var pipeline in await metadata.List("pipeline", token))
                    {
                        if ((string?)pipeline["status"] != "active" || pipeline["sources"] is not JArray bindings || bindings.Count != 1)
                            continue;
                        string sourceId = (string)bindings[0]["source_id"]!;
                        double now = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
                        if (!sourceCache.TryGetValue(sourceId, out var entry) || entry.Expires <= now)
                        {
                            foreach (var key in sourceCache.Where(item => item.Value.Expires <= now).Select(item => item.Key).ToList())
                                sourceCache.Remove(key);
                            if (sourceCache.Count >= 100) throw new InvalidOperationException("Mock source cache capacity reached");
                            var source = await metadata.Read(sourceId, "source", token);
                            entry = (now + refresh * (0.8 + Random.Shared.NextDouble() * 0.2), source);
                            sourceCache[sourceId] = entry;
                        }
                        if (entry.Source is not JObject src || (string?)src["type"] != "mock"
                            || (bool?)src["enabled"] == false || ((int?)src["config"]?["runner_shard"] ?? 0) != shard)
                            continue;
                        desired[(string)pipeline["id"]!] = (pipeline, src, (string?)pipeline["reset_at"] ?? "initial");
                    }
                    foreach (var id in tasks.Keys.ToList())
                    {
                        var current = tasks[id];
                        if (!desired.TryGetValue(id, out var wanted) || current.Generation != wanted.Generation || current.Task.IsCompleted)
                        {
                            current.Stop.Cancel();
                            try { await current.Task; }
                            catch (OperationCanceledException) when (current.Stop.IsCancellationRequested) { }
                            catch (Exception error) { logger.LogError(error, "Mock source task failed for {Pipeline}", id); }
                            current.Stop.Dispose();
                            tasks.Remove(id);
                        }
                    }
                    foreach (var (id, wanted) in desired)
                    {
                        if (tasks.ContainsKey(id)) continue;
                        var stop = CancellationTokenSource.CreateLinkedTokenSource(token);
                        tasks[id] = (wanted.Generation, Run(wanted.Pipeline, wanted.Source, wanted.Generation, stop.Token), stop);
                    }
                }
                catch (OperationCanceledException) when (token.IsCancellationRequested) { break; }
                catch (Exception error) { logger.LogError(error, "Mock pipeline discovery failed"); }
                await Task.Delay(TimeSpan.FromSeconds(2), token);
            }
        }
        finally
        {
            foreach (var current in tasks.Values) current.Stop.Cancel();
            foreach (var current in tasks.Values)
            {
                try { await current.Task; }
                catch (OperationCanceledException) when (current.Stop.IsCancellationRequested) { }
                catch (Exception error) { logger.LogError(error, "Mock source shutdown failed"); }
                current.Stop.Dispose();
            }
            http.Dispose();
        }
    }

    private async Task Run(JObject pipeline, JObject source, string generation, CancellationToken token)
    {
        string pid = (string)pipeline["id"]!;
        string rid = "mock-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes($"{pid}:{generation}")))
            .ToLowerInvariant()[..32];
        JObject? existing = await metadata.Read(rid, "mock_run", token);
        if ((string?)existing?["status"] is "completed" or "failed") return;
        if ((double?)existing?["lease_until"] > Wire.Epoch() && (string?)existing?["owner"] != owner) return;
        JObject run = existing ?? new JObject { ["id"] = rid, ["doc_type"] = "mock_run",
            ["pipeline_id"] = pid, ["generation"] = generation, ["accepted"] = 0, ["payload_bytes"] = 0,
            ["throttles"] = 0, ["elapsed_seconds"] = 0, ["real_embeddings"] = false, ["persisted_vectors"] = 0 };
        run["owner"] = owner;
        run["lease_until"] = Wire.Epoch() + 30;
        run["status"] = "running";
        try { run = existing is null ? await metadata.Create(run, token) : await metadata.Replace(run, token); }
        catch (CosmosException error) when (error.StatusCode is HttpStatusCode.Conflict or HttpStatusCode.PreconditionFailed) { return; }
        using var gate = new SemaphoreSlim(1);
        double baseline = (double)run["elapsed_seconds"]!;
        var timer = Stopwatch.StartNew();
        using var workersStop = CancellationTokenSource.CreateLinkedTokenSource(token);
        async Task Save(string status, CancellationToken ct)
        {
            run["status"] = status;
            run["elapsed_seconds"] = baseline + timer.Elapsed.TotalSeconds;
            run["lease_until"] = status == "paused" ? 0 : Wire.Epoch() + 30;
            run["throughput_docs_per_second"] = (long)run["accepted"]! / Math.Max((double)run["elapsed_seconds"]!, 0.001);
            if (run["stage_seconds"] is JObject totals)
                run["stage_mean_ms"] = new JObject(totals.Properties().Select(property =>
                    new JProperty(property.Name, (double)property.Value * 1000 / Math.Max((int?)run["batches"] ?? 0, 1))));
            run = await metadata.Replace(run, ct);
        }
        try
        {
            var cfg = (JObject)source["config"]!;
            long total = (long)cfg["document_count"]!;
            int size = (int)cfg["document_size_bytes"]!;
            int batchSize = (int)cfg["batch_size"]!;
            int seed = (int?)cfg["seed"] ?? 0;
            double rate = (double)cfg["documents_per_second"]!;
            string transport = (string?)cfg["embedding_transport"] ?? "json";
            if (total is < 1 or > 10_000_000 || size is < 1 or > 1_048_576
                || batchSize is < 1 or > 2048 || seed < 0 || !double.IsFinite(rate)
                || rate is < 0 or > 10_000_000 || transport is not ("json" or "fp32"))
                throw new InvalidOperationException("Invalid mock source configuration");
            string destId = (string)pipeline["destination_id"]!;
            JObject destination = await metadata.Read(destId, "destination", token)
                ?? throw new InvalidOperationException("Destination not found");
            if ((string?)destination["type"] != "mock" || (bool?)destination["enabled"] == false)
                throw new InvalidOperationException("Enabled mock destination is required");
            JObject sinkCfg = (JObject)destination["config"]!;
            int dimensions = (int)sinkCfg["embedding_dimensions"]!;
            if (dimensions is < 1 or > 65536) throw new InvalidOperationException("Invalid embedding dimensions");
            int receiver = (int?)sinkCfg["receiver_shard"] ?? 0;
            if (receiver < 0 || receiver >= sinks.Length) throw new InvalidOperationException("Receiver shard is not deployed");
            if ((long)batchSize * dimensions > 1_048_576) throw new InvalidOperationException("Vector batch exceeds 4 MiB");
            string modelId = (string)pipeline["docgrok_pipeline"]!;
            var model = await metadata.Read(modelId, "docgrok_model", token);
            string? modelType = (string?)model?["type"];
            if (modelType is not ("mock-embedding" or "openai" or "azure-openai")
                || (string?)model?["model_category"] is not (null or "embedding")
                || (int?)model?["embedding_dim"] != dimensions)
                throw new InvalidOperationException("Matching registered embedding model is required");
            bool realEmbeddings = modelType != "mock-embedding";
            run["real_embeddings"] = realEmbeddings;
            int concurrency = Math.Clamp((int?)pipeline["resource_policy"]?["max_concurrency_per_worker"] ?? 2, 1, 32);
            foreach (var (name, value) in new Dictionary<string, object> {
                ["document_count"] = total, ["document_size_bytes"] = size, ["embedding_dimensions"] = dimensions,
                ["batch_size"] = batchSize, ["concurrency"] = concurrency, ["embedding_transport"] = transport,
                ["runner_shard"] = shard, ["receiver_shard"] = receiver, ["source_id"] = (string)source["id"]!,
                ["destination_id"] = destId, ["model_id"] = modelId, ["source_implementation"] = "dotnet",
                ["model_implementation"] = realEmbeddings ? "external" : "rust",
                ["router_implementation"] = "rust", ["sink_implementation"] = "dotnet" })
                run[name] = JToken.FromObject(value);
            long progress = (long)run["accepted"]!;
            long next = progress;
            double nextAdmission = 0;
            var completed = new Dictionary<long, (int Count, int Bytes)>();
            timer.Restart();
            run["data_started_at_epoch"] ??= Wire.Epoch();
            async Task Worker()
            {
                var ct = workersStop.Token;
                while (true)
                {
                    long begin;
                    int count;
                    double admission;
                    await gate.WaitAsync(ct);
                    try
                    {
                        begin = next;
                        if (begin >= total) return;
                        count = (int)Math.Min(batchSize, total - begin);
                        next += count;
                        admission = nextAdmission;
                        if (rate > 0) nextAdmission = Math.Max(nextAdmission, timer.Elapsed.TotalSeconds) + count / rate;
                    }
                    finally { gate.Release(); }
                    double wait = admission - timer.Elapsed.TotalSeconds;
                    if (wait > 0) await Task.Delay(TimeSpan.FromSeconds(wait), ct);
                    var batchTimer = Stopwatch.StartNew();
                    string[] texts = Enumerable.Range(0, count).Select(index => Wire.Text(begin + index, size, seed)).ToArray();
                    double generated = batchTimer.Elapsed.TotalSeconds;
                    using var modelRequest = new HttpRequestMessage(HttpMethod.Post, router + "/embed/batch") {
                        Content = new StringContent(new JObject { ["model_id"] = modelId, ["texts"] = new JArray(texts) }
                            .ToString(Newtonsoft.Json.Formatting.None), Encoding.UTF8, "application/json") };
                    modelRequest.Headers.Accept.ParseAdd(transport == "fp32" ? Wire.Media : "application/json");
                    using var response = await http.SendAsync(modelRequest, ct);
                    response.EnsureSuccessStatusCode();
                    byte[] modelBody = await response.Content.ReadAsByteArrayAsync(ct);
                    double modeled = batchTimer.Elapsed.TotalSeconds;
                    byte[] payload;
                    if (transport == "fp32")
                    {
                        if (response.Content.Headers.ContentType?.MediaType != Wire.Media)
                            throw new InvalidOperationException("Model did not return FP32 transport");
                        payload = modelBody;
                        Wire.Validate(payload, dimensions, count);
                    }
                    else
                    {
                        var result = JObject.Parse(Encoding.UTF8.GetString(modelBody));
                        payload = Wire.Pack((JArray)result["outputs"]!, count, dimensions);
                    }
                    string digest = Wire.Digest(payload);
                    double packed = batchTimer.Elapsed.TotalSeconds;
                    JObject receipt;
                    var retries = Stopwatch.StartNew();
                    while (true)
                    {
                        using var body = new ByteArrayContent(payload);
                        body.Headers.ContentType = new MediaTypeHeaderValue(Wire.Media);
                        using var acceptance = await http.PostAsync($"{sinks[receiver]}/api/mock-sinks/{Uri.EscapeDataString(destId)}/accept"
                            + $"?run_id={rid}&batch_id={begin}", body, ct);
                        if (acceptance.StatusCode != HttpStatusCode.TooManyRequests)
                        {
                            acceptance.EnsureSuccessStatusCode();
                            receipt = JObject.Parse(await acceptance.Content.ReadAsStringAsync(ct));
                            if ((int)receipt["accepted"]! != count || (int)receipt["payload_bytes"]! != payload.Length
                                || (string?)receipt["sha256"] != digest)
                                throw new InvalidOperationException("Sink receipt validation failed");
                            break;
                        }
                        if (retries.Elapsed > TimeSpan.FromMinutes(30)) throw new TimeoutException("Sink throttle retry deadline exceeded");
                        await gate.WaitAsync(ct);
                        try { run["throttles"] = (int)run["throttles"]! + 1; }
                        finally { gate.Release(); }
                        var retry = acceptance.Headers.RetryAfter?.Delta ?? TimeSpan.FromSeconds(1);
                        await Task.Delay(retry < TimeSpan.FromMilliseconds(10) ? TimeSpan.FromMilliseconds(10) : retry, ct);
                    }
                    await gate.WaitAsync(ct);
                    try
                    {
                        double ended = batchTimer.Elapsed.TotalSeconds;
                        run["batches"] = ((int?)run["batches"] ?? 0) + 1;
                        var stages = (JObject)(run["stage_seconds"] ??= new JObject());
                        var maxima = (JObject)(run["stage_max_ms"] ??= new JObject());
                        foreach (var (name, seconds) in new Dictionary<string, double> {
                            ["generation"] = generated, ["model_http"] = modeled - generated,
                            ["conversion_validation"] = packed - modeled, ["sink_http"] = ended - packed,
                            ["sink_validation"] = (double?)receipt["validation_seconds"] ?? 0,
                            ["sink_metadata"] = (double?)receipt["metadata_seconds"] ?? 0,
                            ["sink_server"] = (double?)receipt["request_seconds"] ?? 0, ["batch"] = ended })
                        {
                            stages[name] = ((double?)stages[name] ?? 0) + seconds;
                            maxima[name] = Math.Max((double?)maxima[name] ?? 0, seconds * 1000);
                        }
                        completed[begin] = (count, payload.Length);
                        while (completed.Remove(progress, out var done))
                        {
                            progress += done.Count;
                            run["accepted"] = progress;
                            run["payload_bytes"] = (long)run["payload_bytes"]! + done.Bytes;
                        }
                    }
                    finally { gate.Release(); }
                }
            }
            async Task Heartbeat()
            {
                while (true)
                {
                    await Task.Delay(TimeSpan.FromSeconds(5), workersStop.Token);
                    await gate.WaitAsync(workersStop.Token);
                    try { await Save("running", workersStop.Token); }
                    finally { gate.Release(); }
                }
            }
            async Task SupervisedWorker()
            {
                try { await Worker(); }
                catch
                {
                    workersStop.Cancel();
                    throw;
                }
            }
            var workers = Enumerable.Range(0, concurrency).Select(_ => SupervisedWorker()).ToArray();
            var all = Task.WhenAll(workers);
            var heartbeat = Heartbeat();
            try
            {
                var first = await Task.WhenAny(all, heartbeat);
                if (first == heartbeat && !workersStop.IsCancellationRequested) await heartbeat;
                await all;
                if ((long)run["accepted"]! != total) throw new InvalidOperationException("Incomplete sink acceptance");
                await gate.WaitAsync(token);
                try
                {
                    run["data_finished_at_epoch"] = Wire.Epoch();
                    await Save("completed", token);
                }
                finally { gate.Release(); }
                logger.LogInformation("Mock pipeline {Pipeline} completed: {Documents} at {Rate} docs/s",
                    pid, total, run["throughput_docs_per_second"]);
            }
            finally
            {
                workersStop.Cancel();
                foreach (var task in workers.Append(heartbeat))
                {
                    try { await task; }
                    catch (OperationCanceledException) when (workersStop.IsCancellationRequested) { }
                    catch (Exception error) { logger.LogError(error, "Mock worker stopped with an error"); }
                }
            }
        }
        catch (CosmosException error) when (error.StatusCode == HttpStatusCode.PreconditionFailed)
        { logger.LogError(error, "Mock run {Pipeline} lost lease ownership; stopping", pid); }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await Save("paused", deadline.Token);
        }
        catch (Exception error)
        {
            logger.LogError(error, "Mock pipeline {Pipeline} failed", pid);
            run["error"] = (error.GetType().Name + ": " + error.Message)[..Math.Min(error.GetType().Name.Length + 2 + error.Message.Length, 1000)];
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await Save("failed", deadline.Token);
        }
    }
}
