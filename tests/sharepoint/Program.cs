using System.Collections;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;
using Azure.Messaging.ServiceBus;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using Newtonsoft.Json.Linq;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Services;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Models;
using OmniVec.Worker.Services;
using static OmniVec.Worker.Destinations.CosmosDbDestinationWriter;
using IngestionMessage = OmniVec.ChangeFeed.Models.EmbeddingMessage;
using Pipeline = OmniVec.ChangeFeed.Models.Pipeline;
using Destination = OmniVec.ChangeFeed.Models.Destination;
using Source = OmniVec.ChangeFeed.Models.Source;

var tests = new List<(string Name, Func<Task> Run)>();
void Test(string name, Func<Task> run) => tests.Add((name, run));
void Assert(bool condition, string message = "Assertion failed")
{ if (!condition) throw new Exception(message); }
async Task Throws(Func<Task> action)
{
    try { await action(); } catch (Exception) { return; }
    throw new Exception("Expected failure");
}
const BindingFlags Private = BindingFlags.NonPublic | BindingFlags.Instance;
void Set(object target, string field, object value)
    => target.GetType().GetField(field, Private)!.SetValue(target, value);
async Task Invoke(object target, string method, params object[] args)
    => await (Task)target.GetType().GetMethod(method, Private)!.Invoke(target, args)!;
EmbeddingMessage Message() => new()
{
    SourceId = "source", SourceRef = "Policies/a?#.txt", PipelineId = "pipeline",
    SharePointSiteId = "site", SharePointDriveId = "drive", SharePointItemId = "item",
    SharePointRevision = 1, SharePointETag = "v1", SharePointFileName = "a.txt",
    SharePointMaxFileSizeBytes = 50 * 1024 * 1024, DestinationType = "cosmosdb-vector",
    DocgrokPipeline = "mdl-test", StoreContent = true, MetadataFields = [],
};
SharePointReplacement Replacement(long revision, int count, string pipeline = "pipeline")
{
    var msg = Message();
    msg.PipelineId = pipeline;
    var identity = SharePointIdentity.Create(msg);
    return new(identity, revision, msg.SourceId, pipeline, msg.SourceRef,
        Enumerable.Range(0, count).Select(i => new EmbeddingResult(
            $"{identity}-{revision}-chunk-{i}", msg.SourceRef, [0.1f, 0.2f],
            "hash", identity, pipeline, "name", "generation", $"text-{i}",
            SourceId: msg.SourceId, StoreContent: true, MetadataFields: [])).ToList());
}
var cosmos = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
Task<bool> Replace(MemorySyncStore store, SharePointReplacement replacement)
    => cosmos.ReplaceSharePointInStoreAsync(store, "/document_id", "embedding", replacement, default);

