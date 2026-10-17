namespace OmniVec.Worker.Services;

/// <summary>
/// Best-effort inline-metrics reporter. ReportInlineMetricsAsync only admits the report
/// into the bounded <see cref="MetricsTransport"/> queue and returns a completed task;
/// delivery (bounded timeout/retries) happens on the transport's single background
/// sender. Telemetry loss is counted and logged by the transport, never thrown.
/// </summary>
public class MetricsReporter
{
    private readonly MetricsTransport _transport;
    private readonly ILogger<MetricsReporter> _logger;

    public MetricsReporter(MetricsTransport transport, ILogger<MetricsReporter> logger)
    {
        _transport = transport;
        _logger = logger;
    }

    /// <summary>Test convenience: private transport over a caller-owned HttpClient.</summary>
    internal MetricsReporter(HttpClient http, ILogger<MetricsReporter> logger, MetricsTransportOptions? options = null)
        : this(new MetricsTransport(() => http, logger, options), logger) { }

    internal MetricsTransport Transport => _transport;

    public Task ReportInlineMetricsAsync(
        string pipelineId, int processed, int failed, long processingTimeMs,
        string batchKey, CancellationToken ct = default, string sourceId = "",
        string destinationId = "", string modelId = "", long tokensUsed = 0,
        long inputBytes = 0, double queueWaitMs = 0, int retryCount = 0,
        int throttleCount = 0, double throttleDelayMs = 0,
        string errorCategory = "", string lastDocument = "",
        int resourceWeight = 10, int maxConcurrencyPerWorker = 2,
        string resourcePriority = "normal", string workloadClass = "shared")
    {
        if (ct.IsCancellationRequested)
        {
            _transport.RecordCallerCancelled(pipelineId, batchKey);
            return Task.FromCanceled(ct);
        }
        try
        {
            var payload = new
            {
                processed,
                failed,
                processing_time_ms = processingTimeMs,
                batch_key = batchKey,
                source_id = sourceId,
                destination_id = destinationId,
                model_id = modelId,
                tokens_used = tokensUsed,
                input_bytes = inputBytes,
                queue_wait_ms = queueWaitMs,
                retry_count = retryCount,
                throttle_count = throttleCount,
                throttle_delay_ms = throttleDelayMs,
                error_category = errorCategory,
                last_document = lastDocument,
                resource_weight = resourceWeight,
                max_concurrency_per_worker = maxConcurrencyPerWorker,
                resource_priority = resourcePriority,
                workload_class = workloadClass,
                reported_at = MetricsTransport.ReportedAtNow(),
            };
            _transport.TryEnqueue(pipelineId, batchKey, processed, failed, payload);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to enqueue metrics for pipeline {PipelineId} (best-effort telemetry)", pipelineId);
        }
        return Task.CompletedTask;
    }
}
