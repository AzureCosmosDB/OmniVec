using System.Collections.Concurrent;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Orchestrates SourceWatcher instances. Reconciles running watchers against
/// the desired state from the API (which sources are enabled, which pipelines are active).
///
/// Scaling: Multiple instances of this service share partitions via CFP leases.
/// Each instance runs the same set of SourceWatchers, but the CFP SDK automatically
/// distributes partition leases across instances. With N instances watching a source
/// that has P partitions, each instance handles roughly P/N partitions.
/// </summary>
public class SourceWatcherManager : IAsyncDisposable
{
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly LeaseContainerManager _leaseManager;
    private readonly SourceStateContainerManager _stateContainerManager;
    private readonly BlobLeaseManager _blobLeaseManager;
    private readonly ContentHasher _hasher;
    private readonly ServiceBusPublisher _sbPublisher;
    private readonly ILoggerFactory _loggerFactory;
    private readonly ILogger<SourceWatcherManager> _logger;

    private readonly ConcurrentDictionary<string, ISourceWatcher> _watchers = new();
    private readonly ConcurrentDictionary<string, string> _watcherFingerprints = new();

    private sealed record WatcherPlan(
        string Key,
        string LeaseId,
        string StateScopeId,
        Source Source,
        List<Pipeline> Pipelines,
        string Generation,
        PipelineSource? SharePointSource);

    // Cached destinations for passing to watchers
    private List<Destination> _destinations = new();

    public void UpdateDestinations(List<Destination> destinations)
    {
        _destinations = destinations;
        // Propagate to all existing watchers
        foreach (var (_, watcher) in _watchers)
            watcher.UpdateDestinations(destinations);
    }

    public SourceWatcherManager(
        IOptions<ChangeFeedOptions> options,
        OmniVecApiClient apiClient,
        LeaseContainerManager leaseManager,
        SourceStateContainerManager stateContainerManager,
        BlobLeaseManager blobLeaseManager,
        ContentHasher hasher,
        ServiceBusPublisher sbPublisher,
        ILoggerFactory loggerFactory,
        ILogger<SourceWatcherManager> logger)
    {
        _options = options.Value;
        _apiClient = apiClient;
        _leaseManager = leaseManager;
        _stateContainerManager = stateContainerManager;
        _blobLeaseManager = blobLeaseManager;
        _hasher = hasher;
        _sbPublisher = sbPublisher;
        _loggerFactory = loggerFactory;
        _logger = logger;
    }

