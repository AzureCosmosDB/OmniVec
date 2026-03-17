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
        string batchKey, CancellationToken ct = default)
    {
        try
        {
            var payload = new { processed, failed, processing_time_ms = processingTimeMs, batch_key = batchKey };
            await _http.PostAsJsonAsync($"/api/pipelines/{pipelineId}/metrics/inline", payload, ct);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to report metrics for pipeline {PipelineId}", pipelineId);
        }
    }
}
