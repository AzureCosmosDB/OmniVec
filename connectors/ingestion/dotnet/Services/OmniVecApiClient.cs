using System.Net.Http.Json;
using OmniVec.ChangeFeed.Models;
using OmniVec.Worker.Services;

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

    private readonly MetricsTransport _metrics;

    public OmniVecApiClient(HttpClient http, ILogger<OmniVecApiClient> logger, MetricsTransport metrics)
    {
        _http = http;
        _logger = logger;
        _metrics = metrics;
    }

    /// <summary>Test convenience: private metrics transport over the same caller-owned HttpClient.</summary>
    internal OmniVecApiClient(HttpClient http, ILogger<OmniVecApiClient> logger, MetricsTransportOptions? metricsOptions = null)
        : this(http, logger, new MetricsTransport(() => http, logger, metricsOptions)) { }

    internal MetricsTransport MetricsTransport => _metrics;

    /// <summary>Force the next list call to skip the cache. Use after operator-triggered changes.</summary>
    public void InvalidateListCaches()
    {
        _cachedSourcesAt = DateTime.MinValue;
        _cachedPipelinesAt = DateTime.MinValue;
        _cachedDestinationsAt = DateTime.MinValue;
    }

    private async Task<List<Source>> FetchSourcesAsync(CancellationToken ct)
    {
        using var resp = await GetWithRetryAsync("/api/sources", ct);
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
            using var resp = await GetWithRetryAsync("/api/pipelines?include_stats=false", ct);
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

    /// <summary>Report inline processing metrics for a pipeline (best-effort telemetry).
    /// Non-blocking: the report is admitted into the bounded singleton
    /// <see cref="MetricsTransport"/> and this returns a completed task; the transport's
    /// background sender posts it with bounded timeout/retries, keeping the same
    /// batch_key across retries so the control plane can deduplicate. Queue-full,
    /// rejected, exhausted and shutdown-abandoned reports are counted and logged by
    /// the transport — docs are already written, only the counter is at risk.
    /// A caller token that is already cancelled propagates as a cancelled task.</summary>
    public Task ReportInlineMetricsAsync(
        string pipelineId, int processed, int failed, long processingTimeMs,
        string batchKey, CancellationToken ct = default)
    {
        if (ct.IsCancellationRequested)
        {
            _metrics.RecordCallerCancelled(pipelineId, batchKey);
            return Task.FromCanceled(ct);
        }
        try
        {
            var payload = new { processed, failed, processing_time_ms = processingTimeMs, batch_key = batchKey, reported_at = MetricsTransport.ReportedAtNow() };
            _metrics.TryEnqueue(pipelineId, batchKey, processed, failed, payload);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to enqueue inline metrics for pipeline {PipelineId} (best-effort telemetry)", pipelineId);
        }
        return Task.CompletedTask;
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
            using var resp = await GetWithRetryAsync("/api/destinations", ct);
            resp.EnsureSuccessStatusCode();
            var body = await resp.Content.ReadFromJsonAsync<DestinationsResponse>(cancellationToken: ct);
            _cachedDestinations = body?.Destinations?.Where(d => d.Enabled).ToList() ?? new List<Destination>();
            _cachedDestinationsAt = DateTime.UtcNow;
            return _cachedDestinations;
        }
        finally { _destinationsLock.Release(); }
    }

    private async Task<HttpResponseMessage> GetWithRetryAsync(string path, CancellationToken ct)
    {
        const int maxAttempts = 4;
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                var response = await _http.GetAsync(path, ct);
                var status = (int)response.StatusCode;
                if (response.IsSuccessStatusCode
                    || (status < 500 && status is not (408 or 429))
                    || attempt == maxAttempts)
                    return response;
                response.Dispose();
            }
            catch (Exception ex) when (
                attempt < maxAttempts
                && !ct.IsCancellationRequested
                && ex is HttpRequestException or TaskCanceledException)
            {
                _logger.LogWarning(ex,
                    "Transient API GET failure for {Path}, attempt {Attempt}/{Max}",
                    path, attempt, maxAttempts);
            }
            await Task.Delay(TimeSpan.FromMilliseconds(250 * Math.Pow(2, attempt - 1)), ct);
        }
    }

    /// <summary>Report changefeed batch metrics including skip counts (best-effort telemetry).
    /// Non-blocking: admitted into the bounded singleton <see cref="MetricsTransport"/> and
    /// returns a completed task. A per-report batch_key (stable across transport retries)
    /// and reported_at (enqueue time, UTC) are included for downstream deduplication.</summary>
    public Task ReportChangeFeedMetricsAsync(
        string sourceId, int total, int eligible, int skippedNoContent, int skippedUnchanged,
        int jobsCreated, string partition, CancellationToken ct = default)
    {
        var batchKey = $"changefeed:{sourceId}:{partition}:{Guid.NewGuid():N}";
        if (ct.IsCancellationRequested)
        {
            _metrics.RecordCallerCancelled(sourceId, batchKey);
            return Task.FromCanceled(ct);
        }
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
                partition,
                batch_key = batchKey,
                reported_at = MetricsTransport.ReportedAtNow(),
            };
            _metrics.TryEnqueue("changefeed", "/api/metrics/changefeed", sourceId, batchKey, total, 0, payload);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to enqueue changefeed metrics for source {SourceId} (best-effort telemetry)", sourceId);
        }
        return Task.CompletedTask;
    }
}
