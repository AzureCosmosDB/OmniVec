using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using OmniVec.ChangeFeed.Services;
using OmniVec.Worker.Services;

internal static class MetricsTransportTests
{
    private static void Check(bool value, string message = "Assertion failed")
    { if (!value) throw new Exception(message); }

    private static MetricsTransportOptions Fast(int capacity = 100, int attempts = 3, int drainMs = 2000, int attemptMs = 2000) => new()
    {
        Capacity = capacity, MaxAttempts = attempts, InitialBackoff = TimeSpan.FromMilliseconds(5),
        MaxBackoff = TimeSpan.FromMilliseconds(20), AttemptTimeout = TimeSpan.FromMilliseconds(attemptMs),
        DrainTimeout = TimeSpan.FromMilliseconds(drainMs), DropLogInterval = TimeSpan.Zero,
    };

    private static HttpClient Http(HttpMessageHandler handler) => new(handler) { BaseAddress = new("http://local.invalid") };

    public static async Task<int> RunAsync()
    {
        var failures = 0;
        var passed = 0;
        async Task Test(string name, Func<Task> run)
        {
            try { await run().WaitAsync(TimeSpan.FromSeconds(30)); passed++; Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failures++; Console.WriteLine($"FAIL {name}: {error.Message}"); }
        }

        await Test("Worker report returns immediately while metrics HTTP is blocked", async () =>
        {
            var handler = new BlockedHandler();
            var reporter = new MetricsReporter(Http(handler), NullLogger<MetricsReporter>.Instance, Fast());
            var sw = Stopwatch.StartNew();
            var task = reporter.ReportInlineMetricsAsync("pipeline", 3, 0, 10, "batch-1");
            Check(task.IsCompletedSuccessfully && sw.ElapsedMilliseconds < 500, "report waited on I/O");
            await handler.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            Check(reporter.Transport.Pending == 1 && reporter.Transport.Sent == 0);
            handler.Release();
            Check(await reporter.Transport.FlushAsync(TimeSpan.FromSeconds(5)) && reporter.Transport.Sent == 1);
        });

        await Test("Ingestion API client report returns immediately while metrics HTTP is blocked", async () =>
        {
            var handler = new BlockedHandler();
            var api = new OmniVecApiClient(Http(handler), NullLogger<OmniVecApiClient>.Instance, Fast());
            var task = api.ReportInlineMetricsAsync("pipeline", 5, 1, 7, "ingest-batch");
            Check(task.IsCompletedSuccessfully, "ingestion report waited on I/O");
            await handler.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            handler.Release();
            Check(await api.MetricsTransport.FlushAsync(TimeSpan.FromSeconds(5)) && api.MetricsTransport.Sent == 1);
        });

        await Test("Full bounded queue drops explicitly with counter and warning, never blocks", async () =>
        {
            var handler = new BlockedHandler();
            var logger = new ListLogger();
            var transport = new MetricsTransport(() => Http(handler), logger, Fast(capacity: 2));
            var sw = Stopwatch.StartNew();
            var admitted = 0;
            for (var i = 0; i < 20; i++)
            {
                if (transport.TryEnqueue("pipeline", $"b{i}", 1, 0, new { processed = 1 })) admitted++;
                if (i == 0) await handler.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            }
            Check(sw.ElapsedMilliseconds < 5000, "enqueue blocked");
            Check(admitted == 3, $"admitted={admitted}");
            Check(transport.DroppedQueueFull == 17 && transport.Enqueued == 3, $"dropped={transport.DroppedQueueFull}");
            Check(logger.Entries.Any(e => e.Level == LogLevel.Warning && e.Message.Contains("queue full")), "no drop warning");
            handler.Release();
            Check(await transport.FlushAsync(TimeSpan.FromSeconds(5)) && transport.Sent == 3);
        });

        await Test("Worker payload keeps rich tags and inline metrics path", async () =>
        {
            var handler = new RecordingHandler(_ => HttpStatusCode.OK);
            var reporter = new MetricsReporter(Http(handler), NullLogger<MetricsReporter>.Instance, Fast());
            await reporter.ReportInlineMetricsAsync("pipe-1", 4, 1, 99, "key-1", sourceId: "src", destinationId: "dst",
                modelId: "mdl", tokensUsed: 12, inputBytes: 34, queueWaitMs: 5.5, retryCount: 2, throttleCount: 1,
                throttleDelayMs: 7, errorCategory: "http_429", lastDocument: "doc", resourceWeight: 20,
                maxConcurrencyPerWorker: 4, resourcePriority: "high", workloadClass: "dedicated");
            Check(await reporter.Transport.FlushAsync(TimeSpan.FromSeconds(5)));
            var (path, body, contentType) = handler.Requests.Single();
            Check(path == "/api/pipelines/pipe-1/metrics/inline", path);
            Check(contentType == "application/json", contentType ?? "no content type");
            var root = body.RootElement;
            Check(root.GetProperty("processed").GetInt32() == 4 && root.GetProperty("failed").GetInt32() == 1);
            Check(root.GetProperty("processing_time_ms").GetInt64() == 99 && root.GetProperty("batch_key").GetString() == "key-1");
            Check(root.GetProperty("source_id").GetString() == "src" && root.GetProperty("destination_id").GetString() == "dst");
            Check(root.GetProperty("model_id").GetString() == "mdl" && root.GetProperty("tokens_used").GetInt64() == 12);
            Check(root.GetProperty("input_bytes").GetInt64() == 34 && root.GetProperty("queue_wait_ms").GetDouble() == 5.5);
            Check(root.GetProperty("retry_count").GetInt32() == 2 && root.GetProperty("throttle_count").GetInt32() == 1);
            Check(root.GetProperty("throttle_delay_ms").GetDouble() == 7 && root.GetProperty("error_category").GetString() == "http_429");
            Check(root.GetProperty("last_document").GetString() == "doc" && root.GetProperty("resource_weight").GetInt32() == 20);
            Check(root.GetProperty("max_concurrency_per_worker").GetInt32() == 4);
            Check(root.GetProperty("resource_priority").GetString() == "high" && root.GetProperty("workload_class").GetString() == "dedicated");
            var reportedAt = DateTimeOffset.Parse(root.GetProperty("reported_at").GetString()!);
            Check(Math.Abs((DateTimeOffset.UtcNow - reportedAt).TotalMinutes) < 1, "reported_at missing/not UTC now");
        });

        await Test("Ingestion payload keeps the four inline fields plus reported_at and path", async () =>
        {
            var handler = new RecordingHandler(_ => HttpStatusCode.OK);
            var api = new OmniVecApiClient(Http(handler), NullLogger<OmniVecApiClient>.Instance, Fast());
            await api.ReportInlineMetricsAsync("pipe-2", 8, 2, 50, "ingest-key");
            Check(await api.MetricsTransport.FlushAsync(TimeSpan.FromSeconds(5)));
            var (path, body, _) = handler.Requests.Single();
            Check(path == "/api/pipelines/pipe-2/metrics/inline", path);
            var names = body.RootElement.EnumerateObject().Select(p => p.Name).OrderBy(n => n).ToArray();
            Check(names.SequenceEqual(new[] { "batch_key", "failed", "processed", "processing_time_ms", "reported_at" }), string.Join(",", names));
            Check(DateTimeOffset.Parse(body.RootElement.GetProperty("reported_at").GetString()!).Offset == TimeSpan.Zero);
            Check(body.RootElement.GetProperty("processed").GetInt32() == 8 && body.RootElement.GetProperty("failed").GetInt32() == 2);
            Check(body.RootElement.GetProperty("processing_time_ms").GetInt64() == 50);
            Check(body.RootElement.GetProperty("batch_key").GetString() == "ingest-key");
        });

        await Test("Change-feed metrics are non-blocking, keep payload/path and stable batch_key across retries", async () =>
        {
            var blocked = new BlockedHandler();
            var api = new OmniVecApiClient(Http(blocked), NullLogger<OmniVecApiClient>.Instance, Fast());
            var task = api.ReportChangeFeedMetricsAsync("src", 10, 4, 3, 3, 4, "lease-0");
            Check(task.IsCompletedSuccessfully, "changefeed report waited on I/O");
            await blocked.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            blocked.Release();
            Check(await api.MetricsTransport.FlushAsync(TimeSpan.FromSeconds(5)) && api.MetricsTransport.Sent == 1);

            var calls = 0;
            var flaky = new RecordingHandler(_ => Interlocked.Increment(ref calls) < 2 ? HttpStatusCode.BadGateway : HttpStatusCode.OK);
            var retrying = new OmniVecApiClient(Http(flaky), NullLogger<OmniVecApiClient>.Instance, Fast());
            await retrying.ReportChangeFeedMetricsAsync("src", 10, 4, 3, 3, 4, "lease-0");
            Check(await retrying.MetricsTransport.FlushAsync(TimeSpan.FromSeconds(5)) && retrying.MetricsTransport.Sent == 1);
            var requests = flaky.Requests;
            Check(requests.Count == 2 && requests.All(r => r.Path == "/api/metrics/changefeed"));
            var keys = requests.Select(r => r.Body.RootElement.GetProperty("batch_key").GetString()).Distinct().ToList();
            Check(keys.Count == 1 && keys[0]!.StartsWith("changefeed:src:lease-0:"), "batch_key changed across retries");
            var root = requests[0].Body.RootElement;
            Check(root.GetProperty("source_id").GetString() == "src" && root.GetProperty("total").GetInt32() == 10);
            Check(root.GetProperty("eligible").GetInt32() == 4 && root.GetProperty("skipped_no_content").GetInt32() == 3);
            Check(root.GetProperty("skipped_unchanged").GetInt32() == 3 && root.GetProperty("jobs_created").GetInt32() == 4);
            Check(root.GetProperty("partition").GetString() == "lease-0" && root.TryGetProperty("reported_at", out _));

            using var cancelled = new CancellationTokenSource();
            cancelled.Cancel();
            Check(retrying.ReportChangeFeedMetricsAsync("src", 1, 0, 0, 0, 0, "p", cancelled.Token).IsCanceled
                && retrying.MetricsTransport.CallerCancelled == 1);
        });
        await Test("Transient failures retry with the same batch_key; exhaustion is counted and logged", async () =>
        {
            var calls = 0;
            var handler = new RecordingHandler(_ => Interlocked.Increment(ref calls) < 3 ? HttpStatusCode.ServiceUnavailable : HttpStatusCode.OK);
            var transport = new MetricsTransport(() => Http(handler), NullLogger.Instance, Fast(attempts: 3));
            transport.TryEnqueue("pipeline", "stable-key", 1, 0, new { batch_key = "stable-key" });
            Check(await transport.FlushAsync(TimeSpan.FromSeconds(5)));
            Check(calls == 3 && transport.Sent == 1 && transport.Failed == 0, $"calls={calls}");
            Check(handler.Requests.All(r => r.Body.RootElement.GetProperty("batch_key").GetString() == "stable-key"));

            var logger = new ListLogger();
            var failing = new RecordingHandler(_ => HttpStatusCode.InternalServerError);
            var exhausted = new MetricsTransport(() => Http(failing), logger, Fast(attempts: 3));
            exhausted.TryEnqueue("pipeline", "k", 1, 0, new { });
            Check(await exhausted.FlushAsync(TimeSpan.FromSeconds(5)));
            Check(failing.Requests.Count == 3 && exhausted.Failed == 1 && exhausted.Sent == 0);
            Check(logger.Entries.Any(e => e.Level == LogLevel.Error && e.Message.Contains("FAILED after 3 attempts")));

            var rejecting = new RecordingHandler(_ => HttpStatusCode.BadRequest);
            var rejected = new MetricsTransport(() => Http(rejecting), NullLogger.Instance, Fast(attempts: 3));
            rejected.TryEnqueue("pipeline", "k", 1, 0, new { });
            Check(await rejected.FlushAsync(TimeSpan.FromSeconds(5)));
            Check(rejecting.Requests.Count == 1 && rejected.Failed == 1, "4xx must not retry");
        });

        await Test("Per-attempt timeout bounds a hung metrics endpoint", async () =>
        {
            var handler = new BlockedHandler();
            var transport = new MetricsTransport(() => Http(handler), NullLogger.Instance, Fast(attempts: 2, attemptMs: 100));
            var sw = Stopwatch.StartNew();
            transport.TryEnqueue("pipeline", "k", 1, 0, new { });
            Check(await transport.FlushAsync(TimeSpan.FromSeconds(5)), "timeout did not bound delivery");
            Check(transport.Failed == 1 && handler.Calls == 2 && sw.ElapsedMilliseconds < 3000, $"calls={handler.Calls}");
        });

        await Test("Handler exceptions never surface to the caller", async () =>
        {
            var reporter = new MetricsReporter(Http(new ThrowingHandler()), NullLogger<MetricsReporter>.Instance, Fast(attempts: 2));
            var task = reporter.ReportInlineMetricsAsync("pipeline", 1, 0, 0, "k");
            Check(task.IsCompletedSuccessfully);
            Check(await reporter.Transport.FlushAsync(TimeSpan.FromSeconds(5)) && reporter.Transport.Failed == 1);
        });

        await Test("Caller cancellation propagates before admission and cannot cancel admitted reports", async () =>
        {
            var handler = new BlockedHandler();
            var reporter = new MetricsReporter(Http(handler), NullLogger<MetricsReporter>.Instance, Fast());
            using var cancelled = new CancellationTokenSource();
            cancelled.Cancel();
            var task = reporter.ReportInlineMetricsAsync("pipeline", 1, 0, 0, "k", cancelled.Token);
            Check(task.IsCanceled && reporter.Transport.CallerCancelled == 1 && reporter.Transport.Enqueued == 0);
            try { await task; throw new Exception("expected cancellation"); } catch (OperationCanceledException) { }

            var api = new OmniVecApiClient(Http(handler), NullLogger<OmniVecApiClient>.Instance, Fast());
            Check(api.ReportInlineMetricsAsync("pipeline", 1, 0, 0, "k", cancelled.Token).IsCanceled);

            using var request = new CancellationTokenSource();
            await reporter.ReportInlineMetricsAsync("pipeline", 1, 0, 0, "admitted", request.Token);
            await handler.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            request.Cancel();
            handler.Release();
            Check(await reporter.Transport.FlushAsync(TimeSpan.FromSeconds(5)) && reporter.Transport.Sent == 1,
                "admitted report was cancelled by the caller token");
        });

        await Test("Host shutdown drains queued reports within the drain bound", async () =>
        {
            var handler = new RecordingHandler(_ => HttpStatusCode.OK, delayMs: 20);
            var transport = new MetricsTransport(() => Http(handler), NullLogger.Instance, Fast(drainMs: 5000));
            await transport.StartAsync(default);
            for (var i = 0; i < 10; i++) transport.TryEnqueue("pipeline", $"k{i}", 1, 0, new { });
            await transport.StopAsync(default);
            Check(transport.Sent == 10 && transport.Pending == 0, $"sent={transport.Sent}");
            Check(!transport.TryEnqueue("pipeline", "late", 1, 0, new { }) && transport.DroppedShutdown == 1,
                "post-stop report not explicitly dropped");
        });

        await Test("Host shutdown with hung endpoint is bounded and counts abandoned reports", async () =>
        {
            var handler = new BlockedHandler();
            var logger = new ListLogger();
            var transport = new MetricsTransport(() => Http(handler), logger, Fast(drainMs: 200, attemptMs: 60_000));
            await transport.StartAsync(default);
            for (var i = 0; i < 5; i++) transport.TryEnqueue("pipeline", $"k{i}", 1, 0, new { });
            await handler.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var sw = Stopwatch.StartNew();
            await transport.StopAsync(default);
            Check(sw.ElapsedMilliseconds < 3000, $"stop took {sw.ElapsedMilliseconds}ms");
            Check(transport.DroppedShutdown == 5 && transport.Pending == 0 && transport.Sent == 0, $"dropped={transport.DroppedShutdown}");
            Check(logger.Entries.Any(e => e.Level == LogLevel.Warning && e.Message.Contains("abandoned 4 queued reports")));

            var second = new MetricsTransport(() => Http(new BlockedHandler()), NullLogger.Instance, Fast(drainMs: 60_000, attemptMs: 60_000));
            second.TryEnqueue("pipeline", "k", 1, 0, new { });
            using var hostStop = new CancellationTokenSource(TimeSpan.FromMilliseconds(100));
            sw.Restart();
            await second.StopAsync(hostStop.Token);
            Check(sw.ElapsedMilliseconds < 3000 && second.DroppedShutdown == 1, "host stopping token not respected");
        });

        await Test("Single background sender: no per-report tasks or concurrent posts", async () =>
        {
            var handler = new RecordingHandler(_ => HttpStatusCode.OK, delayMs: 2);
            var reporter = new MetricsReporter(Http(handler), NullLogger<MetricsReporter>.Instance, Fast(capacity: 1000));
            var reports = Enumerable.Range(0, 50).Select(i => reporter.ReportInlineMetricsAsync("pipeline", 1, 0, 0, $"k{i}")).ToList();
            Check(reports.All(t => t.IsCompletedSuccessfully));
            Check(await reporter.Transport.FlushAsync(TimeSpan.FromSeconds(10)));
            Check(reporter.Transport.Sent == 50 && handler.MaxConcurrency == 1 && reporter.Transport.MaxObservedConcurrency == 1,
                $"concurrency={handler.MaxConcurrency}");
        });

        await Test("DI: worker and ingestion share one hosted singleton transport over factory HttpClient", async () =>
        {
            var handler = new RecordingHandler(_ => HttpStatusCode.OK);
            var services = new ServiceCollection();
            services.AddLogging();
            var config = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["MetricsTransport:Capacity"] = "7",
            }).Build();
            services.AddMetricsTransport(config, _ => "http://local.invalid");
            services.AddHttpClient(MetricsTransportOptions.HttpClientName).ConfigurePrimaryHttpMessageHandler(() => handler);
            services.AddSingleton<MetricsReporter>(sp => new MetricsReporter(
                sp.GetRequiredService<MetricsTransport>(), sp.GetRequiredService<ILogger<MetricsReporter>>()));
            services.AddHttpClient<OmniVecApiClient>(client => client.BaseAddress = new("http://local.invalid"));
            await using var provider = services.BuildServiceProvider();
            var transport = provider.GetRequiredService<MetricsTransport>();
            var hosted = provider.GetServices<IHostedService>().ToList();
            Check(hosted.Count == 1 && ReferenceEquals(hosted[0], transport), "transport not the hosted singleton");
            Check(ReferenceEquals(provider.GetRequiredService<MetricsReporter>(), provider.GetRequiredService<MetricsReporter>()));
            var api1 = provider.GetRequiredService<OmniVecApiClient>();
            var api2 = provider.GetRequiredService<OmniVecApiClient>();
            Check(ReferenceEquals(api1.MetricsTransport, transport) && ReferenceEquals(api2.MetricsTransport, transport),
                "typed API client does not use the shared transport");
            Check(ReferenceEquals(provider.GetRequiredService<MetricsReporter>().Transport, transport));
            await transport.StartAsync(default);
            await provider.GetRequiredService<MetricsReporter>().ReportInlineMetricsAsync("p-worker", 1, 0, 0, "w");
            await api1.ReportInlineMetricsAsync("p-ingest", 1, 0, 0, "i");
            await transport.StopAsync(default);
            Check(transport.Sent == 2 && handler.Requests.Select(r => r.Path).OrderBy(p => p)
                .SequenceEqual(new[] { "/api/pipelines/p-ingest/metrics/inline", "/api/pipelines/p-worker/metrics/inline" }));
        });

        Console.WriteLine($"METRICS TRANSPORT: {passed} passed, {failures} failed");
        return failures;
    }

    internal sealed class BlockedHandler : HttpMessageHandler
    {
        private readonly TaskCompletionSource _release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _calls;
        public int Calls => Volatile.Read(ref _calls);
        public void Release() => _release.TrySetResult();
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Interlocked.Increment(ref _calls);
            Entered.TrySetResult();
            await _release.Task.WaitAsync(ct);
            return new HttpResponseMessage(HttpStatusCode.OK);
        }
    }

    private sealed class RecordingHandler(Func<int, HttpStatusCode> status, int delayMs = 0) : HttpMessageHandler
    {
        private int _inFlight, _max, _count;
        public ConcurrentQueue<(string Path, JsonDocument Body, string? ContentType)> RequestQueue { get; } = new();
        public List<(string Path, JsonDocument Body, string? ContentType)> Requests => RequestQueue.ToList();
        public int MaxConcurrency => Volatile.Read(ref _max);
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            var now = Interlocked.Increment(ref _inFlight);
            if (now > _max) _max = now;
            try
            {
                var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
                RequestQueue.Enqueue((request.RequestUri!.AbsolutePath, body, request.Content.Headers.ContentType?.MediaType));
                if (delayMs > 0) await Task.Delay(delayMs, ct);
                return new HttpResponseMessage(status(Interlocked.Increment(ref _count)));
            }
            finally { Interlocked.Decrement(ref _inFlight); }
        }
    }

    private sealed class ThrowingHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
            => throw new HttpRequestException("metrics API offline");
    }

    private sealed class ListLogger : ILogger
    {
        public ConcurrentQueue<(LogLevel Level, string Message)> Entries { get; } = new();
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter)
            => Entries.Enqueue((level, formatter(state, error)));
    }
}
