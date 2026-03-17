using System.Net.Http.Json;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// HTTP client for the OmniVec control plane API.
/// Used to discover sources/pipelines and create jobs.
/// </summary>
public class OmniVecApiClient
{
    private readonly HttpClient _http;
    private readonly ILogger<OmniVecApiClient> _logger;

    public OmniVecApiClient(HttpClient http, ILogger<OmniVecApiClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    /// <summary>Get all enabled CosmosDB sources.</summary>
    public async Task<List<Source>> GetCosmosDbSourcesAsync(CancellationToken ct = default)
        => await GetSourcesByTypeAsync("cosmosdb", ct);

    /// <summary>Get all enabled sources of a given type.</summary>
    public async Task<List<Source>> GetSourcesByTypeAsync(string type, CancellationToken ct = default)
    {
        var resp = await _http.GetAsync("/api/sources", ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<SourcesResponse>(cancellationToken: ct);
        return body?.Sources?
            .Where(s => s.Type == type && s.Enabled)
            .ToList() ?? new List<Source>();
    }

    /// <summary>Get all enabled sources of the given types.</summary>
    public async Task<List<Source>> GetSourcesByTypesAsync(IEnumerable<string> types, CancellationToken ct = default)
    {
        var resp = await _http.GetAsync("/api/sources", ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<SourcesResponse>(cancellationToken: ct);
        var typeSet = types.ToHashSet();
        return body?.Sources?
            .Where(s => typeSet.Contains(s.Type) && s.Enabled)
            .ToList() ?? new List<Source>();
    }

    /// <summary>Get all active pipelines.</summary>
    public async Task<List<Pipeline>> GetActivePipelinesAsync(CancellationToken ct = default)
    {
        var resp = await _http.GetAsync("/api/pipelines", ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<PipelinesResponse>(cancellationToken: ct);
        return body?.Pipelines?
            .Where(p => p.Status == "active")
            .ToList() ?? new List<Pipeline>();
    }

    /// <summary>Create jobs in bulk. Returns (created, skipped).</summary>
    public async Task<(int created, int skipped)> CreateJobsBulkAsync(
        CreateJobsRequest request, CancellationToken ct = default)
    {
        var resp = await _http.PostAsJsonAsync("/api/jobs/bulk", request, ct);
        resp.EnsureSuccessStatusCode();
        var result = await resp.Content.ReadFromJsonAsync<CreateJobsResponse>(cancellationToken: ct);
        return (result?.Created ?? 0, result?.Skipped ?? 0);
    }

    /// <summary>Report inline processing metrics for a pipeline.
    /// batch_key is used for deduplication — if the same batch_key is reported twice
    /// (e.g. due to lease rebalancing), the API ignores the duplicate.</summary>
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
            _logger.LogWarning(ex, "Failed to report inline metrics for pipeline {PipelineId}", pipelineId);
        }
    }

    /// <summary>Get all destinations.</summary>
    public async Task<List<Destination>> GetDestinationsAsync(CancellationToken ct = default)
    {
        var resp = await _http.GetAsync("/api/destinations", ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<DestinationsResponse>(cancellationToken: ct);
        return body?.Destinations?.Where(d => d.Enabled).ToList() ?? new List<Destination>();
    }

    /// <summary>Report changefeed batch metrics including skip counts.</summary>
    public async Task ReportChangeFeedMetricsAsync(
        string sourceId, int total, int eligible, int skippedNoContent, int skippedUnchanged,
        int jobsCreated, string partition, CancellationToken ct = default)
    {
        try
        {
            var payload = new
            {
                source_id = sourceId,
                total,
                eligible,
                skipped_no_content = skippedNoContent,
                skipped_unchanged = skippedUnchanged,
                jobs_created = jobsCreated,
                partition
            };
            await _http.PostAsJsonAsync("/api/metrics/changefeed", payload, ct);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to report changefeed metrics for source {SourceId}", sourceId);
        }
    }
}
