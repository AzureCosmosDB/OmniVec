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

    // Lightweight in-process cache for config-shaped listing endpoints.
    // Sources/destinations rarely change → 60s cache. Pipelines carry `reset_at`
    // which the operator can flip via /reset, so cache them only briefly so the
    // CFP reconcile loop notices a reset within a few seconds, not a minute.
    private static readonly TimeSpan SourcesCacheTtl = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan PipelinesCacheTtl = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan DestinationsCacheTtl = TimeSpan.FromSeconds(60);
    private List<Source>? _cachedSources;
    private DateTime _cachedSourcesAt = DateTime.MinValue;
    private List<Pipeline>? _cachedPipelines;
    private DateTime _cachedPipelinesAt = DateTime.MinValue;
    private List<Destination>? _cachedDestinations;
    private DateTime _cachedDestinationsAt = DateTime.MinValue;
    private readonly SemaphoreSlim _sourcesLock = new(1, 1);
    private readonly SemaphoreSlim _pipelinesLock = new(1, 1);
    private readonly SemaphoreSlim _destinationsLock = new(1, 1);

    public OmniVecApiClient(HttpClient http, ILogger<OmniVecApiClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    /// <summary>Force the next list call to skip the cache. Use after operator-triggered changes.</summary>
    public void InvalidateListCaches()
    {
        _cachedSourcesAt = DateTime.MinValue;
        _cachedPipelinesAt = DateTime.MinValue;
        _cachedDestinationsAt = DateTime.MinValue;
    }

    private async Task<List<Source>> FetchSourcesAsync(CancellationToken ct)
    {
        var resp = await _http.GetAsync("/api/sources", ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<SourcesResponse>(cancellationToken: ct);
        return body?.Sources ?? new List<Source>();
    }

    private async Task<List<Source>> GetSourcesCachedAsync(CancellationToken ct)
    {
        if (_cachedSources != null && DateTime.UtcNow - _cachedSourcesAt < SourcesCacheTtl)
            return _cachedSources;
        await _sourcesLock.WaitAsync(ct);
        try
        {
            if (_cachedSources != null && DateTime.UtcNow - _cachedSourcesAt < SourcesCacheTtl)
                return _cachedSources;
            _cachedSources = await FetchSourcesAsync(ct);
            _cachedSourcesAt = DateTime.UtcNow;
            return _cachedSources;
        }
        finally { _sourcesLock.Release(); }
    }

    /// <summary>Get all enabled CosmosDB sources.</summary>
    public async Task<List<Source>> GetCosmosDbSourcesAsync(CancellationToken ct = default)
        => await GetSourcesByTypeAsync("cosmosdb", ct);

    /// <summary>Get all enabled sources of a given type.</summary>
    public async Task<List<Source>> GetSourcesByTypeAsync(string type, CancellationToken ct = default)
    {
        var all = await GetSourcesCachedAsync(ct);
        return all.Where(s => s.Type == type && s.Enabled).ToList();
    }

    /// <summary>Get all enabled sources of the given types.</summary>
    public async Task<List<Source>> GetSourcesByTypesAsync(IEnumerable<string> types, CancellationToken ct = default)
    {
        var all = await GetSourcesCachedAsync(ct);
        var typeSet = types.ToHashSet();
        return all.Where(s => typeSet.Contains(s.Type) && s.Enabled).ToList();
    }

    /// <summary>Get all active pipelines.</summary>
    public async Task<List<Pipeline>> GetActivePipelinesAsync(CancellationToken ct = default)
    {
        if (_cachedPipelines != null && DateTime.UtcNow - _cachedPipelinesAt < PipelinesCacheTtl)
            return _cachedPipelines;
        await _pipelinesLock.WaitAsync(ct);
        try
        {
            if (_cachedPipelines != null && DateTime.UtcNow - _cachedPipelinesAt < PipelinesCacheTtl)
                return _cachedPipelines;
            var resp = await _http.GetAsync("/api/pipelines?include_stats=false", ct);
            resp.EnsureSuccessStatusCode();
            var body = await resp.Content.ReadFromJsonAsync<PipelinesResponse>(cancellationToken: ct);
            _cachedPipelines = body?.Pipelines?.Where(p => p.Status == "active").ToList() ?? new List<Pipeline>();
            _cachedPipelinesAt = DateTime.UtcNow;
            return _cachedPipelines;
        }
        finally { _pipelinesLock.Release(); }
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
    /// (e.g. due to lease rebalancing), the API ignores the duplicate.
    /// Retries on transient failures so the dashboard counter doesn't drift when
    /// the API replica is restarting / a network hiccup occurs. Docs are already
    /// patched in Cosmos by the time we report — only the counter is at risk.</summary>
    public async Task ReportInlineMetricsAsync(
        string pipelineId, int processed, int failed, long processingTimeMs,
        string batchKey, CancellationToken ct = default)
    {
        var payload = new { processed, failed, processing_time_ms = processingTimeMs, batch_key = batchKey };
        const int MaxAttempts = 6;
        for (int attempt = 1; attempt <= MaxAttempts; attempt++)
        {
            try
            {
                using var resp = await _http.PostAsJsonAsync($"/api/pipelines/{pipelineId}/metrics/inline", payload, ct);
                if (resp.IsSuccessStatusCode) return;

                var status = (int)resp.StatusCode;
                // 4xx (except 408/429) is not retryable — payload is wrong, stop trying.
                if (status >= 400 && status < 500 && status != 408 && status != 429)
                {
                    _logger.LogWarning(
                        "Inline metric report rejected for pipeline {PipelineId} status={Status} batch={BatchKey} processed={Processed} — counter will drift",
                        pipelineId, resp.StatusCode, batchKey, processed);
                    return;
                }
                // Retryable: 408/429/5xx
                if (attempt == MaxAttempts)
                {
                    _logger.LogError(
                        "Inline metric report FAILED after {MaxAttempts} attempts for pipeline {PipelineId} status={Status} batch={BatchKey} processed={Processed} — counter will drift",
                        MaxAttempts, pipelineId, resp.StatusCode, batchKey, processed);
                    return;
                }
                var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 15_000));
                _logger.LogWarning(
                    "Inline metric report status={Status} for pipeline {PipelineId} attempt {Attempt}/{Max}, retrying in {Delay}ms",
                    resp.StatusCode, pipelineId, attempt, MaxAttempts, delay.TotalMilliseconds);
                await Task.Delay(delay, ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                if (attempt == MaxAttempts)
                {
                    _logger.LogError(ex,
                        "Inline metric report THREW after {MaxAttempts} attempts for pipeline {PipelineId} batch={BatchKey} processed={Processed} — counter will drift",
                        MaxAttempts, pipelineId, batchKey, processed);
                    return;
                }
                var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 15_000));
                _logger.LogWarning(ex,
                    "Inline metric report transient error for pipeline {PipelineId} attempt {Attempt}/{Max}, retrying in {Delay}ms",
                    pipelineId, attempt, MaxAttempts, delay.TotalMilliseconds);
                await Task.Delay(delay, ct);
            }
        }
    }

    /// <summary>Get all destinations.</summary>
    public async Task<List<Destination>> GetDestinationsAsync(CancellationToken ct = default)
    {
        if (_cachedDestinations != null && DateTime.UtcNow - _cachedDestinationsAt < DestinationsCacheTtl)
            return _cachedDestinations;
        await _destinationsLock.WaitAsync(ct);
        try
        {
            if (_cachedDestinations != null && DateTime.UtcNow - _cachedDestinationsAt < DestinationsCacheTtl)
                return _cachedDestinations;
            var resp = await _http.GetAsync("/api/destinations", ct);
            resp.EnsureSuccessStatusCode();
            var body = await resp.Content.ReadFromJsonAsync<DestinationsResponse>(cancellationToken: ct);
            _cachedDestinations = body?.Destinations?.Where(d => d.Enabled).ToList() ?? new List<Destination>();
            _cachedDestinationsAt = DateTime.UtcNow;
            return _cachedDestinations;
        }
        finally { _destinationsLock.Release(); }
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
