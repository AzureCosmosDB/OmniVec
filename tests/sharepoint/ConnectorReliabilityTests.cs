using System.Net;
using System.Collections.Concurrent;
using System.Reflection;
using System.Text.Json;
using Azure;
using Azure.Messaging.ServiceBus;
using Azure.Messaging.ServiceBus.Administration;
using Azure.Storage.Blobs.Models;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Newtonsoft.Json.Linq;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Hosting;
using OmniVec.ChangeFeed.Services;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Services;
using Message = OmniVec.Worker.Models.EmbeddingMessage;
using Source = OmniVec.ChangeFeed.Models.Source;
using Pipeline = OmniVec.ChangeFeed.Models.Pipeline;
using Destination = OmniVec.ChangeFeed.Models.Destination;

internal static class ConnectorReliabilityTests
{
    private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;
    private static void Check(bool value, string message = "Assertion failed")
    { if (!value) throw new Exception(message); }
    private static async Task Fails(Func<Task> operation)
    {
        try { await operation(); } catch { return; }
        throw new Exception("Expected an error, not successful completion");
    }
    private static void Set(object value, string field, object replacement)
        => value.GetType().GetField(field, Private)!.SetValue(value, replacement);
    private static object? Get(object value, string field) => value.GetType().GetField(field, Private)!.GetValue(value);
    private static Task Invoke(object value, string method, params object[] args)
        => (Task)value.GetType().GetMethod(method, Private)!.Invoke(value, args)!;
    private static Pipeline PipelineFor(string id = "pipeline") => JsonSerializer.Deserialize<Pipeline>(
        $$"""{"id":"{{id}}","sources":[{"source_id":"source","content_fields":["content"]}],"processing_mode":"queue","destination_id":"dest","docgrok_pipeline":"mdl-test"}""")!;
    private static Source TestSource(string type = "postgres") => new()
    {
        Id = "source", Type = type,
        Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(
            """{"workspace_url":"https://local.invalid","http_path":"/sql/1.0/warehouses/test","file_type":"pdf"}""")!,
    };
    private static Source PhysicalSource(string id, string type, string config) => new()
    { Id = id, Type = type, Config = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(config)! };
    private static Pipeline InlinePipeline(string id, params string[] sources) => new()
    {
        Id = id, Status = "active", ProcessingMode = "inline",
        Sources = sources.Select(source => new OmniVec.ChangeFeed.Models.PipelineSource { SourceId = source }).ToList(),
    };
    private static ServiceBusPublisher Publisher(LocalSender sender)
    {
        var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()), NullLogger<ServiceBusPublisher>.Instance);
        Set(publisher, "_sender", sender);
        Set(publisher, "_enabled", true);
        return publisher;
    }
    private static DocGrokClient Client(HttpMessageHandler handler)
        => new(new HttpClient(handler) { BaseAddress = new("http://local.invalid") }, NullLogger<DocGrokClient>.Instance)
        { DelayAsync = (_, _) => Task.CompletedTask };
    private static EmbeddingWorkerService Worker(DocGrokClient client, ReliabilityWriter? writer = null,
        SharePointContentClient? sharePoint = null, MetricsReporter? metrics = null,
        ILogger<EmbeddingWorkerService>? logger = null)
        => new(Options.Create(new WorkerOptions { MaxConcurrentCalls = 2 }), null, client,
            sharePoint ?? new SharePointContentClient(new HttpClient()),
            metrics ?? new MetricsReporter(new HttpClient(new LocalHttpHandler(_ => new(HttpStatusCode.OK)))
                { BaseAddress = new("http://local.invalid") }, NullLogger<MetricsReporter>.Instance),
            [writer ?? new()], logger ?? NullLogger<EmbeddingWorkerService>.Instance);
    private static Message TextMessage(string pipeline = "pipeline", string model = "mdl-test") => new()
    {
        PipelineId = pipeline, DestinationId = "dest", DestinationType = "cosmosdb-vector",
        SourceId = "source", SourceRef = "doc", PartitionKeyValue = "doc", Content = "hello world",
        DocgrokPipeline = model,
    };
    private static ServiceBusReceivedMessage Delivery(Message message, string id = "message")
        => ServiceBusModelFactory.ServiceBusReceivedMessage(body: BinaryData.FromString(JsonSerializer.Serialize(message)),
            messageId: id, lockedUntil: DateTimeOffset.UtcNow.AddSeconds(1));

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string, Func<Task>)>();
        void Test(string name, Func<Task> action) => tests.Add((name, action));
        foreach (var status in new[] { 429, 500, 503 })
            Test($"DocGrok {status} exhausts exactly four attempts on all endpoint paths", async () =>
            {
                foreach (var path in new[] { "text", "blob", "bulk", "data" })
                {
                    var calls = 0;
                    var client = Client(new LocalHttpHandler(_ =>
                    {
                        calls++;
                        return new((HttpStatusCode)status) { Content = new StringContent("unavailable") };
                    }));
                    await Fails(() => path switch
                    {
                        "text" => client.EmbedBatchAsync("mdl-test", ["hello"], default),
                        "blob" => client.EmbedBlobAsync("mdl-test", "https://local.invalid", null, "files", "a.pdf", default),
                        "bulk" => client.EmbedBlobBatchAsync("mdl-test", "https://local.invalid", "files", ["a.pdf"], default),
                        _ => client.EmbedDataAsync("mdl-test", [1], "a.txt", default),
                    });
                    Check(calls == 4, $"{path}: {calls} attempts");
                }
            });
        Test("Transport failure retries are bounded and cancellation interrupts retry delay", async () =>
        {
            var calls = 0;
            var client = Client(new LocalHttpHandler(_ => { calls++; throw new HttpRequestException("offline"); }));
            await Fails(() => client.EmbedBatchAsync("mdl-test", ["text"], default));
            Check(calls == 4);
            using var cancellation = new CancellationTokenSource();
            client.DelayAsync = (_, ct) => { cancellation.Cancel(); return Task.Delay(1, ct); };
            calls = 0;
            await Fails(() => client.EmbedBatchAsync("mdl-test", ["text"], cancellation.Token));
            Check(calls == 1);
        });
        Test("Permanent input errors do not enter HTTP retry loops", async () =>
        {
            var calls = 0;
            var client = Client(new LocalHttpHandler(_ => { calls++; return new(HttpStatusCode.BadRequest); }));
            await Fails(() => client.EmbedBatchAsync("mdl-test", ["text"], default));
            Check(calls == 1);
        });
        Test("Transient outage is abandoned without mass bisect, truncation or dead-letter", async () =>
        {
            var calls = 0;
            var client = Client(new LocalHttpHandler(_ => { calls++; return new(HttpStatusCode.ServiceUnavailable); }));
            using var worker = Worker(client);
            var receiver = new ReliabilityReceiver();
            var first = TextMessage();
            var second = TextMessage();
            var batch = new List<(Message, ServiceBusReceivedMessage)> { (first, Delivery(first)), (second, Delivery(second)) };
            await Invoke(worker, "ProcessBatchAsync", receiver, batch, CancellationToken.None);
            Check(calls == 4 && receiver.Abandoned == 2 && receiver.DeadLettered == 0 && receiver.Completed == 0);
            Check(first.Content == "hello world" && second.Content == "hello world");
        });
        Test("Unknown destination and unsupported delete cannot be acknowledged", async () =>
        {
            using var worker = Worker(Client(new LocalHttpHandler(_ => throw new Exception("Must not embed"))));
            var receiver = new ReliabilityReceiver();
            var unknown = TextMessage();
            unknown.DestinationType = "missing";
            await Invoke(worker, "ProcessReceivedBatchAsync", receiver, new[] { Delivery(unknown) }, CancellationToken.None);
            var deletion = TextMessage();
            deletion.MessageType = "delete";
            await Invoke(worker, "ProcessReceivedBatchAsync", receiver, new[] { Delivery(deletion) }, CancellationToken.None);
            Check(receiver.Completed == 0 && receiver.DeadLettered == 2);
        });
        Test("A failed destination write abandons rather than acknowledging", async () =>
        {
            using var worker = Worker(Client(new LocalHttpHandler(_ =>
                new(HttpStatusCode.OK) { Content = new StringContent("""{"outputs":[[0.1,0.2]]}""") })),
                new ReliabilityWriter { Fail = true });
            var receiver = new ReliabilityReceiver();
            var message = TextMessage();
            await Invoke(worker, "ProcessBatchAsync", receiver,
                new List<(Message, ServiceBusReceivedMessage)> { (message, Delivery(message)) }, CancellationToken.None);
            Check(receiver.Completed == 0 && receiver.Abandoned == 1);
        });
        Test("Confirmed poison dead-letter reports failure with a stable key, never indexed success", async () =>
        {
            var reports = new List<JsonElement>();
            var metrics = new MetricsReporter(new HttpClient(new AsyncHandler(async (request, ct) =>
            {
                using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                reports.Add(body.RootElement.Clone());
                return new(HttpStatusCode.OK);
            })) { BaseAddress = new("http://local.invalid") }, NullLogger<MetricsReporter>.Instance);
            using var worker = Worker(Client(new LocalHttpHandler(_ => new(HttpStatusCode.BadRequest))), metrics: metrics);
            var receiver = new ReliabilityReceiver();
            var message = TextMessage();
            await Invoke(worker, "ProcessBatchAsync", receiver,
                new List<(Message, ServiceBusReceivedMessage)> { (message, Delivery(message, "poison-1")) }, CancellationToken.None);
            Check(receiver.DeadLettered == 1 && receiver.Completed == 0 && reports.Count == 1);
            Check(reports[0].GetProperty("processed").GetInt32() == 0);
            Check(reports[0].GetProperty("failed").GetInt32() == 1);
            Check(reports[0].GetProperty("batch_key").GetString() == "worker-dlq:poison-1");
        });
        Test("Failed dead-letter settlement does not claim terminal-failure metrics", async () =>
        {
            var reports = 0;
            var metrics = new MetricsReporter(new HttpClient(new LocalHttpHandler(_ =>
            { reports++; return new(HttpStatusCode.OK); })) { BaseAddress = new("http://local.invalid") },
                NullLogger<MetricsReporter>.Instance);
            using var worker = Worker(Client(new LocalHttpHandler(_ => throw new Exception("Must not embed"))), metrics: metrics);
            var receiver = new ReliabilityReceiver { FailDeadLetter = true };
            await Fails(() => Invoke(worker, "DeadLetterWithMetricsAsync", receiver, Delivery(TextMessage()),
                "Poison", "bad input", CancellationToken.None));
            Check(reports == 0 && receiver.DeadLettered == 0);
        });
        Test("Rejected metrics HTTP responses are logged instead of silently treated as delivered", async () =>
        {
            var logger = new MetricsLogger();
            var metrics = new MetricsReporter(new HttpClient(new LocalHttpHandler(_ => new(HttpStatusCode.Unauthorized)))
                { BaseAddress = new("http://local.invalid") }, logger);
            await metrics.ReportInlineMetricsAsync("pipeline", 0, 1, 0, "failure");
            Check(logger.Warnings == 1);
        });
        Test("HTTP timeout does not permanently terminate the receive loop", async () =>
        {
            var calls = 0;
            using var worker = Worker(Client(new LocalHttpHandler(_ =>
            { calls++; throw new TaskCanceledException("HTTP client timeout"); })));
            using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var receiver = new LoopReceiver(cancellation, Delivery(TextMessage()));
            await Invoke(worker, "ReceiveLoopAsync", receiver, cancellation.Token);
            Check(calls == 4 && receiver.Receives == 2 && receiver.Abandoned == 1 && receiver.Completed == 0);
        });
        Test("Local receive-loop end-to-end: permanent and timeout failures emit telemetry while healthy queue work progresses", async () =>
        {
            var timeoutCalls = 0;
            var client = Client(new AsyncHandler(async (request, ct) =>
            {
                using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                var model = body.RootElement.GetProperty("model_id").GetString();
                if (model == "mdl-permanent")
                    return new(HttpStatusCode.BadRequest) { Content = new StringContent("Invalid input") };
                if (model == "mdl-timeout")
                {
                    Interlocked.Increment(ref timeoutCalls);
                    throw new TaskCanceledException("Simulated HTTP timeout");
                }
                return new(HttpStatusCode.OK) { Content = new StringContent("""{"outputs":[[0.25,0.5]]}""") };
            }));
            var telemetry = new ConcurrentQueue<(string Path, JsonElement Body)>();
            var healthyReports = 0;
            var metricsComplete = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var metrics = new MetricsReporter(new HttpClient(new AsyncHandler(async (request, ct) =>
            {
                using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                var path = request.RequestUri!.AbsolutePath;
                telemetry.Enqueue((path, body.RootElement.Clone()));
                if (path.Contains("/healthy/") && Interlocked.Increment(ref healthyReports) == 2)
                    metricsComplete.TrySetResult();
                return new(HttpStatusCode.OK);
            })) { BaseAddress = new("http://local.invalid") }, NullLogger<MetricsReporter>.Instance);
            var logs = new WorkerLogger();
            var writer = new ReliabilityWriter();
            using var worker = Worker(client, writer, metrics: metrics, logger: logs);
            var healthy = TextMessage("healthy", "mdl-healthy");
            healthy.SourceRef = "healthy-inline";
            var later = TextMessage("healthy", "mdl-healthy");
            later.SourceRef = "healthy-after";
            using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var receiver = new ScriptedReceiver(cancellation,
            [
                [
                    Delivery(TextMessage("permanent", "mdl-permanent"), "permanent"),
                    Delivery(TextMessage("timeout", "mdl-timeout"), "timeout"),
                    Delivery(healthy, "healthy-inline"),
                ],
                [Delivery(later, "healthy-after")],
            ]);

            await Invoke(worker, "ReceiveLoopAsync", receiver, cancellation.Token);
            await metricsComplete.Task.WaitAsync(TimeSpan.FromSeconds(2));
            Check(receiver.Receives == 3 && timeoutCalls == 4);
            Check(receiver.Completed == 2 && receiver.CompletedIds.Contains("healthy-after"));
            Check(receiver.DeadLettered == 1 && receiver.DeadLetterIds.Single() == "permanent");
            Check(receiver.Abandoned == 1 && receiver.AbandonedIds.Single() == "timeout");
            Check(writer.Written.Count == 2 && writer.Written.All(result => result.PipelineId == "healthy"));
            var failed = telemetry.Single(record => record.Path.Contains("/permanent/")).Body;
            Check(failed.GetProperty("processed").GetInt32() == 0 && failed.GetProperty("failed").GetInt32() == 1);
            Check(failed.GetProperty("batch_key").GetString() == "worker-dlq:permanent");
            Check(telemetry.Where(record => record.Path.Contains("/healthy/"))
                .Sum(record => record.Body.GetProperty("processed").GetInt32()) == 2);
            Check(logs.Entries.Any(entry => entry.Level == LogLevel.Error
                && entry.Error is TimeoutException && entry.Message.Contains("mdl-timeout")));
            Check(!telemetry.Any(record => record.Path.Contains("/timeout/")),
                "A retryable timeout must not claim indexed success or a terminal DLQ failure");
        });
        Test("Unrelated pipelines process while one model request is blocked", async () =>
        {
            var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var client = Client(new AsyncHandler(async (request, ct) =>
            {
                var body = await request.Content!.ReadAsStringAsync(ct);
                if (body.Contains("mdl-slow")) await release.Task.WaitAsync(ct);
                return new(HttpStatusCode.OK) { Content = new StringContent("""{"outputs":[[0.1]]}""") };
            }));
            using var worker = Worker(client);
            var receiver = new ReliabilityReceiver();
            using var budget = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var processing = Invoke(worker, "ProcessReceivedBatchAsync", receiver,
                new[] { Delivery(TextMessage("slow", "mdl-slow"), "slow"), Delivery(TextMessage("fast", "mdl-fast"), "fast") },
                budget.Token);
            try { await receiver.FastCompleted.Task.WaitAsync(TimeSpan.FromSeconds(2)); }
            finally { release.TrySetResult(); }
            await processing;
            Check(receiver.Completed == 2);
        });
        Test("Received locks renew and settlement removes renewal tracking", async () =>
        {
            var receiver = new ReliabilityReceiver();
            var message = Delivery(TextMessage());
            await using (var scope = new MessageProcessingScope(receiver, [message],
                TimeSpan.FromSeconds(2), default, TimeSpan.FromMilliseconds(5)))
            {
                await receiver.LockRenewed.Task.WaitAsync(TimeSpan.FromSeconds(2));
                Check(receiver.Renewed > 0);
                await scope.Receiver.CompleteMessageAsync(message);
                // An already-started renewal may finish after settlement; it must not re-add tracking.
                var pending = (ConcurrentDictionary<ServiceBusReceivedMessage, DateTimeOffset>)Get(scope, "_pending")!;
                Check(!pending.ContainsKey(message));
            }
            Check(receiver.Abandoned == 0 && receiver.Completed == 1);
        });
        Test("Buffered SharePoint files have a separate global memory concurrency cap", async () =>
        {
            var active = 0;
            var maximum = 0;
            var graph = new SharePointContentClient(new HttpClient(new AsyncHandler(async (request, ct) =>
            {
                if (!request.RequestUri!.AbsolutePath.EndsWith("/content"))
                    return new(HttpStatusCode.OK) { Content = new StringContent("""{"eTag":"v1"}""") };
                var concurrent = Interlocked.Increment(ref active);
                maximum = Math.Max(maximum, concurrent);
                try
                {
                    await Task.Delay(30, ct);
                    return new(HttpStatusCode.OK) { Content = new ByteArrayContent([65]) };
                }
                finally { Interlocked.Decrement(ref active); }
            })));
            Set(graph, "_credential", new LocalCredential());
            using var worker = Worker(Client(new LocalHttpHandler(_ =>
                new(HttpStatusCode.OK) { Content = new StringContent("""{"chunks":[{"text":"A","embedding":[0.1]}]}""") })),
                sharePoint: graph);
            Message SharePoint(string id)
            {
                var message = TextMessage(id);
                message.ContentType = "sharepoint_ref";
                message.SharePointSiteId = "site";
                message.SharePointDriveId = "drive";
                message.SharePointItemId = id;
                message.SharePointETag = "v1";
                message.SharePointRevision = 1;
                message.SharePointFileName = "a.txt";
                return message;
            }
            var receiver = new ReliabilityReceiver();
            await Invoke(worker, "ProcessReceivedBatchAsync", receiver,
                new[] { Delivery(SharePoint("first")), Delivery(SharePoint("second")) }, CancellationToken.None);
            Check(receiver.Completed == 2 && maximum == 1);
        });
        Test("Processing budget cancels work and abandons only unfinished deliveries", async () =>
        {
            var receiver = new ReliabilityReceiver();
            var first = Delivery(TextMessage());
            var second = Delivery(TextMessage());
            await using (var scope = new MessageProcessingScope(receiver, [first, second],
                TimeSpan.FromMilliseconds(40), default, TimeSpan.FromMilliseconds(5)))
            {
                await scope.Receiver.CompleteMessageAsync(first);
                await Task.Delay(80);
                Check(scope.Token.IsCancellationRequested);
            }
            Check(receiver.Completed == 1 && receiver.Abandoned == 1);
        });
        Test("DocGrok timeout allows router ceiling plus grace and is bounded by explicit batch configuration", async () =>
        {
            var options = new WorkerOptions();
            Check(options.GetDocGrokRequestTimeout() == TimeSpan.FromSeconds(330));
            Check(options.GetDocGrokRequestTimeout() > TimeSpan.FromSeconds(300)
                && options.GetDocGrokRequestTimeout() < TimeSpan.FromMinutes(options.MaxProcessingMinutes));
            options.DocGrokRequestTimeoutSeconds = 345;
            Check(options.GetDocGrokRequestTimeout() == TimeSpan.FromSeconds(345));
            options.DocGrokRequestTimeoutSeconds = 1000;
            Check(options.GetDocGrokRequestTimeout() == TimeSpan.FromMinutes(8));
            options.MaxLockRenewalMinutes = 4;
            Check(options.GetDocGrokRequestTimeout() == TimeSpan.FromMinutes(4));
            foreach (var invalid in new[]
            {
                new WorkerOptions { DocGrokRequestTimeoutSeconds = 0 },
                new WorkerOptions { DocGrokRequestTimeoutSeconds = -1 },
                new WorkerOptions { MaxProcessingMinutes = 0 },
                new WorkerOptions { MaxLockRenewalMinutes = -1 },
            })
                await Fails(() => { invalid.GetDocGrokRequestTimeout(); return Task.CompletedTask; });
        });
        Test("Remaining received-batch budget cancels a document HTTP request before its 330-second timeout", async () =>
        {
            var calls = 0;
            using var http = new HttpClient(new AsyncHandler(async (_, ct) =>
            {
                Interlocked.Increment(ref calls);
                await Task.Delay(Timeout.InfiniteTimeSpan, ct);
                return new(HttpStatusCode.OK);
            }))
            {
                BaseAddress = new("http://local.invalid"),
                Timeout = new WorkerOptions().GetDocGrokRequestTimeout(),
            };
            var client = new DocGrokClient(http, NullLogger<DocGrokClient>.Instance);
            var receiver = new ReliabilityReceiver();
            await using (var scope = new MessageProcessingScope(receiver, [Delivery(TextMessage())],
                TimeSpan.FromMilliseconds(500), default))
            {
                Exception? failure = null;
                try { await client.EmbedDataAsync("mdl-test", [1], "document.txt", scope.Token); }
                catch (Exception error) { failure = error; }
                Check(failure is OperationCanceledException && scope.Token.IsCancellationRequested && calls == 1);
            }
            Check(receiver.Abandoned == 1 && receiver.Completed == 0 && receiver.DeadLettered == 0);
        });
        Test("Lost queue ownership cancels processing instead of continuing stale work", async () =>
        {
            var receiver = new ReliabilityReceiver { LoseLock = true };
            await using var scope = new MessageProcessingScope(receiver, [Delivery(TextMessage())],
                TimeSpan.FromSeconds(2), default, TimeSpan.FromMilliseconds(5));
            await Task.Delay(15);
            Check(scope.Token.IsCancellationRequested);
        });
        Test("Deliberately idle worker continues heartbeating instead of failing liveness", async () =>
        {
            using var ct = new CancellationTokenSource();
            var idle = EmbeddingWorkerService.RunIdleAsync(ct.Token, TimeSpan.FromMilliseconds(5));
            var field = typeof(WorkerHeartbeat).GetField("_lastBeatTicks", BindingFlags.Static | BindingFlags.NonPublic)!;
            field.SetValue(null, DateTime.UtcNow.AddMinutes(-10).Ticks);
            await Task.Delay(30);
            Check((long)field.GetValue(null)! > DateTime.UtcNow.AddSeconds(-1).Ticks);
            ct.Cancel();
            await Fails(() => idle);
        });
        Test("All polling sources share ownership leases; Cosmos retains CFP partition leases", () =>
        {
            foreach (var type in new[] { "azure-blob", "databricks", "sharepoint", "mssql", "postgresql" })
                Check(SourceWatcherManager.RequiresPollingLease(type));
            Check(!SourceWatcherManager.RequiresPollingLease("cosmosdb"));
            return Task.CompletedTask;
        });
        Test("Inline SQL/Postgres/Cosmos HTTP failures are bounded and malformed output is rejected", async () =>
        {
            foreach (var status in new[] { HttpStatusCode.BadRequest, HttpStatusCode.ServiceUnavailable })
            {
                var calls = 0;
                using var client = new HttpClient(new LocalHttpHandler(_ => { calls++; return new(status); }))
                    { BaseAddress = new("http://local.invalid") };
                await Fails(() => InlineEmbeddingClient.EmbedAsync(client, "mdl-test", ["hello"], default,
                    (_, _) => Task.CompletedTask));
                Check(calls == (status == HttpStatusCode.BadRequest ? 1 : 4));
            }
            using var malformed = new HttpClient(new LocalHttpHandler(_ =>
                new(HttpStatusCode.OK) { Content = new StringContent("""{"outputs":[]}""") }))
                { BaseAddress = new("http://local.invalid") };
            await Fails(() => InlineEmbeddingClient.EmbedAsync(malformed, "mdl-test", ["hello"], default));
        });
        Test("Exhausted inline HTTP timeout is a failure, not watcher shutdown cancellation", async () =>
        {
            var calls = 0;
            using var client = new HttpClient(new LocalHttpHandler(_ =>
            { calls++; throw new TaskCanceledException("HTTP timeout"); })) { BaseAddress = new("http://local.invalid") };
            Exception? failure = null;
            try { await InlineEmbeddingClient.EmbedAsync(client, "mdl-test", ["hello"], default, (_, _) => Task.CompletedTask); }
            catch (Exception error) { failure = error; }
            Check(calls == 4 && failure is TimeoutException);
        });
        foreach (var (type, config, aliasConfig) in new[]
        {
            ("cosmosdb",
                """{"endpoint":"https://ACCOUNT.local.invalid:443/","database":"data","container":"docs"}""",
                """{"endpoint":"https://account.local.invalid","database":"data","container":"docs"}"""),
            ("postgresql",
                """{"host":"DB.local.invalid","port":5432,"database":"data","table":"docs","user":"test"}""",
                """{"host":"wrong.local.invalid","database":"wrong","table":"docs","schema":"public","connection_string":"Host=db.local.invalid;Port=5432;Database=data;Username=other"}"""),
            ("mssql",
                """{"host":"DB.local.invalid","port":1433,"database":"data","table":"docs"}""",
                """{"host":"wrong.local.invalid","database":"wrong","table":"docs","schema_name":"dbo","connection_string":"Server=tcp:db.local.invalid,1433;Initial Catalog=data;User ID=other"}"""),
        })
            Test($"{type} inline ownership rejects physical aliases even with different vector fields", () =>
            {
                var source = PhysicalSource("source", type, config);
                var alias = PhysicalSource("alias", type, aliasConfig);
                var unrelated = PhysicalSource("unrelated", type, config);
                unrelated.Config[type == "cosmosdb" ? "container" : "table"] = JsonSerializer.SerializeToElement("other");
                var first = InlinePipeline("one", source.Id);
                var second = InlinePipeline("two", alias.Id);
                second.VectorIndexPath = "different_embedding";
                var pipelines = new List<Pipeline> { first, second, InlinePipeline("three", unrelated.Id) };
                var sources = new List<Source> { source, alias, unrelated };
                Check(InlineSourceOwnership.TargetKey(source) == InlineSourceOwnership.TargetKey(alias));
                Check(InlineSourceOwnership.FindBlockedSourceIds(sources, pipelines).SetEquals(["source", "alias"]));
                second.Status = "paused";
                Check(InlineSourceOwnership.FindBlockedSourceIds(sources, pipelines).Count == 0);
                second.Status = "active";
                second.ProcessingMode = "queue";
                Check(InlineSourceOwnership.FindBlockedSourceIds(sources, pipelines).Count == 0);
                Check(InlineSourceOwnership.FindBlockedSourceIds(sources, [InlinePipeline("one", "source", "source")]).Count == 0);
                Check(InlineSourceOwnership.FindBlockedSourceIds(sources, [InlinePipeline("one", "source", "alias")])
                    .SetEquals(["source", "alias"]));
                return Task.CompletedTask;
            });
        Test("Inline target matching honors numeric ports and invalid targets fail closed without blocking others", () =>
        {
            var source = PhysicalSource("source", "postgresql",
                """{"host":"db.local.invalid","port":6432,"database":"data","table":"docs","user":"test"}""");
            Check(new Npgsql.NpgsqlConnectionStringBuilder(source.ConnectionString).Port == 6432);
            var sql = PhysicalSource("sql", "mssql",
                """{"host":"db.local.invalid","port":1434,"database":"data","table":"docs"}""");
            Check(new Microsoft.Data.SqlClient.SqlConnectionStringBuilder(sql.ConnectionString).DataSource.EndsWith(",1434"));
            var bad = PhysicalSource("bad", "postgresql", """{"connection_string":"not valid","table":"docs"}""");
            var badCosmos = PhysicalSource("bad-cosmos", "cosmosdb", """{"endpoint":"not a URL","database":"data","container":"docs"}""");
            Check(InlineSourceOwnership.FindBlockedSourceIds([source, sql, bad, badCosmos],
                [InlinePipeline("one", "source"), InlinePipeline("two", "sql"),
                    InlinePipeline("bad", "bad"), InlinePipeline("bad-cosmos", "bad-cosmos")])
                .SetEquals(["bad", "bad-cosmos"]));
            return Task.CompletedTask;
        });
        Test("Reconciliation and reset refuse existing inline conflicts while unrelated watchers remain running", async () =>
        {
            const string config = """{"endpoint":"https://account.local.invalid","database":"data","container":"docs"}""";
            var sources = new List<Source>
            {
                PhysicalSource("source", "cosmosdb", config), PhysicalSource("alias", "cosmosdb", config),
                PhysicalSource("healthy", "cosmosdb", config.Replace("\"docs\"", "\"other\"")),
            };
            var pipelines = new List<Pipeline>
                { InlinePipeline("one", "source"), InlinePipeline("two", "alias"), InlinePipeline("three", "healthy") };
            var log = new ManagerLogger();
            await using var manager = new SourceWatcherManager(
                Options.Create(new ChangeFeedOptions { EnableCosmosSources = true }), null!, null!, null!,
                new ContentHasher(), null!, NullLoggerFactory.Instance, log);
            var watchers = (ConcurrentDictionary<string, ISourceWatcher>)Get(manager, "_watchers")!;
            var first = new RecordingWatcher("source");
            var second = new RecordingWatcher("alias");
            var healthy = new RecordingWatcher("healthy");
            watchers["source"] = first;
            watchers["alias"] = second;
            watchers["healthy"] = healthy;
            await manager.ReconcileAsync(sources, pipelines, default);
            Check(first.Disposals == 1 && second.Disposals == 1 && healthy.Disposals == 0);
            Check(manager.ActiveWatcherCount == 1 && healthy.PipelineUpdates == 1);
            await manager.ResetWatcherAsync("source", sources[0], pipelines, default, sources);
            Check(manager.ActiveWatcherCount == 1 && !log.Messages.Any(message => message.Contains("Resetting watcher")));
            Check(log.Messages.Count(message => message.StartsWith("Refusing source")) == 4);
        });
        Test("Inline ownership refusal preserves pending reset intent until the conflict is resolved", () =>
        {
            using var discovery = new SourceDiscoveryService(null!, null!, Options.Create(new ChangeFeedOptions()),
                NullLogger<SourceDiscoveryService>.Instance);
            var detect = typeof(SourceDiscoveryService).GetMethod("DetectResets", Private)!;
            var pipeline = InlinePipeline("pipeline", "source");
            pipeline.ResetAt = "2026-01-01T00:00:00Z";
            var pipelines = new List<Pipeline> { pipeline };
            HashSet<string> Detect(HashSet<string> blocked)
                => (HashSet<string>)detect.Invoke(discovery, [pipelines, blocked])!;
            Check(Detect([]).Count == 0);
            pipeline.ResetAt = "2026-02-01T00:00:00Z";
            Check(Detect(["source"]).Count == 0);
            Check(Detect([]).SetEquals(["source"]));
            Check(Detect([]).Count == 0);
            return Task.CompletedTask;
        });
        Test("Cosmos content changes and different pipelines are not mistaken for patch feedback", () =>
        {
            var pipeline = PipelineFor();
            var document = JObject.Parse("""{"pipeline_id":"pipeline","content_hash":"old","embedded_at":"2026-01-01T00:00:00Z"}""");
            Check(SourceWatcher.HasCurrentEmbedding(document, pipeline, "old"));
            Check(!SourceWatcher.HasCurrentEmbedding(document, pipeline, "new"));
            Check(!SourceWatcher.HasCurrentEmbedding(document, PipelineFor("other"), "old"));
            pipeline.ResetAt = "2026-02-01T00:00:00Z";
            Check(!SourceWatcher.HasCurrentEmbedding(document, pipeline, "old"));
            document.Remove("embedded_at");
            Check(!SourceWatcher.HasCurrentEmbedding(document, pipeline, "old"));
            return Task.CompletedTask;
        });
        Test("Blob publication failure retains page cursor and version markers", async () =>
        {
            var sender = new LocalSender { FailSend = 1 };
            await using var publisher = Publisher(sender);
            await using var watcher = new BlobSourceWatcher(TestSource("azure-blob"), new(), null!, new(),
                NullLogger<BlobSourceWatcher>.Instance, sbPublisher: publisher);
            watcher.UpdatePipelines([PipelineFor()]);
            watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
            Set(watcher, "_continuationToken", "old-page");
            var factory = typeof(BlobsModelFactory).GetMethods().Where(method => method.Name == "BlobItemProperties")
                .OrderBy(method => method.GetParameters().Length).First();
            var properties = (BlobItemProperties)factory.Invoke(null, factory.GetParameters().Select(parameter =>
                parameter.Name == "eTag" ? (object)new ETag("version")
                    : parameter.HasDefaultValue ? parameter.DefaultValue
                    : parameter.ParameterType.IsValueType ? Activator.CreateInstance(parameter.ParameterType) : null).ToArray())!;
            var blobs = new List<BlobItem> { BlobsModelFactory.BlobItem("a.pdf", properties: properties) };
            await Fails(() => watcher.PublishPageAsync(blobs, "next-page", default));
            Check((string?)Get(watcher, "_continuationToken") == "old-page");
            Check(((HashSet<string>)Get(watcher, "_processedBlobs")!).Count == 0);
            sender.FailSend = 0;
            await watcher.PublishPageAsync(blobs, "next-page", default);
            Check((string?)Get(watcher, "_continuationToken") == "next-page");
            Check(((HashSet<string>)Get(watcher, "_processedBlobs")!).Count == 1);
            Check(sender.AttemptedIds[0] == sender.AttemptedIds[1]);
        });
        Test("Postgres cursor advances only after publication; inline metadata cannot suppress queue replay", async () =>
        {
            var sender = new LocalSender { FailSend = 1 };
            await using var publisher = Publisher(sender);
            await using var watcher = new PostgresCdcWatcher(TestSource(), new(), null!, new(),
                NullLogger<PostgresCdcWatcher>.Instance, sbPublisher: publisher);
            var inline = PipelineFor("inline");
            inline.ProcessingMode = "inline";
            watcher.UpdatePipelines([inline, PipelineFor()]);
            watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
            var rows = new List<Dictionary<string, object?>> {
                new() { ["id"] = "id-20", ["content"] = "hello", ["content_hash"] = new ContentHasher().ComputeHash("hello"),
                    ["embedded_at"] = DateTime.UtcNow, ["pipeline_id"] = "inline" }
            };
            var next = DateTime.UtcNow;
            await Fails(() => watcher.ProcessPageAsync(rows, "id", next, "id-20", default));
            Check((DateTime)Get(watcher, "_lastCheckpoint")! == DateTime.MinValue);
            Check(Get(watcher, "_lastPkValue") is null);
            sender.FailSend = 0;
            await watcher.ProcessPageAsync(rows, "id", next, "id-20", default);
            Check((DateTime)Get(watcher, "_lastCheckpoint")! == next);
        });
        foreach (var nullable in new[] { true, false })
            Test($"Postgres startup detection and polling handle nullable tracking={nullable} without losing a page", async () =>
            {
                using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                var sender = new LocalSender { Capacity = 10, FailSend = 2 };
                await using var publisher = Publisher(sender);
                await using var watcher = new PostgresCdcWatcher(
                    PhysicalSource("source", "postgresql", """{"table":"docs"}"""),
                    new ChangeFeedOptions { MaxItemsPerBatch = 1, FeedPollIntervalSeconds = 0, ErrorBackoffSeconds = 0 },
                    null!, new(), NullLogger<PostgresCdcWatcher>.Instance, sbPublisher: publisher);
                watcher.UpdatePipelines([PipelineFor()]);
                watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
                watcher.OpenConnectionAsync = _ => Task.FromResult(new Npgsql.NpgsqlConnection());
                var detectionCalls = 0;
                watcher.ExecuteScalarAsync = (command, _) =>
                {
                    detectionCalls++;
                    Check(command.CommandText.Contains("information_schema.columns")
                        && command.CommandText.Contains("is_nullable = 'NO'")
                        && command.CommandText.Contains("timestamp with time zone"));
                    Check(command.Parameters["@table"].Value?.ToString() == "docs");
                    return Task.FromResult<object?>(nullable ? null : "updated_at");
                };
                var queries = new List<string>();
                var cursors = new List<string?>();
                watcher.ReadPageAsync = (command, _) =>
                {
                    queries.Add(command.CommandText);
                    var cursor = command.Parameters.Contains("@lastPk")
                        ? command.Parameters["@lastPk"].Value as string : null;
                    cursors.Add(cursor);
                    if (queries.Count == 4)
                    {
                        cancellation.Cancel();
                        return Task.FromResult(new List<Dictionary<string, object?>>());
                    }
                    if (nullable)
                        Check(command.CommandText.Contains("WHERE (@lastPk IS NULL")
                            && command.CommandText.Contains("ORDER BY \"id\"::text")
                            && !command.CommandText.Contains("\"updated_at\""));
                    else
                        Check(command.CommandText.Contains("ORDER BY \"updated_at\", \"id\"::text"));
                    var id = cursor is null ? "1" : "2";
                    return Task.FromResult(new List<Dictionary<string, object?>>
                    {
                        new() { ["id"] = id, ["content"] = "row " + id,
                            ["updated_at"] = nullable ? null : DateTime.Parse("2026-01-01T00:00:00Z").ToUniversalTime() },
                    });
                };
                await watcher.StartAsync(cancellation.Token);
                try { await ((Task)Get(watcher, "_pollTask")!).WaitAsync(TimeSpan.FromSeconds(5)); }
                catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { }
                Check(detectionCalls == 1 && queries.Count == 4);
                Check(cursors.Take(3).SequenceEqual(new string?[] { null, "1", "1" }));
                Check(sender.Sent.Count == 2);
                Check(sender.Sent.Select(message =>
                    JsonSerializer.Deserialize<OmniVec.ChangeFeed.Models.EmbeddingMessage>(message.Body)!.SourceRef)
                    .SequenceEqual(["1", "2"]));
            });
        Test("SQL full scan supports native GUID and negative keys without advancing failed pages", async () =>
        {
            var sender = new LocalSender { FailSend = 1 };
            await using var publisher = Publisher(sender);
            await using var watcher = new MsSqlCdcWatcher(TestSource("mssql"), new(), null!, new(),
                NullLogger<MsSqlCdcWatcher>.Instance, sbPublisher: publisher);
            watcher.UpdatePipelines([PipelineFor()]);
            watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
            var id = Guid.NewGuid();
            var rows = new List<Dictionary<string, object?>> { new() { ["id"] = id, ["content"] = "hello" } };
            await Fails(() => watcher.ProcessFullScanPageAsync(rows, "id", default));
            Check(Get(watcher, "_lastFullScanPk") is null);
            sender.FailSend = 0;
            await watcher.ProcessFullScanPageAsync(rows, "id", default);
            Check(Equals(Get(watcher, "_lastFullScanPk"), id));
            rows[0]["id"] = -7L;
            await watcher.ProcessFullScanPageAsync(rows, "id", default);
            Check(Equals(Get(watcher, "_lastFullScanPk"), -7L));
        });
        Test("Databricks reads all result chunks and rejects truncated/incomplete results", async () =>
        {
            await using var watcher = new DatabricksCdcWatcher(TestSource("databricks"), new(), null!, new(),
                NullLogger<DatabricksCdcWatcher>.Instance);
            Set(watcher, "_credential", new LocalCredential());
            var truncated = false;
            var chunkCalls = 0;
            using var http = new HttpClient(new LocalHttpHandler(request =>
            {
                if (request.Method == HttpMethod.Post)
                    return new(HttpStatusCode.OK) { Content = new StringContent($$$"""
                        {"statement_id":"s1","status":{"state":"SUCCEEDED"},
                         "manifest":{"truncated":{{{truncated.ToString().ToLowerInvariant()}}},"total_row_count":2,
                         "schema":{"columns":[{"name":"id","type_name":"STRING"}]}},
                         "result":{"data_array":[["a"]],"next_chunk_internal_link":"/api/2.0/sql/statements/s1/result/chunks/1"}}
                        """) };
                chunkCalls++;
                return new(HttpStatusCode.OK) { Content = new StringContent("""{"data_array":[["b"]]}""") };
            })) { BaseAddress = new("http://local.invalid") };
            Set(watcher, "_http", http);
            var call = Invoke(watcher, "ExecuteStatementAsync", "SELECT test", CancellationToken.None);
            await call;
            var rows = (List<Dictionary<string, object?>>)call.GetType().GetProperty("Result")!.GetValue(call)!;
            Check(rows.Count == 2 && chunkCalls == 1);
            truncated = true;
            await Fails(() => Invoke(watcher, "ExecuteStatementAsync", "SELECT test", CancellationToken.None));
            Check(chunkCalls == 1);
        });
        foreach (var snapshot in new[] { true, false })
            Test($"Databricks {(snapshot ? "snapshot" : "commit")} over 25 MiB streams external results before advancing version", async () =>
            {
                const int rowsPerChunk = 210;
                var text = new string('x', 65536);
                var chunks = Enumerable.Range(0, 2).Select(chunk => JsonSerializer.Serialize(
                    Enumerable.Range(chunk * rowsPerChunk, rowsPerChunk)
                        .Select(index => new[] { index.ToString(), text }))).ToArray();
                Check(chunks.Sum(chunk => (long)System.Text.Encoding.UTF8.GetByteCount(chunk)) > 25L * 1024 * 1024);
                var sender = new LocalSender { Capacity = 100 };
                await using var publisher = Publisher(sender);
                await using var watcher = new DatabricksCdcWatcher(TestSource("databricks"),
                    new ChangeFeedOptions { MaxItemsPerBatch = 50 }, null!, new(),
                    NullLogger<DatabricksCdcWatcher>.Instance, sbPublisher: publisher);
                watcher.UpdatePipelines([PipelineFor()]);
                watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
                Set(watcher, "_credential", new LocalCredential());
                var initialVersion = snapshot ? -1L : 41L;
                Set(watcher, "_lastVersion", initialVersion);
                var sawPublicationBeforeContinuation = false;
                var requests = 0;
                using var workspace = new HttpClient(new AsyncHandler(async (request, ct) =>
                {
                    Check(request.Headers.Authorization is not null);
                    Check((long)Get(watcher, "_lastVersion")! == initialVersion);
                    if (request.Method == HttpMethod.Post)
                    {
                        requests++;
                        using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                        Check(body.RootElement.GetProperty("disposition").GetString() == "EXTERNAL_LINKS");
                        var sql = body.RootElement.GetProperty("statement").GetString()!;
                        Check(snapshot ? sql.Contains("VERSION AS OF 42") : sql.Contains("table_changes('catalog.schema.docs', 42, 42)"));
                        return new(HttpStatusCode.OK) { Content = new StringContent(ExternalManifest(rowsPerChunk * 2, rowsPerChunk)) };
                    }
                    sawPublicationBeforeContinuation = sender.Sent.Count == rowsPerChunk;
                    return new(HttpStatusCode.OK) { Content = new StringContent(ExternalChunk(1, rowsPerChunk, rowsPerChunk)) };
                })) { BaseAddress = new("https://workspace.local.invalid") };
                var downloads = 0;
                using var external = new HttpClient(new LocalHttpHandler(request =>
                {
                    Check(request.Headers.Authorization is null && !request.Headers.Contains("Authorization"));
                    Check(request.RequestUri!.Host == "result.local.invalid");
                    return new(HttpStatusCode.OK) { Content = new StringContent(chunks[downloads++]) };
                }));
                Set(watcher, "_http", workspace);
                Set(watcher, "_externalHttp", external);
                await watcher.ProcessVersionAsync("catalog.schema.docs", 42, snapshot, default);
                Check(requests == 1 && downloads == 2 && sawPublicationBeforeContinuation);
                Check(sender.Sent.Count == rowsPerChunk * 2 && (long)Get(watcher, "_lastVersion")! == 42L);
            });
        foreach (var failure in new[] { "publish", "download", "row-count" })
            Test($"Databricks external {failure} failure retains version and complete replay succeeds", async () =>
            {
                var sender = new LocalSender { Capacity = 10, FailSend = failure == "publish" ? 2 : 0 };
                await using var publisher = Publisher(sender);
                await using var watcher = new DatabricksCdcWatcher(TestSource("databricks"),
                    new ChangeFeedOptions { MaxItemsPerBatch = 2 }, null!, new(),
                    NullLogger<DatabricksCdcWatcher>.Instance, sbPublisher: publisher);
                watcher.UpdatePipelines([PipelineFor()]);
                watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
                Set(watcher, "_credential", new LocalCredential());
                var fail = true;
                var historyCalls = 0;
                using var workspace = new HttpClient(new AsyncHandler(async (request, ct) =>
                {
                    if (request.Method == HttpMethod.Post)
                    {
                        using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                        if (body.RootElement.GetProperty("statement").GetString()!.StartsWith("DESCRIBE HISTORY"))
                        {
                            historyCalls++;
                            return new(HttpStatusCode.OK) { Content = new StringContent("""
                                {"statement_id":"history","status":{"state":"SUCCEEDED"},
                                 "manifest":{"total_row_count":1,"schema":{"columns":[{"name":"version","type_name":"LONG"}]}},
                                 "result":{"data_array":[["42"]]}}
                                """) };
                        }
                        Check(body.RootElement.GetProperty("disposition").GetString() == "EXTERNAL_LINKS");
                        Check(body.RootElement.GetProperty("statement").GetString()!.Contains("VERSION AS OF 42"));
                    }
                    return new(HttpStatusCode.OK) { Content = new StringContent(request.Method == HttpMethod.Post
                        ? ExternalManifest(failure == "row-count" && fail ? 5 : 4, 2) : ExternalChunk(1, 2, 2)) };
                })) { BaseAddress = new("https://workspace.local.invalid") };
                using var external = new HttpClient(new LocalHttpHandler(request =>
                {
                    var chunk = request.RequestUri!.AbsolutePath.EndsWith("1") ? 1 : 0;
                    if (failure == "download" && fail && chunk == 1) return new(HttpStatusCode.Forbidden);
                    return new(HttpStatusCode.OK) { Content = new StringContent(JsonSerializer.Serialize(
                        Enumerable.Range(chunk * 2, 2).Select(index => new[] { index.ToString(), "content" }))) };
                }));
                Set(watcher, "_http", workspace);
                Set(watcher, "_externalHttp", external);
                await Fails(() => watcher.ProcessSnapshotAsync("catalog.schema.docs", default));
                Check((long)Get(watcher, "_lastVersion")! == -1L && sender.Sent.Count >= 2);
                Check((long)Get(watcher, "_snapshotVersion")! == 42L);
                fail = false;
                sender.FailSend = 0;
                await watcher.ProcessSnapshotAsync("catalog.schema.docs", default);
                Check((long)Get(watcher, "_lastVersion")! == 42L);
                Check(historyCalls == 1 && Get(watcher, "_snapshotVersion") is null);
                Check(sender.Sent.Select(message =>
                    JsonSerializer.Deserialize<OmniVec.ChangeFeed.Models.EmbeddingMessage>(message.Body)!.SourceRef)
                    .Distinct().Count() == 4);
            });
        Test("Backpressure does not turn administration failures into an empty queue", async () =>
        {
            var sender = new LocalSender();
            await using var publisher = Publisher(sender);
            Set(publisher, "_adminClient", new FailingAdministration());
            await Fails(() => publisher.HasCapacityAsync(default));
            await Fails(() => publisher.PublishBatchAsync([new OmniVec.ChangeFeed.Models.EmbeddingMessage()], default));
            Check(sender.Sent.Count == 0);
        });

        var failed = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failed++; Console.WriteLine($"FAIL {name}: {error}"); }
        }
        Console.WriteLine($"CONNECTOR RELIABILITY: {tests.Count - failed} passed, {failed} failed (local doubles)");
        return failed;
    }

    private static string ExternalManifest(int total, int firstCount) => JsonSerializer.Serialize(new
    {
        statement_id = "s1", status = new { state = "SUCCEEDED" },
        manifest = new { total_row_count = total, total_chunk_count = 2, truncated = false,
            schema = new { columns = new[] { new { name = "id", type_name = "STRING" }, new { name = "content", type_name = "STRING" } } } },
        result = JsonSerializer.Deserialize<JsonElement>(ExternalChunk(0, 0, firstCount)),
    });

    private static string ExternalChunk(int index, int offset, int count) => JsonSerializer.Serialize(new
    {
        external_links = new[] { new { chunk_index = index, row_offset = offset, row_count = count,
            external_link = $"https://result.local.invalid/chunk{index}",
            next_chunk_internal_link = index == 0 ? "/api/2.0/sql/statements/s1/result/chunks/1" : null } },
    });

    private sealed class ReliabilityWriter : IDestinationWriter
    {
        public string DestinationType => "cosmosdb-vector";
        public bool Fail { get; init; }
        public ConcurrentQueue<EmbeddingResult> Written { get; } = new();
        public Task WriteBatchAsync(Dictionary<string, object> config, List<EmbeddingResult> results, CancellationToken ct)
        {
            if (Fail) return Task.FromException(new InvalidOperationException("Destination failed"));
            foreach (var result in results) Written.Enqueue(result);
            return Task.CompletedTask;
        }
        public Task<bool> ReplaceSharePointAsync(Dictionary<string, object> config, SharePointReplacement replacement, CancellationToken ct)
            => Task.FromResult(true);
    }
    private class ReliabilityReceiver : ServiceBusReceiver
    {
        public int Completed, Abandoned, DeadLettered, Renewed;
        public TaskCompletionSource LockRenewed { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public ConcurrentBag<string> CompletedIds { get; } = new();
        public ConcurrentBag<string> AbandonedIds { get; } = new();
        public ConcurrentBag<string> DeadLetterIds { get; } = new();
        public bool LoseLock { get; init; }
        public bool FailDeadLetter { get; init; }
        public TaskCompletionSource FastCompleted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public override Task CompleteMessageAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
        {
            Interlocked.Increment(ref Completed);
            CompletedIds.Add(message.MessageId);
            if (message.MessageId == "fast") FastCompleted.TrySetResult();
            return Task.CompletedTask;
        }
        public override Task AbandonMessageAsync(ServiceBusReceivedMessage message,
            IDictionary<string, object>? propertiesToModify = null, CancellationToken cancellationToken = default)
        { Interlocked.Increment(ref Abandoned); AbandonedIds.Add(message.MessageId); return Task.CompletedTask; }
        public override Task DeadLetterMessageAsync(ServiceBusReceivedMessage message, string deadLetterReason,
            string? deadLetterErrorDescription = null, CancellationToken cancellationToken = default)
        {
            if (FailDeadLetter) return Task.FromException(new InvalidOperationException("Settlement failed"));
            Interlocked.Increment(ref DeadLettered);
            DeadLetterIds.Add(message.MessageId);
            return Task.CompletedTask;
        }
        public override Task RenewMessageLockAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
        {
            Interlocked.Increment(ref Renewed);
            LockRenewed.TrySetResult();
            return LoseLock
                ? Task.FromException(new ServiceBusException("Lost lock", ServiceBusFailureReason.MessageLockLost))
                : Task.CompletedTask;
        }
    }
    private sealed class LoopReceiver(CancellationTokenSource cancellation, ServiceBusReceivedMessage message) : ReliabilityReceiver
    {
        public int Receives { get; private set; }
        public override Task<IReadOnlyList<ServiceBusReceivedMessage>> ReceiveMessagesAsync(
            int maxMessages, TimeSpan? maxWaitTime = null, CancellationToken cancellationToken = default)
        {
            if (++Receives == 1)
                return Task.FromResult<IReadOnlyList<ServiceBusReceivedMessage>>(new[] { message });
            cancellation.Cancel();
            return Task.FromCanceled<IReadOnlyList<ServiceBusReceivedMessage>>(cancellationToken);
        }
    }
    private sealed class ScriptedReceiver(CancellationTokenSource cancellation,
        IReadOnlyList<IReadOnlyList<ServiceBusReceivedMessage>> batches) : ReliabilityReceiver
    {
        public int Receives { get; private set; }
        public override Task<IReadOnlyList<ServiceBusReceivedMessage>> ReceiveMessagesAsync(
            int maxMessages, TimeSpan? maxWaitTime = null, CancellationToken cancellationToken = default)
        {
            if (++Receives <= batches.Count) return Task.FromResult(batches[Receives - 1]);
            cancellation.Cancel();
            return Task.FromCanceled<IReadOnlyList<ServiceBusReceivedMessage>>(cancellationToken);
        }
    }

    private sealed class RecordingWatcher(string sourceId) : ISourceWatcher
    {
        public string SourceId => sourceId;
        public string Generation => "0";
        public bool SkipContentHash { get; set; }
        public int Disposals, PipelineUpdates;
        public void UpdatePipelines(List<Pipeline> pipelines) => PipelineUpdates++;
        public void UpdateDestinations(List<Destination> destinations) { }
        public Task StartAsync(CancellationToken ct) => Task.CompletedTask;
        public ValueTask DisposeAsync() { Disposals++; return ValueTask.CompletedTask; }
    }

    private sealed class ManagerLogger : ILogger<SourceWatcherManager>
    {
        public List<string> Messages { get; } = new();
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter)
            => Messages.Add(formatter(state, error));
    }

    private sealed class WorkerLogger : ILogger<EmbeddingWorkerService>
    {
        public ConcurrentQueue<(LogLevel Level, string Message, Exception? Error)> Entries { get; } = new();
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter)
            => Entries.Enqueue((level, formatter(state, error), error));
    }

    private sealed class MetricsLogger : ILogger<MetricsReporter>
    {
        public int Warnings { get; private set; }
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter)
        { if (level == LogLevel.Warning) Warnings++; }
    }

    private sealed class AsyncHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> handler) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
            => handler(request, ct);
    }
    private sealed class FailingAdministration : ServiceBusAdministrationClient
    {
        public override Task<Response<SubscriptionRuntimeProperties>> GetSubscriptionRuntimePropertiesAsync(
            string topicName, string subscriptionName, CancellationToken cancellationToken = default)
            => throw new InvalidOperationException("Management endpoint unavailable");
    }
}