Test("Stable, Cosmos-safe identity isolates source/site/drive/item/pipeline and ignores path", () =>
{
    var baseline = SharePointIdentity.Create(Message());
    foreach (var property in new[] { "SourceId", "SharePointSiteId", "SharePointDriveId", "SharePointItemId", "PipelineId" })
    {
        var msg = Message();
        typeof(EmbeddingMessage).GetProperty(property)!.SetValue(msg, "different");
        Assert(SharePointIdentity.Create(msg) != baseline, property);
    }
    var renamed = Message();
    renamed.SourceRef = "Renamed/日本語.txt";
    Assert(SharePointIdentity.Create(renamed) == baseline);
    Assert(baseline.IndexOfAny(['/', '\\', '?', '#']) == -1 && baseline.Length < 100);
    return Task.CompletedTask;
});
Test("Chunk shrink writes new first, removes obsolete and persists mandatory sync metadata", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 4));
    await Replace(store, Replacement(2, 1));
    Assert(store.Chunks.Count == 1);
    var chunk = store.Chunks.Single();
    Assert(chunk["source_id"]!.Value<string>() == "source");
    Assert(chunk["pipeline_id"]!.Value<string>() == "pipeline");
    Assert(chunk["source_ref"]!.Value<string>() == Message().SourceRef);
    Assert(chunk["_omnivec_sync"]!["revision"]!.Value<long>() == 2);
    Assert(chunk["pipeline_name"] is null && chunk["embedding_dims"] is null);
    Assert(store.Operations.LastIndexOf("write:2") < store.Operations.LastIndexOf("delete:2"));
});
Test("Failed new chunk batch preserves old vectors; retry completes and prunes", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 2));
    store.FailNextWrite = true;
    await Throws(() => Replace(store, Replacement(2, 150)));
    Assert(store.Chunks.Count == 2 && store.Chunks.All(c => c["_omnivec_sync"]!["revision"]!.Value<long>() == 1));
    await Replace(store, Replacement(2, 150));
    Assert(store.Chunks.Count == 150 && store.MaxBatchSize <= 100);
});
Test("Failed cleanup is not successful; duplicate retry removes stale vectors", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 3));
    store.FailNextDelete = true;
    await Throws(() => Replace(store, Replacement(2, 1)));
    Assert(store.Chunks.Count == 4);
    await Replace(store, Replacement(2, 1));
    Assert(store.Chunks.Count == 1);
    var operations = store.Operations.Count;
    await Replace(store, Replacement(2, 1));
    Assert(store.Operations.Count == operations, "Completed duplicate should not mutate");
});
Test("Partial staging failure retains the old set until a complete replacement succeeds", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 2));
    store.FailWriteNumber = 3;
    await Throws(() => Replace(store, Replacement(2, 150)));
    Assert(store.Chunks.Count == 101);
    Assert(store.Chunks.Count(c => c["_omnivec_sync"]!["revision"]!.Value<long>() == 1) == 2);
    await Replace(store, Replacement(2, 150));
    Assert(store.Chunks.Count == 150 && store.Chunks.All(c => c["_omnivec_sync"]!["revision"]!.Value<long>() == 2));
});
Test("Delete and empty retain tombstones; delayed upsert cannot resurrect; pipeline isolation", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 2));
    await Replace(store, Replacement(1, 2, "other"));
    await Replace(store, Replacement(3, 0));
    Assert(!await Replace(store, Replacement(2, 5)));
    Assert(store.Chunks.Count == 2 && store.Chunks.All(c => c["pipeline_id"]!.Value<string>() == "other"));
    await Replace(store, Replacement(4, 1));
    await Replace(store, Replacement(5, 0));
    Assert(store.Chunks.Count == 2);
});
Test("Concurrent newer revision fences older writes and cleanup across batches", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 3));
    store.BeforeExecute = async () => await Replace(store, Replacement(3, 2));
    await Throws(() => Replace(store, Replacement(2, 1)));
    Assert(store.Chunks.Count == 2 && store.Chunks.All(c => c["_omnivec_sync"]!["revision"]!.Value<long>() == 3));
    Assert(!await Replace(store, Replacement(2, 1)));
});
Test("Concurrent newer revision during cleanup cannot be deleted by older worker", async () =>
{
    var store = new MemorySyncStore();
    await Replace(store, Replacement(1, 3));
    store.BeforeDelete = async () => await Replace(store, Replacement(3, 2));
    await Throws(() => Replace(store, Replacement(2, 1)));
    Assert(store.Chunks.Count == 2 && store.Chunks.All(c => c["_omnivec_sync"]!["revision"]!.Value<long>() == 3));
});
Test("Invalid partition and content fields fail before changing existing data", async () =>
{
    var store = new MemorySyncStore();
    await Throws(() => cosmos.ReplaceSharePointInStoreAsync(store, "/id", "embedding", Replacement(1, 1), default));
    var replacement = Replacement(1, 1);
    replacement.Chunks[0] = replacement.Chunks[0] with { ContentField = "_omnivec_sync" };
    await Throws(() => Replace(store, replacement));
    Assert(store.Documents.Count == 0);
});
Test("Publisher handles more than two batches and rejects oversize messages", async () =>
{
    var sender = new LocalSender { Capacity = 2 };
    await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()),
        NullLogger<ServiceBusPublisher>.Instance);
    Set(publisher, "_sender", sender);
    var messages = Enumerable.Range(0, 7).Select(i => new IngestionMessage { MessageId = i.ToString() }).ToList();
    await publisher.PublishBatchAsync(messages, default);
    Assert(sender.Sent.Select(m => m.MessageId).SequenceEqual(messages.Select(m => m.MessageId)));
    Assert(sender.Sends == 4);
    sender.Capacity = 0;
    await Throws(() => publisher.PublishBatchAsync(messages, default));
});
Test("Publisher surfaces partial queue failure; deterministic IDs survive replay", async () =>
{
    var sender = new LocalSender { Capacity = 2, FailSend = 2 };
    await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()),
        NullLogger<ServiceBusPublisher>.Instance);
    Set(publisher, "_sender", sender);
    var messages = Enumerable.Range(0, 7).Select(i => new IngestionMessage { MessageId = $"event-{i}" }).ToList();
    await Throws(() => publisher.PublishBatchAsync(messages, default));
    sender.FailSend = 0;
    await publisher.PublishBatchAsync(messages, default);
    Assert(messages.All(m => sender.Sent.Any(s => s.MessageId == m.MessageId)));
});
Test("Rename publication failure retains checkpoint references and replays exact outbox", async () =>
{
    var sender = new LocalSender { Capacity = 2 };
    await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()),
        NullLogger<ServiceBusPublisher>.Instance);
    Set(publisher, "_sender", sender);
    var source = new Source { Id = "source", Type = "sharepoint",
        Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>("""{"site_id":"site","drive_id":"drive"}""")! };
    await using var watcher = new SharePointSourceWatcher(source, new(), null!, new(),
        NullLogger<SharePointSourceWatcher>.Instance, sbPublisher: publisher);
    var pipeline = JsonSerializer.Deserialize<Pipeline>(
        """{"id":"pipeline","sources":[{"source_id":"source"}],"destination_id":"dest"}""")!;
    watcher.UpdatePipelines([pipeline]);
    watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
    var known = (Dictionary<string, string>)typeof(SharePointSourceWatcher).GetField("_knownRefs", Private)!.GetValue(watcher)!;
    known["item"] = "old.txt";
    using var json = JsonDocument.Parse("""{"id":"item","name":"new.txt","file":{},"size":1,"eTag":"v2"}""");
    var task = (Task)typeof(SharePointSourceWatcher).GetMethod("ParseChangesAsync", Private)!
        .Invoke(watcher, [json.RootElement, CancellationToken.None])!;
    await task;
    var changes = task.GetType().GetProperty("Result")!.GetValue(task)!;
    Assert(((IList)changes).Count == 1 && known["item"] == "old.txt");
    await Invoke(watcher, "StagePageAsync", changes, "https://graph.microsoft.com/delta", true, false, CancellationToken.None);
    sender.FailSend = 1;
    await Throws(() => Invoke(watcher, "FlushPendingAsync", CancellationToken.None));
    Assert(known["item"] == "old.txt");
    watcher.UpdatePipelines([]);
    sender.FailSend = 0;
    await Invoke(watcher, "FlushPendingAsync", CancellationToken.None);
    Assert(known["item"] == "new.txt");
    var message = JsonSerializer.Deserialize<EmbeddingMessage>(sender.Sent.Single().Body)!;
    Assert(message.PartitionKeyValue == SharePointIdentity.Create(message));
    Assert(message.SharePointRevision > 0 && message.MessageType == "upsert");
    Assert(sender.AttemptedIds[0] == sender.AttemptedIds[1]);
});
Test("Initial scan, new pipeline replay, reset and expired delta preserve increasing revisions", async () =>
{
    var sender = new LocalSender { Capacity = 10 };
    await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()),
        NullLogger<ServiceBusPublisher>.Instance);
    Set(publisher, "_sender", sender);
    var source = new Source { Id = "source", Type = "sharepoint",
        Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>("""{"site_id":"site","drive_id":"drive"}""")! };
    await using var watcher = new SharePointSourceWatcher(source, new(), null!, new(),
        NullLogger<SharePointSourceWatcher>.Instance, sbPublisher: publisher);
    Pipeline PipelineFor(string id) => JsonSerializer.Deserialize<Pipeline>(
        $$"""{"id":"{{id}}","sources":[{"source_id":"source"}],"destination_id":"dest"}""")!;
    watcher.UpdatePipelines([PipelineFor("p1")]);
    watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
    Set(watcher, "_pipelineStateInitialized", true);
    Set(watcher, "_credential", new LocalCredential());
    var expire = false;
    var urls = new List<string>();
    using var http = new HttpClient(new LocalHttpHandler(request =>
    {
        urls.Add(request.RequestUri!.ToString());
        if (expire) { expire = false; return new(HttpStatusCode.Gone); }
        return new(HttpStatusCode.OK)
        {
            Content = new StringContent("""
                {"value":[{"id":"item","name":"a.txt","file":{},"size":1,"eTag":"v1"}],
                 "@odata.deltaLink":"https://graph.microsoft.com/checkpoint"}
                """)
        };
    }));
    Set(watcher, "_http", http);
    Task Poll() => Invoke(watcher, "PollDeltaAsync", CancellationToken.None);
    await Poll();
    Assert(sender.Sent.Count == 1 && urls[0].Contains("/root/delta"));
    var previous = JsonSerializer.Deserialize<EmbeddingMessage>(sender.Sent.Last().Body)!.SharePointRevision;
    watcher.UpdatePipelines([PipelineFor("p1"), PipelineFor("p2")]);
    await Poll();
    Assert(sender.Sent.Count == 3 && urls.Last().Contains("/root/delta"));
    Assert(JsonSerializer.Deserialize<EmbeddingMessage>(sender.Sent.Last().Body)!.SharePointRevision > previous);
    previous = JsonSerializer.Deserialize<EmbeddingMessage>(sender.Sent.Last().Body)!.SharePointRevision;
    watcher.SkipContentHash = true;
    await Poll();
    Assert(sender.Sent.Count == 5 && urls.Last().Contains("/root/delta"));
    Assert(JsonSerializer.Deserialize<EmbeddingMessage>(sender.Sent.Last().Body)!.SharePointRevision > previous);
    expire = true;
    await Poll();
    Assert(sender.Sent.Count == 7 && urls[^2].EndsWith("/checkpoint") && urls[^1].Contains("/root/delta"));
    Assert(sender.Sent.Select(m => m.MessageId).Distinct().Count() == sender.Sent.Count);
});
Test("Unknown deletion retains stable item identity without guessing display path", async () =>
{
    await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()),
        NullLogger<ServiceBusPublisher>.Instance);
    await using var watcher = new SharePointSourceWatcher(new Source(), new(), null!, new(),
        NullLogger<SharePointSourceWatcher>.Instance, sbPublisher: publisher);
    using var json = JsonDocument.Parse("""{"id":"unknown","deleted":{}}""");
    var task = (Task)typeof(SharePointSourceWatcher).GetMethod("ParseChangesAsync", Private)!
        .Invoke(watcher, [json.RootElement, CancellationToken.None])!;
    await task;
    var changes = (IList)task.GetType().GetProperty("Result")!.GetValue(task)!;
    var change = JsonSerializer.SerializeToElement(changes[0]);
    Assert(changes.Count == 1 && change.GetProperty("Deleted").GetBoolean()
        && change.GetProperty("ItemId").GetString() == "unknown");
});

