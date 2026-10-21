using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Threading.Channels;
using Microsoft.Extensions.Options;

namespace OmniVec.Worker.Services;

/// <summary>Bounds for best-effort control-plane metrics delivery (config section "MetricsTransport").</summary>
public sealed class MetricsTransportOptions
{
    public const string SectionName = "MetricsTransport";
    public const string HttpClientName = "OmniVecMetrics";

    public int Capacity { get; set; } = 10_000;
    public int MaxAttempts { get; set; } = 3;
    public TimeSpan AttemptTimeout { get; set; } = TimeSpan.FromSeconds(5);
    public TimeSpan InitialBackoff { get; set; } = TimeSpan.FromMilliseconds(200);
    public TimeSpan MaxBackoff { get; set; } = TimeSpan.FromSeconds(2);
    public TimeSpan DrainTimeout { get; set; } = TimeSpan.FromSeconds(10);
    public TimeSpan DropLogInterval { get; set; } = TimeSpan.FromSeconds(10);
}

/// <summary>
/// Best-effort, non-blocking inline-metrics transport. Reports are admitted with a
/// bounded TryWrite (never blocking the embedding loop) and posted by a single
/// background sender with bounded per-attempt timeout and retries. Every report
/// that is not delivered is explicitly counted and logged (queue full, shutdown
/// abandonment, rejection, retry exhaustion). Retries reuse the same batch_key so
/// the control plane can deduplicate. Registered as a singleton hosted service so
/// queued reports are drained (bounded) on host shutdown.
/// </summary>
public sealed class MetricsTransport : IHostedService
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private static readonly MediaTypeHeaderValue JsonContentType = new("application/json") { CharSet = "utf-8" };

    private readonly Func<HttpClient> _httpFactory;
    private readonly ILogger _logger;
    private readonly MetricsTransportOptions _options;
    private readonly Channel<Report> _queue;
    private readonly CancellationTokenSource _abort = new();
    private readonly object _startLock = new();
    private Task? _sender;
    private long _enqueued, _sent, _droppedFull, _droppedShutdown, _failed, _cancelled, _pending;
    private long _droppedSinceLog, _lastDropLogTicks;
    private int _maxObservedConcurrency, _inFlight;

    internal readonly record struct Report(string Kind, string Path, byte[] Body, string PipelineId, string BatchKey, int Processed, int Failed);

    public MetricsTransport(Func<HttpClient> httpFactory, ILogger logger, MetricsTransportOptions? options = null)
    {
        _httpFactory = httpFactory;
        _logger = logger;
        _options = options ?? new MetricsTransportOptions();
        _queue = Channel.CreateBounded<Report>(new BoundedChannelOptions(Math.Max(1, _options.Capacity))
        {
            FullMode = BoundedChannelFullMode.Wait, // only TryWrite is used: full => explicit drop
            SingleReader = true,
            SingleWriter = false,
        });
    }

    public long Enqueued => Interlocked.Read(ref _enqueued);
    public long Sent => Interlocked.Read(ref _sent);
    public long DroppedQueueFull => Interlocked.Read(ref _droppedFull);
    public long DroppedShutdown => Interlocked.Read(ref _droppedShutdown);
    public long Failed => Interlocked.Read(ref _failed);
    public long CallerCancelled => Interlocked.Read(ref _cancelled);
    public long Pending => Interlocked.Read(ref _pending);
    internal int MaxObservedConcurrency => Volatile.Read(ref _maxObservedConcurrency);

    /// <summary>UTC enqueue timestamp emitted as reported_at so delayed deliveries keep their true time.</summary>
    public static string ReportedAtNow() => DateTime.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture);

    /// <summary>Admit an inline pipeline report without blocking. Returns false (counted + logged) when not admitted.</summary>
    public bool TryEnqueue(string pipelineId, string batchKey, int processed, int failed, object payload)
        => TryEnqueue("inline", $"/api/pipelines/{Uri.EscapeDataString(pipelineId)}/metrics/inline",
            pipelineId, batchKey, processed, failed, payload);

    /// <summary>Admit any telemetry POST without blocking. Returns false (counted + logged) when not admitted.</summary>
    public bool TryEnqueue(string kind, string path, string scopeId, string batchKey, int processed, int failed, object payload)
    {
        Report report;
        try
        {
            report = new Report(kind, path, JsonSerializer.SerializeToUtf8Bytes(payload, Json),
                scopeId, batchKey, processed, failed);
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref _failed);
            _logger.LogError(ex, "Metrics report ({Kind}) could not be serialized for {ScopeId} batch={BatchKey}; dropped (best-effort telemetry)",
                kind, scopeId, batchKey);
            return false;
        }

        EnsureStarted();
        Interlocked.Increment(ref _pending);
        if (_queue.Writer.TryWrite(report))
        {
            Interlocked.Increment(ref _enqueued);
            return true;
        }
        Interlocked.Decrement(ref _pending);
        var shuttingDown = _abort.IsCancellationRequested || IsWriterCompleted;
        Interlocked.Increment(ref shuttingDown ? ref _droppedShutdown : ref _droppedFull);
        LogDrop(report, shuttingDown ? "transport stopped" : "queue full");
        return false;
    }

    /// <summary>Count a report the caller cancelled before admission.</summary>
    public void RecordCallerCancelled(string pipelineId, string batchKey)
    {
        Interlocked.Increment(ref _cancelled);
        _logger.LogDebug("Metrics report not admitted (caller cancelled) pipeline={PipelineId} batch={BatchKey}", pipelineId, batchKey);
    }

    private volatile bool _writerCompleted;
    private bool IsWriterCompleted => _writerCompleted;

    private void EnsureStarted()
    {
        if (_sender is not null) return;
        lock (_startLock)
            _sender ??= Task.Run(SendLoopAsync);
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        EnsureStarted();
        return Task.CompletedTask;
    }

    /// <summary>Stop admitting reports and drain queued ones within min(DrainTimeout, host stop token).</summary>
    public async Task StopAsync(CancellationToken cancellationToken)
    {
        _writerCompleted = true;
        _queue.Writer.TryComplete();
        EnsureStarted();
        var sender = _sender!;
        using (var drain = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
        {
            drain.CancelAfter(_options.DrainTimeout);
            try { await sender.WaitAsync(drain.Token); }
            catch (OperationCanceledException) { }
        }
        if (!sender.IsCompleted)
        {
            _abort.Cancel();
            try { await sender.WaitAsync(TimeSpan.FromSeconds(2)); }
            catch (TimeoutException)
            {
                _logger.LogError("Metrics sender did not stop after abort; {Pending} reports unaccounted (best-effort telemetry)", Pending);
            }
        }
        _logger.LogInformation(
            "Metrics transport stopped: enqueued={Enqueued} sent={Sent} failed={Failed} dropped_queue_full={DroppedFull} dropped_shutdown={DroppedShutdown} caller_cancelled={Cancelled}",
            Enqueued, Sent, Failed, DroppedQueueFull, DroppedShutdown, CallerCancelled);
    }

    /// <summary>Test/diagnostic helper: wait until all admitted reports are settled.</summary>
    internal async Task<bool> FlushAsync(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (Pending > 0)
        {
            if (DateTime.UtcNow >= deadline) return false;
            await Task.Delay(5);
        }
        return true;
    }

    private async Task SendLoopAsync()
    {
        var abort = _abort.Token;
        try
        {
            while (await _queue.Reader.WaitToReadAsync(abort).ConfigureAwait(false))
            {
                while (_queue.Reader.TryRead(out var report))
                {
                    try { await SendAsync(report, abort).ConfigureAwait(false); }
                    catch (OperationCanceledException) when (abort.IsCancellationRequested)
                    {
                        Interlocked.Increment(ref _droppedShutdown);
                        LogDrop(report, "shutdown drain timeout (in flight)");
                        throw;
                    }
                    finally { Interlocked.Decrement(ref _pending); }
                }
            }
        }
        catch (OperationCanceledException) when (abort.IsCancellationRequested) { }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Metrics sender loop crashed; queued reports will be dropped (best-effort telemetry)");
        }

        var abandoned = 0;
        while (_queue.Reader.TryRead(out _))
        {
            abandoned++;
            Interlocked.Decrement(ref _pending);
        }
        if (abandoned > 0)
        {
            Interlocked.Add(ref _droppedShutdown, abandoned);
            _logger.LogWarning("Metrics transport abandoned {Count} queued reports at shutdown (drain timeout); dashboard counters will under-count",
                abandoned);
        }
    }

    private async Task SendAsync(Report report, CancellationToken abort)
    {
        var attempts = Math.Max(1, _options.MaxAttempts);
        for (var attempt = 1; ; attempt++)
        {
            string outcome;
            Exception? error = null;
            var concurrency = Interlocked.Increment(ref _inFlight);
            if (concurrency > Volatile.Read(ref _maxObservedConcurrency))
                Volatile.Write(ref _maxObservedConcurrency, concurrency);
            try
            {
                using var attemptCts = CancellationTokenSource.CreateLinkedTokenSource(abort);
                attemptCts.CancelAfter(_options.AttemptTimeout);
                using var content = new ByteArrayContent(report.Body);
                content.Headers.ContentType = JsonContentType;
                using var response = await _httpFactory().PostAsync(report.Path, content, attemptCts.Token).ConfigureAwait(false);
                if (response.IsSuccessStatusCode)
                {
                    Interlocked.Increment(ref _sent);
                    return;
                }
                var status = (int)response.StatusCode;
                if (status is >= 400 and < 500 and not 408 and not 429)
                {
                    Interlocked.Increment(ref _failed);
                    _logger.LogWarning(
                        "Metrics report ({Kind}) rejected status={Status} scope={PipelineId} batch={BatchKey} processed={Processed} failed={FailedDocs}; not retried (best-effort telemetry)",
                        report.Kind, response.StatusCode, report.PipelineId, report.BatchKey, report.Processed, report.Failed);
                    return;
                }
                outcome = $"status {(int)response.StatusCode}";
            }
            catch (OperationCanceledException) when (abort.IsCancellationRequested) { throw; }
            catch (OperationCanceledException ex) { outcome = "attempt timeout"; error = ex; }
            catch (Exception ex) { outcome = ex.GetType().Name; error = ex; }
            finally { Interlocked.Decrement(ref _inFlight); }

            if (attempt >= attempts)
            {
                Interlocked.Increment(ref _failed);
                _logger.LogError(error,
                    "Metrics report ({Kind}) FAILED after {Attempts} attempts ({Outcome}) scope={PipelineId} batch={BatchKey} processed={Processed} failed={FailedDocs}; dropped (best-effort telemetry)",
                    report.Kind, attempts, outcome, report.PipelineId, report.BatchKey, report.Processed, report.Failed);
                return;
            }
            var delay = TimeSpan.FromTicks(Math.Min(_options.MaxBackoff.Ticks,
                _options.InitialBackoff.Ticks * (1L << Math.Min(attempt - 1, 20))));
            _logger.LogDebug("Metrics report {Outcome} pipeline={PipelineId} batch={BatchKey} attempt {Attempt}/{Max}; retrying in {Delay}ms",
                outcome, report.PipelineId, report.BatchKey, attempt, attempts, delay.TotalMilliseconds);
            if (delay > TimeSpan.Zero)
                await Task.Delay(delay, abort).ConfigureAwait(false);
        }
    }

    private void LogDrop(Report report, string reason)
    {
        Interlocked.Increment(ref _droppedSinceLog);
        var now = DateTime.UtcNow.Ticks;
        var last = Interlocked.Read(ref _lastDropLogTicks);
        if (now - last < _options.DropLogInterval.Ticks
            || Interlocked.CompareExchange(ref _lastDropLogTicks, now, last) != last)
            return;
        var dropped = Interlocked.Exchange(ref _droppedSinceLog, 0);
        _logger.LogWarning(
            "Metrics report ({Kind}) dropped ({Reason}) scope={PipelineId} batch={BatchKey} processed={Processed} failed={FailedDocs}; {Dropped} dropped since last log (rate-limited), totals queue_full={DroppedFull} shutdown={DroppedShutdown} (best-effort telemetry, capacity={Capacity})",
            report.Kind, reason, report.PipelineId, report.BatchKey, report.Processed, report.Failed, dropped,
            DroppedQueueFull, DroppedShutdown, _options.Capacity);
    }
}

