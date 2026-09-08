using System.Net;
using System.Reflection;
using System.Text.Json;
using Azure.Messaging.ServiceBus;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Models;
using OmniVec.Worker.Services;

internal static class CosmosChunkTests
{
    private static void Check(bool value) { if (!value) throw new Exception("Assertion failed"); }
    private static async Task Fails(Func<Task> action)
    {
        try { await action(); } catch { return; }
        throw new Exception("Expected failure");
    }
    private static EmbeddingMessage Message() => new()
    {
        PipelineId = "pipeline", SourceId = "source", SourceRef = "document.txt", PartitionKeyValue = "tenant",
        DestinationType = "cosmosdb-vector", DestinationId = "destination", DocgrokPipeline = "mdl-test",
        ContentStrategy = "chunk", Content = new string('a', 240),
        ChunkConfig = new() { Size = 100, Overlap = 20, StoreText = true }, MetadataFields = [],
        SourceContentFields = new() { ["text"] = "MUST NOT overwrite chunk text" },
    };

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string, Func<Task>)>();
        void Test(string name, Action run) => tests.Add((name, () => { run(); return Task.CompletedTask; }));
        Test("Cosmos chunk settings cross ingestion pipeline and queue JSON unchanged", () =>
        {
            var pipeline = JsonSerializer.Deserialize<OmniVec.ChangeFeed.Models.Pipeline>(
                """{"content_strategy":"chunk","chunk_config":{"chunk_size":300,"chunk_overlap":50,"chunk_unit":"tokens","store_text":true,"text_field":"body","doc_id_pattern":"{pipeline}-{source_hash}-{chunk}"}}""")!;
            var message = new OmniVec.ChangeFeed.Models.EmbeddingMessage
            { ContentStrategy = pipeline.ContentStrategy, ChunkConfig = pipeline.ChunkConfig };
            var worker = JsonSerializer.Deserialize<EmbeddingMessage>(JsonSerializer.Serialize(message))!;
            Check(worker.ContentStrategy == "chunk" && worker.ChunkConfig!.Size == 300
                && worker.ChunkConfig.Overlap == 50 && worker.ChunkConfig.Unit == "tokens"
                && worker.ChunkConfig.StoreText && worker.ChunkConfig.TextField == "body");
            Check(new EmbeddingMessage().ContentStrategy == "truncate"
                && new OmniVec.ChangeFeed.Models.Pipeline().ContentStrategy == "truncate");
        });
        Test("Character chunks retain overlap, Python boundaries and Unicode codepoints", () =>
        {
            var config = new TextChunkConfig { Size = 100, Overlap = 20 };
            var text = new string('a', 85) + ". " + new string('b', 120);
            var chunks = CosmosTextChunker.Split(text, config);
            Check(chunks.Count == 3 && chunks[0] == new string('a', 85) + ".");
            Check(chunks[1].StartsWith(new string('a', 18) + ". "));
            var unicode = CosmosTextChunker.Split(string.Concat(Enumerable.Repeat("😀", 150)), config);
            Check(unicode.Count == 2 && unicode[0].EnumerateRunes().Count() == 100);
            Check(CosmosTextChunker.Split(" \n\t", config).Count == 0);
            Check(CosmosTextChunker.Split("  short text  ", config).Single() == "  short text  ");
            config.Overlap = 99;
            Check(CosmosTextChunker.Split(text, config).Count < text.Length);
        });
        Test("Tokens match Python whitespace tokenization and overlap", () =>
        {
            var words = Enumerable.Range(0, 210).Select(i => $"word{i}").ToArray();
            var chunks = CosmosTextChunker.Split(string.Join("\n", words),
                new() { Size = 100, Overlap = 20, Unit = "tokens" });
            Check(chunks.Count == 3 && chunks[1].Split(' ')[0] == "word80"
                && chunks[2].Split(' ')[^1] == "word209");
        });
        Test("Chunk identities honor all variables and isolate source, pipeline and source partitions", () =>
        {
            var msg = Message();
            var config = msg.ChunkConfig!;
            var id = CosmosTextChunker.DocId(msg, config, 0);
            Check(id.EndsWith("-document-chunk-000") && id == CosmosTextChunker.DocId(msg, config, 0));
            msg.SourceId = "other";
            Check(id != CosmosTextChunker.DocId(msg, config, 0));
            msg = Message(); msg.PipelineId = "other";
            Check(id != CosmosTextChunker.DocId(msg, config, 0));
            msg = Message(); msg.PartitionKeyValue = "other";
            Check(id != CosmosTextChunker.DocId(msg, config, 0));
            config.DocIdPattern = "{source}-{source_ref}-{source_hash}-{chunk}-{pipeline}-{pipeline_hash}";
            Check(CosmosTextChunker.DocId(Message(), config, 7).Contains("-007-pipeline-"));
        });
        Test("Configured chunk-name suffixes retain source references and cannot collide", () =>
        {
            var config = new TextChunkConfig { Size = 100, Overlap = 20, DocIdPattern = "custom-{source}-{chunk}" };
            var msg = Message();
            var ids = new HashSet<string>();
            foreach (var source in new[] { "source-a", "source-b" })
                foreach (var pipeline in new[] { "pipeline-a", "pipeline-b" })
                    foreach (var partition in new[] { "tenant-a", "tenant-b" })
                        foreach (var reference in new[] { "folder-a/document.txt", "folder-b/document.txt" })
                        {
                            msg.SourceId = source; msg.PipelineId = pipeline;
                            msg.PartitionKeyValue = partition; msg.SourceRef = reference;
                            for (var index = 0; index < 12; index++)
                            {
                                var result = CosmosTextChunker.Result(msg, config, "text", [1, 2], index, 12);
                                Check(ids.Add(result.DocId));
                                Check(result.DocId.EndsWith($"-custom-document-{index:D3}"));
                                Check(result.SourceRef == reference && result.DocId != result.SourceRef);
                            }
                        }
            Check(ids.Count == 192);
        });
        foreach (var invalid in new TextChunkConfig[] {
            new() { Size = 99 }, new() { Overlap = -1 }, new() { Overlap = 1000 },
            new() { Unit = "bpe" }, new() { DocIdPattern = "{source}" },
            new() { DocIdPattern = "{unknown}-{chunk}" }, new() { TextField = "source_ref" },
        })
            tests.Add(("Invalid chunk config is rejected", () => Fails(() =>
            { CosmosTextChunker.Split("text", invalid); return Task.CompletedTask; })));
        Test("Chunk text opt-in and mandatory cleanup metadata cannot be overwritten by raw source", () =>
        {
            var msg = Message();
            foreach (var store in new[] { true, false })
            {
                msg.ChunkConfig!.StoreText = store;
                var result = CosmosTextChunker.Result(msg, msg.ChunkConfig, "chunk text", [1, 2], 0, 3);
                foreach (var create in new[] { true, false })
                {
                    var fields = CosmosDbDestinationWriter.BuildDocumentFields(result, "id", "embedding", "now", create);
                    Check(fields.ContainsKey("text") == store && fields["source_ref"].Equals(msg.SourceRef)
                        && fields["source_id"].Equals(msg.SourceId) && fields["chunk_count"].Equals(3)
                        && fields["chunk_source_partition"].Equals(msg.PartitionKeyValue));
                    if (store) Check(fields["text"].Equals("chunk text"));
                }
            }
        });
        tests.Add(("Replacement never cleans on write failure and surfaces cleanup failures", async () =>
        {
            var called = false;
            await Fails(() => CosmosDbDestinationWriter.ReplaceTextChunksInStoreAsync([],
                _ => throw new IOException("write"), _ => { called = true; return Task.CompletedTask; }));
            Check(!called);
            await Fails(() => CosmosDbDestinationWriter.ReplaceTextChunksInStoreAsync([],
                _ => Task.CompletedTask, _ => throw new IOException("cleanup")));
        }));
        tests.Add(("Actual worker chunks before truncation, shrinks, empties and retries failed writes", async () =>
        {
            var inputs = new List<string>();
            var client = new DocGrokClient(new HttpClient(new ChunkHttpHandler(request =>
            {
                using var payload = JsonDocument.Parse(request.Content!.ReadAsStringAsync().GetAwaiter().GetResult());
                var texts = payload.RootElement.GetProperty("texts").EnumerateArray().Select(v => v.GetString()!).ToList();
                inputs.AddRange(texts);
                return new(HttpStatusCode.OK) { Content = new StringContent(JsonSerializer.Serialize(new
                    { outputs = texts.Select((_, i) => new float[] { 1, i + 1 }).ToArray() })) };
            })) { BaseAddress = new("http://local.invalid") }, NullLogger<DocGrokClient>.Instance);
            var writer = new ChunkWriter();
            using var worker = new EmbeddingWorkerService(
                Options.Create(new WorkerOptions { MaxSingleTextTokens = 50, EmbedBatchSize = 2 }), null, client,
                new SharePointContentClient(new HttpClient()),
                new MetricsReporter(new HttpClient(new ChunkHttpHandler(_ => new(HttpStatusCode.OK)))
                    { BaseAddress = new("http://local.invalid") }, NullLogger<MetricsReporter>.Instance),
                [writer], NullLogger<EmbeddingWorkerService>.Instance);
            var receiver = new ChunkReceiver();
            var msg = Message();
            async Task Process()
            {
                var delivery = ServiceBusModelFactory.ServiceBusReceivedMessage(
                    body: BinaryData.FromString(JsonSerializer.Serialize(msg)), messageId: Guid.NewGuid().ToString(),
                    lockedUntil: DateTimeOffset.UtcNow.AddMinutes(5));
                await (Task)typeof(EmbeddingWorkerService).GetMethod("ProcessReceivedBatchAsync",
                    BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(worker,
                        [receiver, new[] { delivery }, CancellationToken.None])!;
            }
            await Process();
            Check(writer.Items.Count == 3 && receiver.Completed == 1 && inputs.Sum(t => t.Length) == 280);
            var originalId = writer.Items[0].DocId;
            msg.Content = "short"; await Process();
            Check(writer.Items.Count == 1 && writer.Items[0].DocId == originalId);
            writer.Fail = true; await Process();
            Check(receiver.Completed == 2 && receiver.Abandoned == 1 && writer.Items.Count == 1);
            writer.Fail = false; msg.Content = ""; await Process();
            Check(writer.Items.Count == 0 && receiver.Completed == 3);
        }));
        var failed = 0;
        foreach (var (name, test) in tests)
        {
            try { await test(); Console.WriteLine($"PASS {name}"); }
            catch (Exception ex) { failed++; Console.WriteLine($"FAIL {name}: {ex}"); }
        }
        Console.WriteLine($"COSMOS CHUNK: {tests.Count - failed} passed, {failed} failed (local doubles)");
        return failed;
    }
    private sealed class ChunkHttpHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
            => Task.FromResult(respond(request));
    }
    private sealed class ChunkWriter : IDestinationWriter
    {
        public string DestinationType => "cosmosdb-vector";
        public List<EmbeddingResult> Items = [];
        public bool Fail;
        public Task WriteBatchAsync(Dictionary<string, object> config, List<EmbeddingResult> results, CancellationToken ct)
            => throw new Exception("Must use chunk replacement");
        public Task ReplaceTextChunksAsync(Dictionary<string, object> config, DeleteRequest source,
            List<EmbeddingResult> chunks, CancellationToken ct)
        {
            if (Fail) throw new IOException("write failed");
            Items = chunks; return Task.CompletedTask;
        }
    }
    private sealed class ChunkReceiver : ServiceBusReceiver
    {
        public int Completed, Abandoned;
        public override Task CompleteMessageAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
        { Completed++; return Task.CompletedTask; }
        public override Task AbandonMessageAsync(ServiceBusReceivedMessage message,
            IDictionary<string, object>? propertiesToModify = null, CancellationToken ct = default)
        { Abandoned++; return Task.CompletedTask; }
    }
}