var graphBytes = Encoding.UTF8.GetBytes("example text");
var graphETag = "v1";
var graph = new SharePointContentClient(new HttpClient(new LocalHttpHandler(request =>
    new HttpResponseMessage(HttpStatusCode.OK)
    {
        Content = request.RequestUri!.AbsolutePath.EndsWith("/content")
            ? new ByteArrayContent(graphBytes)
            : new StringContent(JsonSerializer.Serialize(new { eTag = graphETag }))
    })));
Set(graph, "_credential", new LocalCredential());
var reply = """{"chunks":[{"text":"hello","embedding":[0.1,0.2]}]}""";
var embedHandler = new LocalHttpHandler(_ => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(reply) });
var docgrok = new DocGrokClient(new HttpClient(embedHandler) { BaseAddress = new("http://local.invalid") },
    NullLogger<DocGrokClient>.Instance);
foreach (var invalid in new[] { "{}", """{"chunks":[]}""", """{"chunks":null}""",
    """{"chunks":[{"embedding":[]}]}""", """{"chunks":[{"embedding":[1]}],"dim_skipped":1}""" })
    Test($"Invalid processor response fails: {invalid}", async () =>
    {
        reply = invalid;
        await Throws(() => docgrok.EmbedDataAsync("mdl-test", [1], "file.txt", default));
    });