public static class MetricsTransportServiceCollectionExtensions
{
    /// <summary>
    /// Registers the singleton <see cref="MetricsTransport"/> as a hosted service with a
    /// factory-managed named HttpClient. Register BEFORE workers that report so it stops
    /// after them (hosted services stop in reverse order) and drains their final reports.
    /// </summary>
    public static IServiceCollection AddMetricsTransport(this IServiceCollection services,
        IConfiguration configuration, Func<IServiceProvider, string> apiBaseUrl)
    {
        services.Configure<MetricsTransportOptions>(configuration.GetSection(MetricsTransportOptions.SectionName));
        services.AddHttpClient(MetricsTransportOptions.HttpClientName, (sp, client) =>
        {
            var opts = sp.GetRequiredService<IOptions<MetricsTransportOptions>>().Value;
            client.BaseAddress = new Uri(apiBaseUrl(sp));
            client.Timeout = opts.AttemptTimeout + TimeSpan.FromSeconds(1);
        });
        services.AddSingleton(sp =>
        {
            var factory = sp.GetRequiredService<IHttpClientFactory>();
            return new MetricsTransport(() => factory.CreateClient(MetricsTransportOptions.HttpClientName),
                sp.GetRequiredService<ILogger<MetricsTransport>>(),
                sp.GetRequiredService<IOptions<MetricsTransportOptions>>().Value);
        });
        services.AddHostedService(sp => sp.GetRequiredService<MetricsTransport>());
        return services;
    }
}
