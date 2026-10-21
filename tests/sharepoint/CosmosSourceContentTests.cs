using System.Net;
using Microsoft.Extensions.Logging.Abstractions;
using Newtonsoft.Json.Linq;
using OmniVec.Worker.Destinations;
using static OmniVec.Worker.Destinations.CosmosDbDestinationWriter;

internal static class CosmosSourceContentTests
{
    private const string Now = "2026-09-08T00:00:00.0000000Z";
    private static void Check(bool condition)
    {
        if (!condition) throw new Exception("Assertion failed");
    }
    private static EmbeddingResult Document(string id = "doc-001") => new(
        id, id, [0.1f, 0.2f], "new-hash", "tenant-1", "pipeline", "name", "", "processed",
        MetadataFields: []);
    private static List<List<JObject>> Plan(params EmbeddingResult[] docs)
        => BuildUpsertBatches(docs, "tenant", "embedding", Now);

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string, Func<Task>)>();
        void Test(string name, Action run) => tests.Add((name, () => { run(); return Task.CompletedTask; }));
        Test("Separate Cosmos destination upserts replace existing documents and refresh source fields", () =>
        {
            foreach (bool? storeContent in new bool?[] { null, false, true })
            {
                var doc = Document() with
                {
                    SourceId = "source", StoreContent = storeContent,
                    SourceContentFields = new() { ["content"] = "raw", ["title"] = "new title" },
                };
                var item = Plan(doc).Single().Single();
                Check(item["content"]!.Value<string>() == "raw");
                Check(item["title"]!.Value<string>() == "new title");
                Check(item["content_hash"]!.Value<string>() == "new-hash");
                Check(!item.ContainsKey("unrelated") && !item.ContainsKey("pipeline_name"));
                Check(item["source_id"]!.Value<string>() == "source");
                Check(JToken.DeepEquals(item["embedding"], JArray.FromObject(doc.Embedding)));
            }
        });
        Test("Processed content and optional metadata retain their opt-in semantics", () =>
        {
            foreach (bool? enabled in new bool?[] { null, false, true })
            {
                var doc = Document() with
                {
                    StoreContent = enabled, ContentField = "processed", PipelineGeneration = "generation",
                };
                var item = Plan(doc).Single().Single();
                Check(item.ContainsKey("processed") == (enabled == true));
                Check(item["pipeline_generation"]!.Value<string>() == "generation");
                Check(!item.ContainsKey("source_ref") && !item.ContainsKey("embedding_dims"));
                var defaults = Plan(doc with { MetadataFields = null }).Single().Single();
                Check(defaults["source_ref"]!.Value<string>() == doc.SourceRef);
                Check(defaults["embedding_dims"]!.Value<int>() == 2);
            }
            Check(!Plan(Document() with { StoreContent = true, Content = "" }).Single().Single().ContainsKey("content"));
        });
        Test("Upserts preserve literal field names and canonical document/partition identities", () =>
        {
            var doc = Document() with
            {
                SourceContentFields = new()
                {
                    ["id"] = "wrong", ["tenant"] = "wrong", ["body/~text"] = "literal",
                },
            };
            var item = Plan(doc).Single().Single();
            Check(item["id"]!.Value<string>() == doc.DocId);
            Check(item["tenant"]!.Value<string>() == doc.PartitionKeyValue);
            Check(item["body/~text"]!.Value<string>() == "literal");
            Check(DestinationPartitionKey(doc, "id") == doc.DocId);
            Check(DestinationPartitionKey(doc, "tenant") == "tenant-1");
            Check((string)BuildDocumentFields(doc, "id", "embedding", Now, true)["id"] == doc.DocId);
        });
        Test("Upsert batches enforce 100-operation and payload boundaries", () =>
        {
            var docs = Enumerable.Range(0, 201).Select(i => Document($"doc-{i}")).ToArray();
            var batches = Plan(docs);
            Check(batches.Select(batch => batch.Count).SequenceEqual([100, 100, 1]));
            Check(batches.SelectMany(batch => batch).Select(item => item["id"]!.Value<string>())
                .SequenceEqual(docs.Select(doc => doc.DocId)));
            var large = Document() with { StoreContent = true, Content = new string('x', 800_000) };
            Check(Plan(large, large with { DocId = "second" }).Count == 2);
            var nearLimit = large with { Content = new string('x', 1_900_000) };
            Check(Plan(Document(), nearLimit, Document("last")).Select(b => b.Count).SequenceEqual([1, 1, 1]));
            Check(Plan().Count == 0);
            try
            {
                Plan(Document(), large with { Content = new string('x', 2 * 1024 * 1024) });
                throw new Exception("Expected payload limit failure");
            }
            catch (InvalidOperationException) { }
            var manyFields = Document() with
            {
                SourceContentFields = Enumerable.Range(0, 1100).ToDictionary(i => $"field-{i}", _ => "value"),
            };
            Check(Plan(manyFields).Single().Single().ContainsKey("field-1099"));
        });
        Test("Deletion partition planning preserves /id and legacy source-partition behavior", () =>
        {
            string[] ids = ["blob", "blob-chunk-0", "blob-chunk-1"];
            Check(GroupDeletionIds(ids, "/id", "tenant").All(g => g.Count() == 1 && g.Key == g.Single()));
            Check(GroupDeletionIds(ids, "/tenant", "tenant").Single().Count() == 3);
            Check(DeletionPartitionKey("doc", "stored", "source", "/tenant") == "stored");
            Check(DeletionPartitionKey("doc", null, "source", "/tenant") == "source");
            Check(DeletionPartitionKey("doc", null, "source", "/id") == "doc");
        });
        tests.Add(("Mixed new/existing Cosmos documents use one direct upsert execution", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            var store = new Dictionary<string, JObject> { ["existing"] = new() { ["unrelated"] = "old" } };
            var calls = 0;
            await writer.WriteUpsertBatchWithRetryAsync(Plan(Document("existing"), Document("new")).Single(),
                "tenant-1", (items, _) =>
                {
                    calls++;
                    foreach (var item in items) store[item["id"]!.Value<string>()!] = item;
                    return Task.FromResult<(HttpStatusCode, TimeSpan?)>((HttpStatusCode.OK, null));
                }, default);
            Check(calls == 1 && store.Count == 2 && !store["existing"].ContainsKey("unrelated"));
        }));
        tests.Add(("Permanent failures and cancellation propagate without fallback writes", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            foreach (var status in new[] { HttpStatusCode.BadRequest, HttpStatusCode.NotFound })
            {
                var calls = 0;
                try
                {
                    await writer.WriteUpsertBatchWithRetryAsync(Plan(Document()).Single(), "tenant",
                        (_, _) =>
                        {
                            calls++;
                            return Task.FromResult<(HttpStatusCode, TimeSpan?)>((status, null));
                        }, default);
                    throw new Exception("Expected error");
                }
                catch (InvalidOperationException) { Check(calls == 1); }
            }
            try
            {
                await writer.WriteUpsertBatchWithRetryAsync(Plan(Document()).Single(), "tenant",
                    (_, _) => throw new OperationCanceledException(), default);
                throw new Exception("Expected cancellation");
            }
            catch (OperationCanceledException) { }
        }));
        tests.Add(("429 upsert responses retry using the same complete batch", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            var plan = Plan(Document("a"), Document("b")).Single();
            var calls = 0;
            await writer.WriteUpsertBatchWithRetryAsync(plan, "tenant", (items, _) =>
            {
                Check(ReferenceEquals(plan, items));
                return Task.FromResult<(HttpStatusCode, TimeSpan?)>(
                    (++calls == 1 ? HttpStatusCode.TooManyRequests : HttpStatusCode.OK, TimeSpan.FromMilliseconds(1)));
            }, default);
            Check(calls == 2);
        }));
        var failed = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS {name}"); }
            catch (Exception ex) { failed++; Console.WriteLine($"FAIL {name}: {ex}"); }
        }
        Console.WriteLine($"COSMOS SOURCE RESULT: {tests.Count - failed} passed, {failed} failed (local upsert plans; no Azure writes)");
        return failed;
    }
}