Test("Explicit skip is accepted but not confused with invalid empty response", async () =>
{
    reply = """{"chunks":[],"skipped":true,"skip_reason":"empty_document"}""";
    Assert((await docgrok.EmbedDataAsync("mdl-test", [1], "file.txt", default)).Count == 0);
});
Test("50 MiB input fits base64 JSON budget; oversized input rejected locally", async () =>
{
    reply = """{"chunks":[{"text":"hello","embedding":[[0.1,0.2]]}]}""";
    var bytes = new byte[50 * 1024 * 1024];
    Array.Fill(bytes, (byte)0xfb);
    Assert((await docgrok.EmbedDataAsync("mdl-test", bytes, "file.txt", default)).Count == 1);
    Assert(Encoding.UTF8.GetByteCount(embedHandler.LastBody!) < 70 * 1024 * 1024);
    Assert(!embedHandler.LastBody!.Contains("\\u002B"), "Base64 must not incur HTML escaping overhead");
    await Throws(() => docgrok.EmbedDataAsync("mdl-test", new byte[bytes.Length + 1], "file.txt", default));
});
Test("Download validates byte cap and detects changed Graph version", async () =>
{
    await Throws(() => graph.DownloadAsync("site", "drive", "item", 2, default));
    await Throws(() => graph.DownloadAsync("site", "drive", "item", 51L * 1024 * 1024, default));
    graphETag = "v2";
    await Throws(() => graph.DownloadAsync("site", "drive", "item", 100, default, "v1"));
    graphETag = "v1";
});
Test("Version change during download is detected before any document processing", async () =>
{
    var checks = 0;
    var client = new SharePointContentClient(new HttpClient(new LocalHttpHandler(request =>
        new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = request.RequestUri!.AbsolutePath.EndsWith("/content")
                ? new ByteArrayContent([1, 2, 3])
                : new StringContent(JsonSerializer.Serialize(new { eTag = ++checks == 1 ? "v1" : "v2" }))
        })));
    Set(client, "_credential", new LocalCredential());
    await Throws(() => client.DownloadAsync("site", "drive", "item", 100, default, "v1"));
    Assert(checks == 2);
});
Test("Worker retries malformed extraction and writer failure; empty and deletes remove chunks", async () =>
{
    var writer = new RecordingWriter();
    using var worker = new EmbeddingWorkerService(Options.Create(new WorkerOptions()), null, docgrok, graph,
        new MetricsReporter(new HttpClient(new LocalHttpHandler(_ => new(HttpStatusCode.OK)))
            { BaseAddress = new("http://local.invalid") }, NullLogger<MetricsReporter>.Instance),
        [writer], NullLogger<EmbeddingWorkerService>.Instance);
    var receiver = new LocalReceiver();
    var msg = Message();
    Task Process() => Invoke(worker, "ProcessSharePointMessageAsync", receiver,
        (msg, ServiceBusModelFactory.ServiceBusReceivedMessage()), CancellationToken.None);
    reply = "{}";
    await Process();
    Assert(receiver.Abandoned == 1 && receiver.Completed == 0 && writer.Replacements.Count == 0);
    reply = """{"chunks":[{"text":"hello","embedding":[0.1,0.2]}]}""";
    writer.Fail = true;
    await Process();
    Assert(receiver.Abandoned == 2 && receiver.Completed == 0);
    writer.Fail = false;
    await Process();
    Assert(receiver.Completed == 1 && writer.Replacements.Last().Chunks.Count == 1);
    graphBytes = [];
    await Process();
    Assert(receiver.Completed == 2 && writer.Replacements.Last().Chunks.Count == 0);
    msg.MessageType = "delete";
    await Process();
    Assert(receiver.Completed == 3 && writer.Replacements.Last().Chunks.Count == 0);
    msg.SharePointRevision = 0;
    await Process();
    Assert(receiver.Abandoned == 3, "Legacy messages must fail closed");
    graphBytes = Encoding.UTF8.GetBytes("example text");
});

