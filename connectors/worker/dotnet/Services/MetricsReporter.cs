using System.Net.Http.Json;

namespace OmniVec.Worker.Services;

public class MetricsReporter
{
    private readonly HttpClient _http;
    private readonly ILogger<MetricsReporter> _logger;

    public MetricsReporter(HttpClient http, ILogger<MetricsReporter> logger)
    {
        _http = http;
        _logger = logger;
    }

    public async Task ReportInlineMetricsAsync(
        string pipelineId, int processed, int failed, long processingTimeMs,
        string batchKey, CancellationToken ct = default, string sourceId = "",
        string destinationId = "", string modelId = "", long tokensUsed = 0,
        long inputBytes = 0, double queueWaitMs = 0, int retryCount = 0,
        int throttleCount = 0, double throttleDelayMs = 0,
        string errorCategory = "", string lastDocument = "",
        int resourceWeight = 10, int maxConcurrencyPerWorker = 2,
        string resourcePriority = "normal", string workloadClass = "shared")
    {
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
            };
            using var response = await _http.PostAsJsonAsync($"/api/pipelines/{pipelineId}/metrics/inline", payload, ct);
            response.EnsureSuccessStatusCode();
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to report metrics for pipeline {PipelineId}", pipelineId);
        }
    }
}
