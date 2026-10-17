using OmniVec.ChangeFeed.Models;
using OmniVec.ChangeFeed.Services;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.Worker.Destinations;
using Microsoft.Extensions.Logging.Abstractions;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using StackExchange.Redis;

internal static class GarnetSourceTests
{
    public static async Task<int> RunAsync()
    {
        var failures = 0;
        void Check(string name, Action test)
        {
            try { test(); Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failures++; Console.WriteLine($"FAIL {name}: {error.Message}"); }
        }
        void Require(bool condition)
        {
            if (!condition) throw new Exception("Assertion failed");
        }
        Check("Garnet source preserves JSON fields, stable reference and version", () =>
        {
            var record = GarnetSourceRecordParser.Parse("reference",
                """{"id":"document","version":2,"content":"hello","custom":true}""");
            Require(record.Id == "document" && record.Version == 2 && record.Content == "hello");
            Require(record.RawFields["custom"] == "true" && record.ContentHash.Length == 64 && !record.Deleted);
        });
        Check("Garnet versioned tombstones are explicit and replayable", () =>
        {
            var record = GarnetSourceRecordParser.Parse("reference",
                """{"id":"document","version":3,"content":"","deleted":true}""");
            Require(record.Deleted && record.Version == 3);
        });
        Check("Garnet malformed source contracts fail explicitly", () =>
        {
            foreach (var json in new[] { "[]", "{}",
                """{"id":"doc","version":0,"content":"hi"}""",
                """{"id":"doc","version":1.5,"content":"hi"}""",
                """{"id":"doc","version":1,"content":null}""",
                """{"id":"doc","version":1,"content":"hi","deleted":"true"}""" })
            {
                try { GarnetSourceRecordParser.Parse("ref", json); }
                catch (Exception error) when (error is System.Text.Json.JsonException or InvalidDataException)
                { continue; }
                throw new Exception($"Invalid source accepted: {json}");
            }
        });
        Check("Garnet same-version mutation has a different checkpoint digest", () =>
        {
            var first = GarnetSourceRecordParser.Parse("ref",
                """{"id":"doc","version":1,"content":"first"}""");
            var second = GarnetSourceRecordParser.Parse("ref",
                """{"id":"doc","version":1,"content":"second"}""");
            Require(first.ContentHash != second.ContentHash);
        });
        Check("Garnet model edits and resets advance destination fencing revision", () =>
        {
            var pipeline = new Pipeline { UpdatedAt = "2026-10-06T04:00:00Z" };
            var first = GarnetSourceWatcher.GetPipelineRevision(pipeline);
            pipeline.UpdatedAt = "2026-10-06T04:01:00Z";
            var edited = GarnetSourceWatcher.GetPipelineRevision(pipeline);
            pipeline.ResetAt = "2026-10-06T04:02:00Z";
            Require(edited > first && GarnetSourceWatcher.GetPipelineRevision(pipeline) > edited);
        });
        Check("Garnet endpoint aliases match while effective TLS and port stay distinct", () =>
        {
            Require(GarnetEndpoint.Parse("redis://HOST.test./") == GarnetEndpoint.Parse("host.test:6380"));
            Require(GarnetEndpoint.Parse("rediss://host.test:6379", false) == GarnetEndpoint.Parse("host.test", false));
            Require(GarnetEndpoint.Parse("[::1]", false) == new GarnetEndpoint("::1", 6379, false));
            Require(GarnetEndpoint.Parse("host:6380") != GarnetEndpoint.Parse("host:6380", false));
        });
        Check("Garnet ownership blocks endpoint aliases but not other keys or TLS configurations", () =>
        {
            Source Source(string id, string endpoint, bool tls, string key) => new()
            {
                Id = id, Type = "garnet", Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(
                    JsonSerializer.Serialize(new { endpoint, tls, hash_key = key }))!
            };
            var first = Source("first", "redis://HOST.test./", true, "documents");
            var alias = Source("alias", "host.test:6380", true, "documents");
            Require(InlineSourceOwnership.TargetKey(first) == InlineSourceOwnership.TargetKey(alias));
            var pipelines = new List<Pipeline>
            {
                new() { Id = "p1", Status = "active", ProcessingMode = "inline", Sources = [new() { SourceId = "first" }] },
                new() { Id = "p2", Status = "active", ProcessingMode = "inline", Sources = [new() { SourceId = "alias" }] }
            };
            Require(InlineSourceOwnership.FindBlockedSourceIds([first, alias], pipelines).SetEquals(["first", "alias"]));
            Require(InlineSourceOwnership.TargetKey(first) != InlineSourceOwnership.TargetKey(Source("tls", "host.test:6380", false, "documents")));
            Require(InlineSourceOwnership.TargetKey(first) != InlineSourceOwnership.TargetKey(Source("key", "host.test:6380", true, "other")));
        });
        async Task CheckAsync(string name, Func<Task> test)
        {
            try { await test(); Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failures++; Console.WriteLine($"FAIL {name}: {error}"); }
        }
        await CheckAsync("Garnet connection options use normalized host, effective port and explicit TLS", async () =>
        {
            var options = await OneLakeIcebergDestinationWriter.CreateGarnetOptionsAsync(
                new() { ["endpoint"] = "rediss://HOST.test./", ["tls"] = false, ["use_entra_auth"] = false }, default);
            Require(!options.Ssl && options.EndPoints.Single() is DnsEndPoint { Host: "host.test", Port: 6379 });
        });
        await CheckAsync("Garnet successful writes checkpoint, replay skips, and versioned tombstones delete", async () =>
        {
            using var modelClient = new HttpClient(new ModelHandler()) { BaseAddress = new Uri("http://model") };
            var writer = new MemoryWriter();
            var database = DispatchProxy.Create<IDatabase, DatabaseProxy>();
            var memory = (DatabaseProxy)(object)database;
            var watcher = Watcher(writer, modelClient);
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 1 && memory.Checkpoints.Count == 1);
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 1);
            memory.Record = """{"id":"doc","version":2,"content":"","deleted":true}""";
            await watcher.PollOnceAsync(default, database);
            Require(writer.Deletes.Count == 1 && writer.Deletes[0].SourceVersion == 2);
            await watcher.PollOnceAsync(default, database);
            Require(writer.Deletes.Count == 1);
        });
        await CheckAsync("Garnet destination failure and checkpoint failure retain replay eligibility", async () =>
        {
            using var modelClient = new HttpClient(new ModelHandler()) { BaseAddress = new Uri("http://model") };
            var writer = new MemoryWriter { FailWrites = true };
            var database = DispatchProxy.Create<IDatabase, DatabaseProxy>();
            var memory = (DatabaseProxy)(object)database;
            var watcher = Watcher(writer, modelClient);
            await watcher.PollOnceAsync(default, database);
            Require(memory.Checkpoints.Count == 0);
            writer.FailWrites = false;
            memory.FailCheckpoints = true;
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 1 && memory.Checkpoints.Count == 0);
            memory.FailCheckpoints = false;
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 2 && memory.Checkpoints.Count == 1);
            Require(writer.Writes.All(item => item.SourceVersion == 1 && item.SourceRef == "ref"));
        });
        await CheckAsync("Garnet same-version mutations fail closed, older versions skip and model edits replay", async () =>
        {
            using var modelClient = new HttpClient(new ModelHandler()) { BaseAddress = new Uri("http://model") };
            var writer = new MemoryWriter();
            var database = DispatchProxy.Create<IDatabase, DatabaseProxy>();
            var memory = (DatabaseProxy)(object)database;
            var watcher = Watcher(writer, modelClient);
            var pipeline = Pipeline();
            watcher.UpdatePipelines([pipeline]);
            await watcher.PollOnceAsync(default, database);
            memory.Record = """{"id":"doc","version":1,"content":"changed"}""";
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 1);
            memory.Record = """{"id":"doc","version":2,"content":"new"}""";
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 2);
            memory.Record = """{"id":"doc","version":1,"content":"original"}""";
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 2);
            memory.Record = """{"id":"doc","version":2,"content":"new"}""";
            pipeline.UpdatedAt = "2026-10-06T04:01:00Z";
            watcher.UpdatePipelines([pipeline]);
            await watcher.PollOnceAsync(default, database);
            Require(writer.Writes.Count == 3 && writer.Writes[^1].PipelineRevision > writer.Writes[^2].PipelineRevision);
        });
        await CheckAsync("Garnet cancellation stops before writes and checkpoint advancement", async () =>
        {
            using var modelClient = new HttpClient(new ModelHandler()) { BaseAddress = new Uri("http://model") };
            var writer = new MemoryWriter();
            var database = DispatchProxy.Create<IDatabase, DatabaseProxy>();
            using var cancellation = new CancellationTokenSource();
            cancellation.Cancel();
            try { await Watcher(writer, modelClient).PollOnceAsync(cancellation.Token, database); }
            catch (OperationCanceledException)
            {
                Require(writer.Writes.Count == 0 && ((DatabaseProxy)(object)database).Checkpoints.Count == 0);
                return;
            }
            throw new Exception("Expected cancellation");
        });
        Console.WriteLine($"GARNET SOURCE: {12 - failures} passed, {failures} failed");
        return failures;
    }

    private static Pipeline Pipeline() => new()
    {
        Id = "pipeline", Name = "test", Status = "active", ProcessingMode = "inline", ContentStrategy = "truncate",
        DestinationId = "destination", DocgrokPipeline = "model", UpdatedAt = "2026-10-06T04:00:00Z",
        Sources = [new() { SourceId = "source", ContentFields = ["content"], ContentMode = "field" }]
    };

    private static GarnetSourceWatcher Watcher(IDestinationWriter writer, HttpClient modelClient)
    {
        var source = new Source
        {
            Id = "source", Type = "garnet", Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(
                """{"endpoint":"redis://HOST.test./","hash_key":"documents","tls":false}""")!
        };
        var api = new OmniVecApiClient(new HttpClient(new ModelHandler()) { BaseAddress = new Uri("http://api") },
            NullLogger<OmniVecApiClient>.Instance);
        var watcher = new GarnetSourceWatcher(source, new ChangeFeedOptions { DocGrokBaseUrl = "http://model" },
            api, new ContentHasher(), null!, NullLogger<GarnetSourceWatcher>.Instance,
            writer: writer, embeddingClient: modelClient);
        watcher.UpdatePipelines([Pipeline()]);
        watcher.UpdateDestinations([new()
        {
            Id = "destination", Type = "garnet",
            Config = new() { ["endpoint"] = "host.test:6379", ["tls"] = false, ["vector_set"] = "vectors" }
        }]);
        return watcher;
    }

    public class DatabaseProxy : DispatchProxy
    {
        public string Record = """{"id":"doc","version":1,"content":"original"}""";
        public Dictionary<string, RedisValue> Checkpoints = new();
        public bool FailCheckpoints;

        protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        {
            switch (targetMethod?.Name)
            {
                case "ExecuteAsync":
                    return Task.FromResult(RedisResult.Create(new[]
                    {
                        RedisResult.Create((RedisValue)"0"),
                        RedisResult.Create(new[] { RedisResult.Create((RedisValue)"ref"), RedisResult.Create((RedisValue)Record) })
                    }));
                case "HashGetAsync":
                    return Task.FromResult(Checkpoints.GetValueOrDefault(args![1]!.ToString()!, RedisValue.Null));
                case "HashSetAsync":
                    if (FailCheckpoints) return Task.FromException<bool>(new IOException("checkpoint unavailable"));
                    Checkpoints[args![1]!.ToString()!] = (RedisValue)args[2]!;
                    return Task.FromResult(true);
                default:
                    throw new NotSupportedException(targetMethod?.Name);
            }
        }
    }

    private sealed class MemoryWriter : IDestinationWriter
    {
        public string DestinationType => "garnet";
        public List<EmbeddingResult> Writes = new();
        public List<DeleteRequest> Deletes = new();
        public bool FailWrites;
        public Task WriteBatchAsync(Dictionary<string, object> config, List<EmbeddingResult> results, CancellationToken ct)
        {
            if (FailWrites) throw new IOException("destination unavailable");
            Writes.AddRange(results);
            return Task.CompletedTask;
        }
        public Task DeleteByRefAsync(Dictionary<string, object> config, List<DeleteRequest> requests, CancellationToken ct)
        {
            Deletes.AddRange(requests);
            return Task.CompletedTask;
        }
    }

    private sealed class ModelHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
            => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"outputs":[[0.1,0.2]]}""", Encoding.UTF8, "application/json")
            });
    }
}
