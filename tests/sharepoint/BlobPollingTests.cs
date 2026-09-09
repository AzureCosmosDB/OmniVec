using System.Reflection;
using Azure;
using Azure.Messaging.ServiceBus;
using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using OmniVec.ChangeFeed.Services;

internal static class BlobPollingTests
{
    private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;
    private static readonly DateTimeOffset Second = new(2026, 9, 8, 5, 41, 46, TimeSpan.Zero);
    private static void Check(bool value, string message = "Assertion failed")
    { if (!value) throw new Exception(message); }
    private static void Set(object target, string name, object value)
        => target.GetType().GetField(name, Private)!.SetValue(target, value);
    private static object? Get(object target, string name)
        => target.GetType().GetField(name, Private)!.GetValue(target);
    private static Task Invoke(BlobSourceWatcher watcher, string name, CancellationToken ct = default)
        => (Task)typeof(BlobSourceWatcher).GetMethod(name, Private)!.Invoke(watcher, [ct])!;
    private static BlobItem Blob(string name, string version, DateTimeOffset? modified)
    {
        var factory = typeof(BlobsModelFactory).GetMethods().Where(method => method.Name == "BlobItemProperties")
            .OrderBy(method => method.GetParameters().Length).First();
        var properties = (BlobItemProperties)factory.Invoke(null, factory.GetParameters().Select(parameter =>
            parameter.Name == "eTag" ? (object)new ETag(version)
                : parameter.Name == "lastModified" ? modified
                : parameter.HasDefaultValue ? parameter.DefaultValue
                : parameter.ParameterType.IsValueType ? Activator.CreateInstance(parameter.ParameterType) : null).ToArray())!;
        return BlobsModelFactory.BlobItem(name, properties: properties);
    }

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string, Func<Task>)>();
        void Test(string name, Func<Task> run) => tests.Add((name, run));

        Test("Blob live polling includes a version created after scan start in the same second", async () =>
        {
            await using var f = new Fixture();
            f.Now = Second.AddMilliseconds(800);
            Set(f.Watcher, "_lastPollTime", Second.AddSeconds(-5));
            await f.Poll();
            f.Container.Blobs = [Blob("new.pdf", "v1", Second)];
            f.Now = Second.AddSeconds(5).AddMilliseconds(800);
            await f.Poll();
            Check(f.Sender.Sent.Count == 1, "Whole-second LastModified was permanently skipped");
            await f.Poll();
            Check(f.Sender.Sent.Count == 1, "Unchanged version must not be republished");
        });

        Test("Blob overlapping watermark deduplicates versions and prunes history outside the window", async () =>
        {
            await using var f = new Fixture();
            f.Now = Second.AddMilliseconds(100);
            Set(f.Watcher, "_lastPollTime", Second.AddSeconds(-5));
            f.Container.Blobs = [Blob("a.pdf", "v1", Second)];
            await f.Poll();
            f.Now = Second.AddMilliseconds(500);
            await f.Poll();
            Check(f.Sender.Sent.Count == 1 && f.Versions.Count == 1);
            f.Container.Blobs = [Blob("a.pdf", "v2", Second), Blob("b.pdf", "v1", Second)];
            f.Now = Second.AddMilliseconds(900);
            await f.Poll();
            Check(f.Sender.Sent.Count == 3, "A new ETag or blob in the boundary second must publish");
            Check(f.Versions.SetEquals(["a.pdf:v2", "b.pdf:v1"]), "Obsolete ETags must be pruned");
            f.Now = Second.AddSeconds(5);
            await f.Poll();
            Check(f.Sender.Sent.Count == 3 && f.Versions.Count == 0, "Old overlap entries must not accumulate");
        });

        Test("Blob prefill boundary version is deduplicated but a later same-second update is published", async () =>
        {
            await using var f = new Fixture();
            f.Now = Second.AddMilliseconds(800);
            Set(f.Watcher, "_lastPollTime", f.Now);
            f.Container.Blobs = [Blob("a.pdf", "v1", Second)];
            await f.Watcher.PublishPageAsync(f.Container.Blobs, null, default);
            await f.Poll();
            Check(f.Sender.Sent.Count == 1 && f.Versions.Count == 1);
            f.Container.Blobs = [Blob("a.pdf", "v2", Second)];
            f.Now = Second.AddSeconds(5);
            await f.Poll();
            Check(f.Sender.Sent.Count == 2);
        });

        Test("Blob backpressure retains the overlapping watermark and published-version markers", async () =>
        {
            await using var f = new Fixture();
            f.Now = Second.AddMilliseconds(800);
            Set(f.Watcher, "_lastPollTime", f.Now);
            f.Versions.Add("known.pdf:v1");
            f.Container.Blobs = [Blob("new.pdf", "v1", Second)];
            Set(f.Publisher, "_backpressureThreshold", 0);
            f.Now = Second.AddSeconds(5);
            await f.Poll();
            Check(f.Watermark == Second.AddMilliseconds(800), "Backpressure advanced the checkpoint");
            Check(f.Versions.SetEquals(["known.pdf:v1"]) && f.Sender.Sent.Count == 0);
            Set(f.Publisher, "_backpressureThreshold", 5000);
            await f.Poll();
            Check(f.Sender.Sent.Count == 1 && f.Watermark == f.Now);
        });

        Test("Blob partial live publication retains checkpoint and replays deterministic message IDs", async () =>
        {
            await using var f = new Fixture();
            f.Now = Second.AddSeconds(5);
            var checkpoint = Second.AddMilliseconds(800);
            Set(f.Watcher, "_lastPollTime", checkpoint);
            f.Container.Blobs = [Blob("a.pdf", "v1", Second), Blob("b.pdf", "v1", Second)];
            f.Sender.Capacity = 1;
            f.Sender.FailSend = 2;
            Exception? failure = null;
            try { await f.Poll(); } catch (InvalidOperationException ex) { failure = ex; }
            Check(failure is not null, "Expected the second send to fail");
            Check(f.Watermark == checkpoint && f.Versions.Count == 0);
            var attempted = f.Sender.AttemptedIds.ToArray();
            f.Sender.FailSend = 0;
            await f.Poll();
            Check(f.Sender.AttemptedIds.Skip(2).SequenceEqual(attempted));
            Check(f.Watermark == f.Now && f.Sender.Sent.Count == 3);
        });

        foreach (var live in new[] { false, true })
        foreach (var storageTimeout in new[] { false, true })
            Test($"Blob {(live ? "live" : "prefill")} recovers from {(storageTimeout ? "storage" : "publisher")} cancellation with bounded backoff", async () =>
            {
                await using var f = new Fixture(new ChangeFeedOptions { BlobLivePollingEnabled = live });
                using var stop = new CancellationTokenSource();
                Set(f.Watcher, "_prefillDone", live);
                f.Now = Second;
                f.Container.Blobs = [Blob("a.pdf", "v1", Second.AddSeconds(1))];
                f.Container.BeforeList = () =>
                {
                    if (storageTimeout && f.Container.Calls == 1)
                        throw new OperationCanceledException("Storage request timed out");
                };
                if (!storageTimeout) Set(f.Publisher, "_sender", new TimeoutSender(f.Sender));
                var delays = new List<TimeSpan>();
                f.Watcher.DelayAsync = (delay, ct) =>
                {
                    delays.Add(delay);
                    if (delay == TimeSpan.FromSeconds(30))
                        Check(f.Watermark == f.Now && f.Versions.Count == 0,
                            "Cancellation must not advance publication state");
                    if (delay == TimeSpan.FromSeconds(5)) stop.Cancel();
                    ct.ThrowIfCancellationRequested();
                    return Task.CompletedTask;
                };
                await Invoke(f.Watcher, "RunAsync", stop.Token);
                Check(f.Container.Calls == 2 && f.Sender.Sent.Count == 1, "Timeout silently stopped the watcher");
                Check(delays.First() == TimeSpan.FromSeconds(30), "Timeout must use configured error backoff");
                Check(f.Logger.Errors.Single().Error is OperationCanceledException, "Timeout must be logged");
            });

        foreach (var live in new[] { false, true })
            Test($"Blob {(live ? "live" : "prefill")} actual shutdown exits without error or retry", async () =>
            {
                await using var f = new Fixture();
                using var stop = new CancellationTokenSource();
                Set(f.Watcher, "_prefillDone", live);
                f.Container.BeforeList = () =>
                {
                    stop.Cancel();
                    throw new OperationCanceledException(stop.Token);
                };
                var delays = 0;
                f.Watcher.DelayAsync = (_, _) => { delays++; return Task.CompletedTask; };
                await Invoke(f.Watcher, "RunAsync", stop.Token);
                Check(f.Container.Calls == 1 && delays == 0 && f.Logger.Errors.Count == 0);
                Check(f.Sender.Sent.Count == 0);
            });

        Test("Blob shutdown during timeout backoff exits cleanly without polling again", async () =>
        {
            await using var f = new Fixture();
            using var stop = new CancellationTokenSource();
            Set(f.Watcher, "_prefillDone", true);
            f.Container.BeforeList = () => throw new OperationCanceledException("Request timeout");
            var delays = 0;
            f.Watcher.DelayAsync = (_, ct) =>
            {
                delays++;
                stop.Cancel();
                return Task.FromCanceled(ct);
            };
            await Invoke(f.Watcher, "RunAsync", stop.Token);
            Check(f.Container.Calls == 1 && delays == 1 && f.Logger.Errors.Count == 1);
        });

        var failures = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS {name}"); }
            catch (Exception ex) { failures++; Console.WriteLine($"FAIL {name}: {ex}"); }
        }
        Console.WriteLine($"BLOB POLLING: {tests.Count - failures} passed, {failures} failed (local doubles; no Azure writes)");
        return failures;
    }

    private sealed class Fixture : IAsyncDisposable
    {
        public LocalSender Sender { get; } = new();
        public ServiceBusPublisher Publisher { get; }
        public LocalContainer Container { get; } = new();
        public BlobLogger Logger { get; } = new();
        public BlobSourceWatcher Watcher { get; }
        public DateTimeOffset Now { get; set; } = Second;
        public DateTimeOffset? Watermark => (DateTimeOffset?)Get(Watcher, "_lastPollTime");
        public HashSet<string> Versions => (HashSet<string>)Get(Watcher, "_processedBlobs")!;

        public Fixture(ChangeFeedOptions? options = null)
        {
            Publisher = new(Options.Create(new ChangeFeedOptions()), NullLogger<ServiceBusPublisher>.Instance);
            Set(Publisher, "_sender", Sender);
            Set(Publisher, "_enabled", true);
            Watcher = new(new Source { Id = "source", Type = "azure-blob" }, options ?? new(), null!, new(),
                Logger, sbPublisher: Publisher)
            {
                BlobClientFactory = () => new LocalBlobService(Container),
                UtcNow = () => Now,
            };
            Watcher.UpdatePipelines([new Pipeline
            {
                Id = "pipeline", DestinationId = "dest",
                Sources = [new PipelineSource { SourceId = "source" }],
            }]);
            Watcher.UpdateDestinations([new Destination { Id = "dest", Type = "cosmosdb-vector" }]);
        }
        public Task Poll() => Invoke(Watcher, "PollForNewBlobsAsync");
        public async ValueTask DisposeAsync()
        {
            await Watcher.DisposeAsync();
            await Publisher.DisposeAsync();
        }
    }

    private sealed class LocalBlobService(LocalContainer container) : BlobServiceClient
    {
        public override BlobContainerClient GetBlobContainerClient(string blobContainerName) => container;
    }

    private sealed class TimeoutSender(LocalSender inner) : ServiceBusSender
    {
        private bool _firstSend = true;
        public override ValueTask<ServiceBusMessageBatch> CreateMessageBatchAsync(CancellationToken ct = default)
            => inner.CreateMessageBatchAsync(ct);
        public override Task SendMessagesAsync(ServiceBusMessageBatch batch, CancellationToken ct = default)
        {
            if (_firstSend)
            {
                _firstSend = false;
                throw new TaskCanceledException("Publisher request timed out");
            }
            return inner.SendMessagesAsync(batch, ct);
        }
        public override ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private sealed class LocalContainer : BlobContainerClient
    {
        public List<BlobItem> Blobs { get; set; } = [];
        public int Calls { get; private set; }
        public Action? BeforeList { get; set; }
        public override AsyncPageable<BlobItem> GetBlobsAsync(BlobTraits traits = BlobTraits.None,
            BlobStates states = BlobStates.None, string? prefix = null, CancellationToken cancellationToken = default)
        {
            Calls++;
            BeforeList?.Invoke();
            return AsyncPageable<BlobItem>.FromPages([Page<BlobItem>.FromValues(Blobs, null, null!)]);
        }
    }

    private sealed class BlobLogger : ILogger<BlobSourceWatcher>
    {
        public List<(string Message, Exception? Error)> Errors { get; } = [];
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel logLevel) => true;
        public void Log<TState>(LogLevel level, EventId eventId, TState state, Exception? exception,
            Func<TState, Exception?, string> formatter)
        {
            if (level >= LogLevel.Error) Errors.Add((formatter(state, exception), exception));
        }
    }
}