    /// <summary>
    /// Derive a generation string for a source from its pipelines' reset_at values.
    /// When reset_at changes, the generation changes, causing CFP to use a new
    /// processorName and start fresh from the beginning. Old lease docs are abandoned.
    /// </summary>
    private static string GetGeneration(string sourceId, List<Pipeline> pipelines)
    {
        // Use the latest reset_at across all pipelines that reference this source
        var latest = pipelines
            .Where(p => p.Sources.Any(ps => ps.SourceId == sourceId))
            .Select(p => p.ResetAt ?? "")
            .Where(r => !string.IsNullOrEmpty(r))
            .OrderDescending()
            .FirstOrDefault();

        // No reset_at → generation "0" (initial). Otherwise hash the timestamp for a short stable key.
        if (string.IsNullOrEmpty(latest)) return "0";
        // Use first 8 chars of SHA256 for a short deterministic generation tag
        var bytes = System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(latest));
        return Convert.ToHexString(bytes)[..8].ToLowerInvariant();
    }

    internal static string GetSharePointWatcherKey(string sourceId, string pipelineId)
        => $"{sourceId}::sharepoint::{pipelineId}";

    internal static string GetSharePointStateScopeId(string sourceId, string pipelineId)
    {
        var hash = System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(pipelineId));
        return $"{sourceId}-sp-{Convert.ToHexString(hash)[..16].ToLowerInvariant()}";
    }

    private static List<WatcherPlan> BuildWatcherPlans(
        List<Source> sources,
        List<Pipeline> activePipelines)
    {
        var plans = new List<WatcherPlan>();
        foreach (var source in sources)
        {
            if (!string.Equals(source.Type, "sharepoint", StringComparison.OrdinalIgnoreCase))
            {
                plans.Add(new WatcherPlan(
                    source.Id, source.Id, source.Id, source, activePipelines,
                    GetGeneration(source.Id, activePipelines), null));
                continue;
            }

            var bindings = activePipelines
                .Select(pipeline => (
                    Pipeline: pipeline,
                    Binding: pipeline.Sources.FirstOrDefault(item => item.SourceId == source.Id)))
                .Where(item => item.Binding is not null)
                .Select(item => (item.Pipeline, Binding: item.Binding!))
                .ToList();

            var sameTenantPipelines = bindings
                .Where(item => item.Binding.SharePointIdentity is null)
                .Select(item => item.Pipeline)
                .ToList();
            if (sameTenantPipelines.Count > 0)
            {
                plans.Add(new WatcherPlan(
                    source.Id,
                    source.Id,
                    source.Id,
                    source,
                    sameTenantPipelines,
                    GetGeneration(source.Id, sameTenantPipelines),
                    null));
            }

            foreach (var (pipeline, binding) in bindings.Where(
                         item => item.Binding.SharePointIdentity is not null))
            {
                var key = GetSharePointWatcherKey(source.Id, pipeline.Id);
                plans.Add(new WatcherPlan(
                    key,
                    key,
                    GetSharePointStateScopeId(source.Id, pipeline.Id),
                    source,
                    [pipeline],
                    GetGeneration(source.Id, [pipeline]),
                    binding));
            }
        }
        return plans;
    }

    internal static IReadOnlyList<string> GetDesiredWatcherKeys(
        List<Source> sources,
        List<Pipeline> activePipelines)
        => BuildWatcherPlans(sources, activePipelines).Select(plan => plan.Key).ToList();

    internal static string SourceConfigurationFingerprint(
        Source source, PipelineSource? pipelineSource = null)
    {
        var canonical = new System.Text.StringBuilder(source.Type ?? "");
        foreach (var entry in source.Config.OrderBy(entry => entry.Key, StringComparer.Ordinal))
            canonical.Append('\n').Append(entry.Key).Append('=').Append(entry.Value.GetRawText());
        if (pipelineSource?.SharePointIdentity is not null)
        {
            canonical.Append("\nsharepoint_tenant=")
                .Append(pipelineSource.SharePointIdentity.TenantId)
                .Append("\nsharepoint_client=")
                .Append(pipelineSource.SharePointIdentity.ClientId);
        }
        var hash = System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(canonical.ToString()));
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    /// <summary>
    /// Reconcile running watchers against desired state.
    /// Starts new watchers for added sources, stops watchers for removed/disabled sources,
    /// and updates pipeline references on existing watchers.
    /// Also detects generation changes (from reset_at) and restarts watchers with new processorName.
    /// </summary>
    public async Task ReconcileAsync(
        List<Source> desiredSources,
        List<Pipeline> activePipelines,
        CancellationToken ct)
    {
        var blocked = await RefuseInlineConflictsAsync(desiredSources, activePipelines);
        desiredSources = desiredSources.Where(s => !blocked.Contains(s.Id)).ToList();
        // Filter by source-type toggles so each deployment owns a subset of sources.
        // Blob enumeration runs in its own single-replica deployment; Cosmos CFP scales independently;
        // Databricks Delta CDF runs in its own poll-only deployment.
        desiredSources = desiredSources.Where(s =>
        {
            var t = (s.Type ?? "").ToLowerInvariant();
            if (t == "azure-blob") return _options.EnableBlobSources;
            if (t == "databricks") return _options.EnableDatabricksSources;
            if (t == "sharepoint") return _options.EnableSharePointSources;
            return _options.EnableCosmosSources;
        }).ToList();

        var plans = BuildWatcherPlans(desiredSources, activePipelines);
        var plansByKey = plans.ToDictionary(plan => plan.Key);
        var desiredIds = plansByKey.Keys.ToHashSet();
        var currentIds = _watchers.Keys.ToHashSet();

        // Start watchers for new sources (or restart if generation changed)
        foreach (var plan in plans)
        {
            var source = plan.Source;
            var fingerprint = SourceConfigurationFingerprint(source, plan.SharePointSource);
            // Every polling source needs one owner; only Cosmos CFP distributes
            // its own partition leases. This also applies during explicit resets.
            bool isSharePoint = string.Equals(source.Type, "sharepoint", StringComparison.OrdinalIgnoreCase);
            var generation = plan.Generation;
            var replayFromBeginning = false;
            if (RequiresPollingLease(source.Type))
            {
                var haveLease = await _blobLeaseManager.TryAcquireAsync(plan.LeaseId, ct);
                if (!haveLease)
                {
                    // Another pod owns this source. If we were running it, stop.
                    if (_watchers.TryRemove(plan.Key, out var old))
                    {
                        _watcherFingerprints.TryRemove(plan.Key, out _);
                        _logger.LogInformation(
                            "Lost lease for watcher {WatcherId} source={SourceId} ({Type}), stopping local watcher",
                            plan.Key, source.Id, source.Type);
                        await old.DisposeAsync();
                    }
                    continue;
                }
            }

            if (_watchers.TryGetValue(plan.Key, out var existing))
            {
                var configChanged = _watcherFingerprints.TryGetValue(plan.Key, out var currentFingerprint)
                    && currentFingerprint != fingerprint;
                var generationChanged = existing.Generation != generation;
                // A new processor name starts from the beginning without privileged lease-container deletion.
                if (generationChanged || configChanged)
                {
                    _logger.LogInformation(
                        "Watcher configuration changed for {WatcherId} source={SourceId} ({Name}): generation {Old} → {New}, configChanged={ConfigChanged}; restarting",
                        plan.Key, source.Id, source.Name, existing.Generation, generation, configChanged);
                    if (_watchers.TryRemove(plan.Key, out var old))
                    {
                        _watcherFingerprints.TryRemove(plan.Key, out _);
                        await old.DisposeAsync();
                    }
                    replayFromBeginning = generationChanged && !isSharePoint;
                    // Fall through to create a new watcher below
                }
                else
                {
                    _watcherFingerprints.TryAdd(plan.Key, fingerprint);
                    continue; // Same generation, nothing to do
                }
            }

            ISourceWatcher? watcher = null;
            try
            {
                watcher = CreateWatcher(plan);
                watcher.UpdateDestinations(_destinations);
                watcher.UpdatePipelines(plan.Pipelines);
                watcher.SkipContentHash = replayFromBeginning;
                await watcher.StartAsync(ct);
                _watchers.TryAdd(plan.Key, watcher);
                _watcherFingerprints[plan.Key] = fingerprint;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to start watcher for source {SourceId} ({Name})",
                    source.Id, source.Name);
                if (watcher is not null) await watcher.DisposeAsync();
            }
        }

        // Stop watchers for removed/disabled sources
        foreach (var sourceId in currentIds.Except(desiredIds))
        {
            if (_watchers.TryRemove(sourceId, out var watcher))
            {
                _watcherFingerprints.TryRemove(sourceId, out _);
                _logger.LogInformation("Stopping watcher for removed source {SourceId}", sourceId);
                await watcher.DisposeAsync();
            }
        }

        // Update pipeline references on existing watchers
        foreach (var (key, watcher) in _watchers)
        {
            watcher.UpdateDestinations(_destinations);
            if (plansByKey.TryGetValue(key, out var plan))
                watcher.UpdatePipelines(plan.Pipelines);
        }
    }

    /// <summary>
    /// Reset a watcher by stopping it and restarting with a new generation.
    /// The new processorName causes CFP to create fresh lease docs and replay from the beginning.
    /// Old lease docs are abandoned in-place (harmless, can be cleaned up later).
    /// Called when a pipeline's reset_at changes.
    /// </summary>
    public async Task ResetWatcherAsync(string sourceId, Source source, List<Pipeline> activePipelines,
        CancellationToken ct, List<Source>? knownSources = null)
    {
        knownSources ??= await _apiClient.GetSourcesByTypesAsync(
            new[] { "cosmosdb", "mssql", "postgresql", "azure-blob", "databricks", "sharepoint" }, ct);
        var blocked = await RefuseInlineConflictsAsync(knownSources, activePipelines);
        if (blocked.Contains(sourceId)) return;
        if (string.Equals(source.Type, "sharepoint", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var entry in _watchers.Where(entry => entry.Value.SourceId == sourceId).ToList())
            {
                if (_watchers.TryRemove(entry.Key, out var removedWatcher))
                {
                    _watcherFingerprints.TryRemove(entry.Key, out _);
                    await removedWatcher.DisposeAsync();
                }
            }
            await ReconcileAsync(knownSources, activePipelines, ct);
            return;
        }
        var generation = GetGeneration(sourceId, activePipelines);
        _logger.LogInformation("Resetting watcher for source {SourceId} ({Name}), new generation={Generation}",
            sourceId, source.Name, generation);

        // Stop and remove existing watcher
        if (_watchers.TryRemove(sourceId, out var existingWatcher))
        {
            _watcherFingerprints.TryRemove(sourceId, out _);
            await existingWatcher.DisposeAsync();
        }
        if (RequiresPollingLease(source.Type) && !await _blobLeaseManager.TryAcquireAsync(sourceId, ct))
        {
            _logger.LogInformation("Source {SourceId} reset will be handled by its polling lease owner", sourceId);
            return;
        }

        ISourceWatcher? watcher = null;
        try
        {
            watcher = CreateWatcher(new WatcherPlan(
                sourceId, sourceId, sourceId, source, activePipelines, generation, null));
            watcher.UpdateDestinations(_destinations);
            watcher.UpdatePipelines(activePipelines);
            watcher.SkipContentHash = true;
            await watcher.StartAsync(ct);
            _watchers.TryAdd(sourceId, watcher);
            _watcherFingerprints[sourceId] = SourceConfigurationFingerprint(source);
            _logger.LogInformation("Watcher reset complete for source {SourceId} gen={Generation} — replaying from beginning",
                sourceId, generation);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to restart watcher for source {SourceId} after reset", sourceId);
            if (watcher is not null) await watcher.DisposeAsync();
        }
    }

    private async Task<HashSet<string>> RefuseInlineConflictsAsync(List<Source> sources, List<Pipeline> pipelines)
    {
        var blocked = InlineSourceOwnership.FindBlockedSourceIds(sources, pipelines);
        foreach (var sourceId in blocked)
        {
            _logger.LogError(
                "Refusing source {SourceId}: inline metadata ownership conflicts or cannot be determined. " +
                "Pause competing inline pipelines or repair source configuration; checkpoints are retained.", sourceId);
            foreach (var entry in _watchers.Where(entry => entry.Value.SourceId == sourceId).ToList())
            {
                if (_watchers.TryRemove(entry.Key, out var watcher))
                {
                    _watcherFingerprints.TryRemove(entry.Key, out _);
                    await watcher.DisposeAsync();
                }
            }
        }
        return blocked;
    }

    internal static bool RequiresPollingLease(string? type)
        => type?.ToLowerInvariant() is "azure-blob" or "databricks" or "sharepoint" or "mssql" or "postgresql";

    private ISourceWatcher CreateWatcher(WatcherPlan plan)
    {
        var source = plan.Source;
        var generation = plan.Generation;
        return source.Type?.ToLowerInvariant() switch
        {
            "mssql" => new MsSqlCdcWatcher(
                source, _options, _apiClient, _hasher,
                _loggerFactory.CreateLogger<MsSqlCdcWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher),

            "postgresql" => new PostgresCdcWatcher(
                source, _options, _apiClient, _hasher,
                _loggerFactory.CreateLogger<PostgresCdcWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher),

            "azure-blob" => new BlobSourceWatcher(
                source, _options, _apiClient, _hasher,
                _loggerFactory.CreateLogger<BlobSourceWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher.IsEnabled ? _sbPublisher : null),

            "databricks" => new DatabricksCdcWatcher(
                source, _options, _apiClient, _hasher,
                _loggerFactory.CreateLogger<DatabricksCdcWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher),

            "sharepoint" => new SharePointSourceWatcher(
                source, _options, _stateContainerManager, _hasher,
                _loggerFactory.CreateLogger<SharePointSourceWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher,
                pipelineSource: plan.SharePointSource,
                stateScopeId: plan.StateScopeId),

            _ => new SourceWatcher(
                source, _options, _apiClient, _leaseManager, _hasher,
                _loggerFactory.CreateLogger<SourceWatcher>(),
                generation: generation,
                sbPublisher: _sbPublisher),
        };
    }

    public int ActiveWatcherCount => _watchers.Count;

    public async ValueTask DisposeAsync()
    {
        _logger.LogInformation("Disposing all watchers");
        foreach (var (_, watcher) in _watchers)
        {
            await watcher.DisposeAsync();
        }
        _watchers.Clear();
        _watcherFingerprints.Clear();
    }
}
