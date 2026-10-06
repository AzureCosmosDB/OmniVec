using System.Net;
using System.Reflection;
using System.Text.Json;
using Microsoft.Extensions.Logging.Abstractions;
using Newtonsoft.Json.Linq;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using OmniVec.ChangeFeed.Services;
using static OmniVec.ChangeFeed.Services.SourceWatcher;

internal static class InlineCosmosTests
{
    private static void Check(bool condition, string message = "Assertion failed")
    {
        if (!condition) throw new Exception(message);
    }

    private static async Task<Exception> Fails(Func<Task> action)
    {
        try { await action(); }
        catch (Exception error) { return error; }
        throw new Exception("Expected failure");
    }

    private static object? Value(Microsoft.Azure.Cosmos.PatchOperation operation)
        => operation.GetType().GetProperty("Value")!.GetValue(operation);

    private static InlineEmbeddedDocument Document(int index, string partition = "partition")
        => new($"doc-{index}", partition, [0.1f, -0.25f], $"hash-{index}");

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string Name, Func<Task> Run)>();
        void Test(string name, Func<Task> run) => tests.Add((name, run));

        Test("Inline PATCH keeps the validated float array and all six metadata fields", () =>
        {
            var document = Document(0);
            var pipeline = new Pipeline { Id = "pipeline", Name = "name", VectorIndexPath = "/nested/vector" };
            var operations = BuildInlinePatchOperations(document, pipeline, "timestamp");
            Check(operations.Count == 6);
            Check(operations.Select(operation => operation.Path).SequenceEqual(
                ["/nested/vector", "/embedded_at", "/embedding_dims", "/pipeline_id", "/pipeline_name", "/content_hash"]));
            Check(ReferenceEquals(Value(operations[0]), document.Embedding), "Vector must not be serialized and reparsed");
            Check((string)Value(operations[1])! == "timestamp");
            Check((int)Value(operations[2])! == 2);
            Check((string)Value(operations[3])! == "pipeline");
            Check((string)Value(operations[4])! == "name");
            Check((string)Value(operations[5])! == "hash-0");
            Check(InlinePatchRequestOptions.EnableContentResponseOnWrite == false);
            return Task.CompletedTask;
        });
        Test("Inline batches preserve partitions and every document with the 100-op limit", async () =>
        {
            var docs = Enumerable.Range(0, 201).Select(index => Document(index)).ToList();
            docs.Add(Document(201, "other"));
            var batches = new List<(string Partition, List<InlineEmbeddedDocument> Docs)>();
            var result = await ExecuteInlinePatchBatchesAsync(docs, 1, (partition, batch, _) =>
            {
                batches.Add((partition, batch));
                Check(batch.All(doc => doc.PartitionKey == partition));
                return Task.FromResult(new InlinePatchResult(batch.Count, 0, batch.Count * 2, batch.Count));
            }, default);
            Check(batches.Select(batch => batch.Docs.Count).SequenceEqual([100, 100, 1, 1]));
            Check(batches.SelectMany(batch => batch.Docs).SequenceEqual(docs));
            Check(result.Patched == 202 && result.Failed == 0 && result.RequestCharge == 404);
            Check(result.MaxSdkMilliseconds == 100);
        });
        Test("Inline write concurrency bounds actual in-flight transactions", async () =>
        {
            var docs = Enumerable.Range(0, 24).Select(index => Document(index, $"pk-{index}")).ToList();
            int active = 0, maximum = 0;
            var result = await ExecuteInlinePatchBatchesAsync(docs, 3, async (_, batch, token) =>
            {
                var count = Interlocked.Increment(ref active);
                int prior;
                do { prior = maximum; }
                while (count > prior && Interlocked.CompareExchange(ref maximum, count, prior) != prior);
                try
                {
                    await Task.Delay(10, token);
                    return new InlinePatchResult(batch.Count, 0);
                }
                finally { Interlocked.Decrement(ref active); }
            }, default);
            Check(maximum == 3 && active == 0 && result.Patched == docs.Count);
        });
        Test("Legacy zero concurrency and empty inline pages remain supported", async () =>
        {
            var docs = Enumerable.Range(0, 4).Select(index => Document(index, $"pk-{index}")).ToList();
            var result = await ExecuteInlinePatchBatchesAsync(docs, 0,
                (_, batch, _) => Task.FromResult(new InlinePatchResult(batch.Count, 0)), default);
            Check(result.Patched == 4);
            result = await ExecuteInlinePatchBatchesAsync([], 0,
                (_, _, _) => throw new Exception("Empty pages must not execute writes"), default);
            Check(result == new InlinePatchResult(0, 0));
        });
        Test("Partial PATCH failure is counted, never reported as complete persistence", async () =>
        {
            var docs = new List<InlineEmbeddedDocument> { Document(0, "good"), Document(1, "bad") };
            var result = await ExecuteInlinePatchBatchesAsync(docs, 2, (partition, batch, _) =>
                Task.FromResult(partition == "bad"
                    ? new InlinePatchResult(0, batch.Count)
                    : new InlinePatchResult(batch.Count, 0)), default);
            Check(result.Patched == 1 && result.Failed == 1);
        });
        Test("Thrown PATCH failures propagate and stop further bounded work", async () =>
        {
            var docs = Enumerable.Range(0, 4).Select(index => Document(index, $"pk-{index}")).ToList();
            int calls = 0;
            await Fails(() => ExecuteInlinePatchBatchesAsync(docs, 1, (_, _, _) =>
            {
                calls++;
                throw new InvalidOperationException("write failed");
            }, default));
            Check(calls == 1);
        });
        Test("Cancellation is propagated without admitting more PATCHes", async () =>
        {
            using var cancellation = new CancellationTokenSource();
            var docs = Enumerable.Range(0, 4).Select(index => Document(index, $"pk-{index}")).ToList();
            int calls = 0;
            var failure = await Fails(() => ExecuteInlinePatchBatchesAsync(docs, 1, (_, _, token) =>
            {
                calls++;
                cancellation.Cancel();
                token.ThrowIfCancellationRequested();
                return Task.FromResult(new InlinePatchResult(1, 0));
            }, cancellation.Token));
            Check(failure is OperationCanceledException && calls == 1);
        });
        Test("Inline PATCH honors positive RetryAfter and caps fallback backoff", () =>
        {
            Check(InlinePatchRetryDelay(1, TimeSpan.FromMilliseconds(17)) == TimeSpan.FromMilliseconds(17));
            Check(InlinePatchRetryDelay(5, TimeSpan.FromSeconds(45)) == TimeSpan.FromSeconds(45));
            Check(InlinePatchRetryDelay(1, null) == TimeSpan.FromSeconds(1));
            Check(InlinePatchRetryDelay(1, TimeSpan.Zero) == TimeSpan.FromSeconds(1));
            Check(InlinePatchRetryDelay(1, TimeSpan.FromMilliseconds(-1)) == TimeSpan.FromSeconds(1));
            Check(InlinePatchRetryDelay(20, null) == TimeSpan.FromSeconds(30));
            return Task.CompletedTask;
        });
        Test("Invalid inline concurrency fails explicitly instead of silently defaulting", async () =>
        {
            foreach (var concurrency in new[] { -1, 257 })
            {
                var failure = await Fails(() => ExecuteInlinePatchBatchesAsync([], concurrency,
                    (_, _, _) => Task.FromResult(new InlinePatchResult()), default));
                Check(failure is ArgumentOutOfRangeException);
                failure = await Fails(() =>
                {
                    _ = new SourceWatcher(new Source(), new ChangeFeedOptions { InlinePatchConcurrency = concurrency },
                        null!, null!, new ContentHasher(), NullLogger<SourceWatcher>.Instance);
                    return Task.CompletedTask;
                });
                Check(failure is ArgumentOutOfRangeException);
            }
        });
        Test("Inline model outputs still flow through real client validation before checkpoint refusal", async () =>
        {
            int calls = 0, documents = 0;
            using var client = new HttpClient(new Handler(async (request, token) =>
            {
                using var payload = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(token));
                Check(payload.RootElement.GetProperty("model_id").GetString() == "mdl-ext-test");
                int count = payload.RootElement.GetProperty("texts").GetArrayLength();
                Interlocked.Increment(ref calls);
                Interlocked.Add(ref documents, count);
                return new(HttpStatusCode.OK)
                {
                    Content = new StringContent(JsonSerializer.Serialize(new
                    {
                        outputs = Enumerable.Range(0, count).Select(_ => new[] { 0.1f, 0.2f }).ToArray(),
                    })),
                };
            })) { BaseAddress = new("http://local.invalid") };
            await using var watcher = new SourceWatcher(new Source(), new ChangeFeedOptions(),
                null!, null!, new ContentHasher(), NullLogger<SourceWatcher>.Instance, docGrokClient: client);
            var pipeline = new Pipeline { Id = "pipeline", DocgrokPipeline = "mdl-ext-test" };
            var docs = Enumerable.Range(0, 120).Select(index => (
                docId: $"doc-{index}", content: "text", contentHash: "hash",
                pkValue: $"pk-{index % 10}", doc: new JObject())).ToList();
            var method = typeof(SourceWatcher).GetMethod("ProcessInlineAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var failure = await Fails(() => (Task)method.Invoke(watcher,
                [new List<Pipeline> { pipeline }, docs, "lease", CancellationToken.None])!);
            Check(calls == 3 && documents == 120);
            Check(failure is InvalidOperationException && failure.Message.Contains("Patch failed"),
                "A missing destination must retain the checkpoint, not report indexed success");
        });

        int failed = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failed++; Console.WriteLine($"FAIL {name}: {error}"); }
        }
        Console.WriteLine($"RESULT: {tests.Count - failed} inline Cosmos tests passed, {failed} failed");
        return failed;
    }

    private sealed class Handler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send)
        : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
            => send(request, token);
    }
}