var failed = 0;
foreach (var (name, run) in tests)
{
    try { await run(); Console.WriteLine($"PASS {name}"); }
    catch (Exception ex) { failed++; Console.WriteLine($"FAIL {name}: {ex}"); }
}
Console.WriteLine($"RESULT: {tests.Count - failed} passed, {failed} failed (local doubles; no Azure writes)");
var connectorFailures = await ConnectorReliabilityTests.RunAsync();
var cosmosSourceFailures = await CosmosSourceContentTests.RunAsync();
var blobPollingFailures = await BlobPollingTests.RunAsync();
return failed + connectorFailures + cosmosSourceFailures + blobPollingFailures == 0 ? 0 : 1;

sealed class MemorySyncStore : ISharePointSyncStore
{
    public Dictionary<string, JObject> Documents { get; } = new();
    private readonly Dictionary<string, string> _etags = new();
    private int _etag;
    public List<JObject> Chunks => Documents.Values.Where(d => d["_omnivec_sync"]!["kind"]!.Value<string>() == "chunk").ToList();
    public List<string> Operations { get; } = [];
    public bool FailNextWrite { get; set; }
    public int FailWriteNumber { get; set; }
    private int _writeCalls;
    public bool FailNextDelete { get; set; }
    public Func<Task>? BeforeExecute { get; set; }
    public Func<Task>? BeforeDelete { get; set; }
    public int MaxBatchSize { get; private set; }
    public Task<SharePointSyncState?> ReadStateAsync(string id, CancellationToken ct)
        => Task.FromResult(Documents.TryGetValue(id, out var value) ? new SharePointSyncState((JObject)value.DeepClone(), _etags[id]) : null);
    public Task<string?> TryClaimAsync(JObject manifest, string? etag, CancellationToken ct)
    {
        var id = manifest["id"]!.Value<string>()!;
        if (_etags.GetValueOrDefault(id) != etag) return Task.FromResult<string?>(null);
        return Task.FromResult<string?>(Save(manifest));
    }
    private string Save(JObject manifest)
    {
        var id = manifest["id"]!.Value<string>()!;
        Documents[id] = (JObject)manifest.DeepClone();
        return _etags[id] = (++_etag).ToString();
    }
    public async Task<string> ExecuteAsync(JObject manifest, string etag, IReadOnlyList<JObject> writes,
        IReadOnlyList<string> deletes, CancellationToken ct)
    {
        if (BeforeExecute is { } callback) { BeforeExecute = null; await callback(); }
        if (deletes.Count > 0 && BeforeDelete is { } deletion) { BeforeDelete = null; await deletion(); }
        var id = manifest["id"]!.Value<string>()!;
        if (_etags[id] != etag) throw new InvalidOperationException("412: fenced");
        if (writes.Count > 0 && ++_writeCalls == FailWriteNumber)
            throw new InvalidOperationException("503: partial staging failed");
        if (writes.Count > 0 && FailNextWrite) { FailNextWrite = false; throw new InvalidOperationException("503: write failed"); }
        if (deletes.Count > 0 && FailNextDelete) { FailNextDelete = false; throw new InvalidOperationException("503: delete failed"); }
        MaxBatchSize = Math.Max(MaxBatchSize, 1 + writes.Count + deletes.Count);
        var revision = manifest["_omnivec_sync"]!["revision"]!.Value<long>();
        foreach (var doc in writes) Documents[doc["id"]!.Value<string>()!] = (JObject)doc.DeepClone();
        if (writes.Count > 0) Operations.Add($"write:{revision}");
        foreach (var delete in deletes) Documents.Remove(delete);
        if (deletes.Count > 0) Operations.Add($"delete:{revision}");
        return Save(manifest);
    }
    public async IAsyncEnumerable<string> ReadChunkIdsAsync(SharePointReplacement replacement,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
    {
        await Task.CompletedTask;
        foreach (var doc in Chunks.Where(d => d["_omnivec_sync"]!["identity"]!.Value<string>() == replacement.Identity
            && d["pipeline_id"]!.Value<string>() == replacement.PipelineId && d["source_id"]!.Value<string>() == replacement.SourceId))
            yield return doc["id"]!.Value<string>()!;
    }
}
sealed class LocalSender : ServiceBusSender
{
    public override ValueTask DisposeAsync() => ValueTask.CompletedTask;
    public int Capacity { get; set; } = 2;
    public int Sends { get; private set; }
    public int FailSend { get; set; }
    public List<ServiceBusMessage> Sent { get; } = [];
    public List<string> AttemptedIds { get; } = [];
    private readonly Dictionary<ServiceBusMessageBatch, IList<ServiceBusMessage>> _batches = new();
    public override ValueTask<ServiceBusMessageBatch> CreateMessageBatchAsync(CancellationToken ct = default)
    {
        IList<ServiceBusMessage> messages = new List<ServiceBusMessage>();
        var batch = ServiceBusModelFactory.ServiceBusMessageBatch(1024, messages,
            tryAddCallback: _ => messages.Count < Capacity);
        _batches[batch] = messages;
        return ValueTask.FromResult(batch);
    }
    public override Task SendMessagesAsync(ServiceBusMessageBatch batch, CancellationToken ct = default)
    {
        Sends++;
        AttemptedIds.Add(string.Join(",", _batches[batch].Select(m => m.MessageId)));
        if (Sends == FailSend) throw new InvalidOperationException("Simulated queue failure");
        Sent.AddRange(_batches[batch]);
        return Task.CompletedTask;
    }
}
sealed class LocalCredential : DefaultAzureCredential
{
    public override ValueTask<AccessToken> GetTokenAsync(TokenRequestContext context, CancellationToken ct = default)
        => ValueTask.FromResult(new AccessToken("local-test-token", DateTimeOffset.UtcNow.AddHours(1)));
}
sealed class LocalHttpHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    public string? LastBody { get; private set; }
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        LastBody = request.Content is null ? null : await request.Content.ReadAsStringAsync(ct);
        return respond(request);
    }
}
sealed class RecordingWriter : IDestinationWriter
{
    public string DestinationType => "cosmosdb-vector";
    public bool Fail { get; set; }
    public List<SharePointReplacement> Replacements { get; } = [];
    public Task WriteBatchAsync(Dictionary<string, object> config, List<EmbeddingResult> results, CancellationToken ct)
        => throw new Exception("SharePoint must not use the legacy writer");
    public Task<bool> ReplaceSharePointAsync(Dictionary<string, object> config, SharePointReplacement replacement, CancellationToken ct)
    {
        if (Fail) throw new InvalidOperationException("Simulated destination failure");
        Replacements.Add(replacement);
        return Task.FromResult(true);
    }
}
sealed class LocalReceiver : ServiceBusReceiver
{
    public int Completed { get; private set; }
    public int Abandoned { get; private set; }
    public override Task CompleteMessageAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
    { Completed++; return Task.CompletedTask; }
    public override Task AbandonMessageAsync(ServiceBusReceivedMessage message,
        IDictionary<string, object>? propertiesToModify = null, CancellationToken cancellationToken = default)
    { Abandoned++; return Task.CompletedTask; }
}
