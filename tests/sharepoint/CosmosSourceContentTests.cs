using System.Net;
using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.Logging.Abstractions;
using Newtonsoft.Json.Linq;
using OmniVec.Worker.Destinations;
using static OmniVec.Worker.Destinations.CosmosDbDestinationWriter;

internal static class CosmosSourceContentTests
{
    private const string Now = "2026-09-08T00:00:00.0000000Z";

    private static void Check(bool condition, string message = "Assertion failed")
    {
        if (!condition) throw new Exception(message);
    }

    private static EmbeddingResult Document(string id = "doc-001", int sourceFieldCount = 0) => new(
        id, id, [0.1f, 0.2f], "new-hash", "tenant-1", "pipeline", "name", "", "processed text",
        Enumerable.Range(0, sourceFieldCount).ToDictionary(i => $"field-{i}", i => $"new-{i}"),
        MetadataFields: []);

    private static List<List<DocumentPatch>> Plan(params EmbeddingResult[] docs)
        => BuildPatchBatches(docs, "tenant", "embedding", Now);

    private static JObject Create(EmbeddingResult doc)
        => JObject.FromObject(BuildDocumentFields(doc, "tenant", "embedding", Now, true));

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string, Func<Task>)>();
        void Test(string name, Action run) => tests.Add((name, () => { run(); return Task.CompletedTask; }));

        foreach (bool? storeContent in new bool?[] { null, false, true })
            Test($"Cosmos refreshes raw source fields with StoreContent={storeContent?.ToString() ?? "default"}", () =>
            {
                var original = Document() with
                {
                    ContentHash = "old-hash", Embedding = [0.9f],
                    SourceContentFields = new() { ["content"] = "old source", ["title"] = "old title" },
                    StoreContent = storeContent,
                };
                var store = new MemoryPatchStore();
                store.Items[original.DocId] = Create(original);
                store.Items[original.DocId]["unrelated"] = new JObject { ["keep"] = true };
                var updated = original with
                {
                    ContentHash = "new-hash", Embedding = [0.1f, 0.2f],
                    SourceContentFields = new() { ["content"] = "new source", ["title"] = "new title" },
                };
                foreach (var batch in Plan(updated)) store.Execute(batch);
                var result = store.Items[original.DocId];
                Check(result["content"]!.Value<string>() == "new source");
                Check(result["title"]!.Value<string>() == "new title");
                Check(result["content_hash"]!.Value<string>() == "new-hash");
                Check(JToken.DeepEquals(result["embedding"], JArray.FromObject(updated.Embedding)));
                Check(result["unrelated"]!["keep"]!.Value<bool>());
                foreach (var field in Create(updated).Properties())
                    Check(JToken.DeepEquals(result[field.Name], field.Value), $"Patch/create mismatch: {field.Name}");
                var paths = Plan(updated).Single().Single().Operations.SelectMany(ops => ops).Select(op => op.Path).ToList();
                Check(paths.Count == paths.Distinct().Count(), "Overlapping raw/processed content must be deduplicated");
            });

        Test("Cosmos processed text remains opt-in without source fields", () =>
        {
            foreach (bool? enabled in new bool?[] { null, false, true })
            {
                var doc = Document() with { StoreContent = enabled, ContentField = "processed", SourceContentFields = null };
                var fields = BuildDocumentFields(doc, "tenant", "embedding", Now, false);
                Check(fields.ContainsKey("processed") == (enabled == true));
                Check(Create(doc).ContainsKey("processed") == (enabled == true));
                var store = new MemoryPatchStore();
                store.Items[doc.DocId] = new JObject { ["processed"] = "existing", ["content"] = "untouched" };
                store.Execute(Plan(doc).Single());
                Check(store.Items[doc.DocId]["processed"]!.Value<string>() == (enabled == true ? doc.Content : "existing"));
                Check(store.Items[doc.DocId]["content"]!.Value<string>() == "untouched");
            }
            var empty = Document() with { StoreContent = true, Content = "" };
            Check(!Create(empty).ContainsKey("content"));
            Check(!BuildDocumentFields(empty, "tenant", "embedding", Now, false).ContainsKey("content"));
        });

        Test("Cosmos retains separate processed content and optional metadata semantics", () =>
        {
            var doc = Document() with
            {
                StoreContent = true, ContentField = "processed", PipelineGeneration = "generation",
                SourceId = "source", SourceContentFields = new() { ["content"] = "raw", ["title"] = "title" },
            };
            foreach (var forCreate in new[] { false, true })
            {
                var fields = BuildDocumentFields(doc, "tenant", "embedding", Now, forCreate);
                Check((string)fields["processed"] == doc.Content && (string)fields["content"] == "raw");
                Check((string)fields["pipeline_generation"] == "generation");
                Check(!fields.ContainsKey("embedding_dims") && !fields.ContainsKey("pipeline_name"));
                Check(!fields.ContainsKey("source_ref"));
                Check(fields.ContainsKey("source_id") == forCreate);
                var defaults = BuildDocumentFields(doc with { MetadataFields = null }, "tenant", "embedding", Now, forCreate);
                Check((int)defaults["embedding_dims"] == 2 && (string)defaults["pipeline_name"] == "name");
                Check(defaults.ContainsKey("source_ref") == forCreate);
            }
        });

        Test("Cosmos escapes literal JSON pointer fields and deduplicates configured content", () =>
        {
            var doc = Document() with
            {
                StoreContent = true, ContentField = "body/~text",
                SourceContentFields = new() { ["body/~text"] = "raw", ["~1"] = "literal", [""] = "empty name" },
            };
            var batch = BuildPatchBatches([doc], "tenant", "vector/~value", Now).Single();
            var operations = batch.Single().Operations.SelectMany(ops => ops).ToList();
            Check(operations.Count(op => op.Path == "/body~1~0text") == 1);
            Check(operations.Any(op => op.Path == "/~01") && operations.Any(op => op.Path == "/"));
            Check(operations.Any(op => op.Path == "/vector~1~0value"));
            var store = new MemoryPatchStore();
            store.Items[doc.DocId] = new JObject { ["body"] = new JObject { ["~text"] = "do not change" } };
            store.Execute(batch);
            Check(store.Items[doc.DocId]["body/~text"]!.Value<string>() == "raw");
            Check(store.Items[doc.DocId]["body"]!["~text"]!.Value<string>() == "do not change");
            Check(store.Items[doc.DocId]["~1"]!.Value<string>() == "literal");
        });

        Test("Cosmos never patches reserved id or partition key and creates canonical identities", () =>
        {
            foreach (var contentField in new[] { "id", "tenant" })
            {
                var doc = Document() with
                {
                    StoreContent = true, ContentField = contentField,
                    SourceContentFields = new() { ["id"] = "wrong-id", ["tenant"] = "wrong-tenant", ["content"] = "raw" },
                };
                var operations = Plan(doc).Single().Single().Operations.SelectMany(ops => ops).ToList();
                Check(operations.All(op => op.Path != "/id" && op.Path != "/tenant"));
                var created = Create(doc);
                Check(created["id"]!.Value<string>() == doc.DocId);
                Check(created["tenant"]!.Value<string>() == doc.PartitionKeyValue);
                Check(created["content"]!.Value<string>() == "raw");
            }
            var idPartition = Document() with { PartitionKeyValue = "doc-001", SourceContentFields = new() { ["id"] = "wrong" } };
            Check(BuildPatchBatches([idPartition], "id", "embedding", Now).Single().Single()
                .Operations.SelectMany(ops => ops).All(op => op.Path != "/id"));
            Check((string)BuildDocumentFields(idPartition, "id", "embedding", Now, true)["id"] == "doc-001");
        });

        Test("Cosmos splits 11 fields into atomic patches and respects transaction boundaries", () =>
        {
            var docs = Enumerable.Range(0, 101).Select(i => Document($"doc-{i}", 7)).ToArray();
            var batches = Plan(docs);
            Check(batches.Select(batch => batch.Sum(doc => doc.Operations.Count)).SequenceEqual([100, 100, 2]));
            Check(batches.SelectMany(batch => batch).Select(doc => doc.Document.DocId).SequenceEqual(docs.Select(doc => doc.DocId)));
            foreach (var batch in batches)
                foreach (var doc in batch)
                    Check(doc.Operations.Select(ops => ops.Length).SequenceEqual([10, 1]));

            var store = new MemoryPatchStore();
            foreach (var doc in docs)
                store.Items[doc.DocId] = new JObject { ["field-6"] = "old", ["content_hash"] = "old", ["unrelated"] = 42 };
            // Failing the second PatchItem for the first document must not expose
            // its already-staged hash/vector changes outside the transaction.
            Check(!store.Execute(batches[0], failOperation: 1));
            Check(store.Items.Values.All(item => item["content_hash"]!.Value<string>() == "old"));
            foreach (var batch in batches) Check(store.Execute(batch));
            Check(store.Items.Values.All(item => item["field-6"]!.Value<string>() == "new-6"
                && item["content_hash"]!.Value<string>() == "new-hash"
                && item["unrelated"]!.Value<int>() == 42));
        });

        Test("Cosmos id partitions preserve every chunk identity", () =>
        {
            var chunks = Enumerable.Range(0, 16).Select(i => Document($"blob.txt#chunk{i}") with
            {
                SourceRef = "blob.txt", PartitionKeyValue = "blob.txt",
            }).ToList();
            var ids = chunks.Select(doc =>
            {
                var created = BuildDocumentFields(doc, "id", "embedding", Now, true);
                Check((string)created["id"] == doc.DocId);
                Check(DestinationPartitionKey(doc, "id") == doc.DocId);
                Check(DestinationPartitionKey(doc, "tenant") == "blob.txt");
                return (string)created["id"];
            }).ToList();
            Check(ids.Distinct().Count() == 16, "Chunks must not overwrite one another");
            Check(GroupDeletionIds(ids, "/id", "blob.txt").All(group =>
                group.Count() == 1 && group.Key == group.Single()));
            var sharedPartition = GroupDeletionIds(ids, "/tenant", "blob.txt").Single();
            Check(sharedPartition.Key == "blob.txt" && sharedPartition.Count() == 16);
        });

        Test("Cosmos id-partition deletion includes legacy collapsed document and all chunks", () =>
        {
            string[] ids = ["blob.txt", "blob.txt#chunk0", "blob.txt#chunk1"];
            var groups = GroupDeletionIds(ids, "/id", "blob.txt").ToList();
            Check(groups.Select(group => group.Key).SequenceEqual(ids));
            Check(!GroupDeletionIds([], "/id", "blob.txt").Any());
        });

        Test("Cosmos handles exact patch limits and fails oversized atomic plans before returning batches", () =>
        {
            Check(Plan(Document(sourceFieldCount: 6)).Single().Single().Operations.Single().Length == 10);
            var maximum = Document(sourceFieldCount: 996);
            var batches = Plan(maximum, Document("next"));
            Check(batches.Count == 2 && batches[0].Single().Operations.Count == 100);
            Check(batches[0].Single().Operations.All(ops => ops.Length == 10));
            try
            {
                Plan(Document(), Document("oversized", 997));
                throw new Exception("Expected a fail-fast atomic limit error");
            }
            catch (InvalidOperationException ex)
            {
                Check(ex.Message.Contains("1000-field atomic patch limit"));
            }
            Check(Plan().Count == 0);
        });

        tests.Add(("Cosmos mixed missing/existing batch preserves unrelated fields during fallback", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            var existing = Document("existing", 17);
            var missing = Document("missing", 17);
            var store = new MemoryPatchStore();
            store.Items[existing.DocId] = new JObject { ["unrelated"] = "keep", ["field-0"] = "old" };
            var upserts = new List<string>();
            var executions = new List<List<DocumentPatch>>();
            await writer.WritePatchBatchWithRetryAsync(Plan(existing, missing).Single(), "tenant-1",
                (batch, _) =>
                {
                    executions.Add(batch);
                    Check(batch.Sum(doc => doc.Operations.Count) <= 100);
                    if (batch.Any(doc => !store.Items.ContainsKey(doc.Document.DocId)))
                        return Task.FromResult(HttpStatusCode.NotFound);
                    Check(store.Execute(batch));
                    return Task.FromResult(HttpStatusCode.OK);
                },
                (doc, _) =>
                {
                    upserts.Add(doc.DocId);
                    store.Items[doc.DocId] = Create(doc);
                    return Task.CompletedTask;
                }, default);
            Check(upserts.SequenceEqual(["missing"]), "Fallback must not upsert existing neighbors");
            Check(executions.Count == 3);
            Check(executions.All(batch => batch.All(doc => doc.Operations.Count == 3)));
            Check(store.Items["existing"]["unrelated"]!.Value<string>() == "keep");
            Check(store.Items.Values.All(item => item["field-0"]!.Value<string>() == "new-0"
                && item["field-16"]!.Value<string>() == "new-16"
                && item["content_hash"]!.Value<string>() == "new-hash"));
        }));

        tests.Add(("Cosmos non-retryable patch failures propagate without upsert fallback", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            var upserts = 0;
            Exception? failure = null;
            try
            {
                await writer.WritePatchBatchWithRetryAsync(Plan(Document()).Single(), "tenant-1",
                    (_, _) => Task.FromResult(HttpStatusCode.BadRequest),
                    (_, _) => { upserts++; return Task.CompletedTask; }, default);
            }
            catch (Exception ex) { failure = ex; }
            Check(failure?.Message == "Batch patch failed: BadRequest" && upserts == 0);
        }));

        tests.Add(("Cosmos failed atomic transaction leaves source and embedding unchanged until retry", async () =>
        {
            var writer = new CosmosDbDestinationWriter(NullLogger<CosmosDbDestinationWriter>.Instance);
            var doc = Document(sourceFieldCount: 17);
            var store = new MemoryPatchStore();
            store.Items[doc.DocId] = new JObject
            {
                ["id"] = doc.DocId, ["tenant"] = doc.PartitionKeyValue,
                ["field-0"] = "old", ["embedding"] = new JArray(9), ["content_hash"] = "old",
            };
            var plan = Plan(doc).Single();
            var cancel = new OperationCanceledException("interrupted transaction");
            Exception? failure = null;
            try
            {
                await writer.WritePatchBatchWithRetryAsync(plan, "tenant-1", (batch, _) =>
                {
                    Check(!store.Execute(batch, failOperation: 2));
                    return Task.FromException<HttpStatusCode>(cancel);
                }, (_, _) => throw new Exception("Unexpected upsert"), default);
            }
            catch (OperationCanceledException ex) { failure = ex; }
            Check(ReferenceEquals(failure, cancel));
            Check(store.Items[doc.DocId]["field-0"]!.Value<string>() == "old");
            Check(store.Items[doc.DocId]["embedding"]![0]!.Value<int>() == 9);
            Check(store.Items[doc.DocId]["content_hash"]!.Value<string>() == "old");
            await writer.WritePatchBatchWithRetryAsync(plan, "tenant-1",
                (batch, _) => Task.FromResult(store.Execute(batch) ? HttpStatusCode.OK : HttpStatusCode.BadRequest),
                (_, _) => throw new Exception("Unexpected upsert"), default);
            Check(JToken.DeepEquals(store.Items[doc.DocId], Create(doc)));
        }));

        var failed = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS {name}"); }
            catch (Exception ex) { failed++; Console.WriteLine($"FAIL {name}: {ex}"); }
        }
        Console.WriteLine($"COSMOS SOURCE RESULT: {tests.Count - failed} passed, {failed} failed (local patch plans; no Azure writes)");
        return failed;
    }

    private sealed class MemoryPatchStore
    {
        public Dictionary<string, JObject> Items { get; private set; } = new();

        public bool Execute(List<DocumentPatch> batch, int failOperation = -1)
        {
            Check(batch.Sum(doc => doc.Operations.Count) <= 100, "Transaction exceeds 100 operations");
            var pending = Items.ToDictionary(item => item.Key, item => (JObject)item.Value.DeepClone());
            var index = 0;
            foreach (var doc in batch)
                foreach (var operations in doc.Operations)
                {
                    Check(operations.Length is > 0 and <= 10, "Patch exceeds 10 fields");
                    if (index++ == failOperation) return false;
                    foreach (var op in operations)
                    {
                        Check(op.OperationType == PatchOperationType.Set);
                        Check(!op.Path[1..].Contains('/'), "Expected escaped top-level field");
                        var field = op.Path[1..].Replace("~1", "/").Replace("~0", "~");
                        var value = op.GetType().GetProperty("Value")!.GetValue(op);
                        pending[doc.Document.DocId][field] = value is null ? JValue.CreateNull() : JToken.FromObject(value);
                    }
                }
            Items = pending;
            return true;
        }
    }
}
