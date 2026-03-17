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
    private readonly ContentHasher _hasher;
    private readonly ServiceBusPublisher _sbPublisher;
    private readonly ILoggerFactory _loggerFactory;
    private readonly ILogger<SourceWatcherManager> _logger;

    private readonly ConcurrentDictionary<string, ISourceWatcher> _watchers = new();

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
        ContentHasher hasher,
        ServiceBusPublisher sbPublisher,
        ILoggerFactory loggerFactory,
        ILogger<SourceWatcherManager> logger)
    {
        _options = options.Value;
        _apiClient = apiClient;
        _leaseManager = leaseManager;
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
        var desiredIds = desiredSources.Select(s => s.Id).ToHashSet();
        var currentIds = _watchers.Keys.ToHashSet();

        // Start watchers for new sources (or restart if generation changed)
        foreach (var source in desiredSources)
        {
            var generation = GetGeneration(source.Id, activePipelines);

            if (currentIds.Contains(source.Id))
            {
                // Check if generation changed — if so, stop old watcher, clear leases, start fresh
                var existing = _watchers[source.Id];
                if (existing.Generation != generation)
                {
                    _logger.LogInformation(
                        "Generation changed for source {SourceId} ({Name}): {Old} → {New}, restarting watcher",
                        source.Id, source.Name, existing.Generation, generation);
                    if (_watchers.TryRemove(source.Id, out var old))
                        await old.DisposeAsync();
                    // Clear leases so the single processorName starts fresh
                    try { await _leaseManager.DeleteLeaseContainerAsync(source.Id, ct); }
                    catch (Exception ex) { _logger.LogWarning(ex, "Could not clear leases for {SourceId}", source.Id); }
                    // Fall through to create a new watcher below
                }
                else
                {
                    continue; // Same generation, nothing to do
                }
            }

            var watcher = CreateWatcher(source, generation);
            watcher.UpdateDestinations(_destinations);

            watcher.UpdatePipelines(activePipelines);

            try
            {
                await watcher.StartAsync(ct);
                _watchers.TryAdd(source.Id, watcher);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to start watcher for source {SourceId} ({Name})",
                    source.Id, source.Name);
                await watcher.DisposeAsync();
            }
        }

        // Stop watchers for removed/disabled sources
        foreach (var sourceId in currentIds.Except(desiredIds))
        {
            if (_watchers.TryRemove(sourceId, out var watcher))
            {
                _logger.LogInformation("Stopping watcher for removed source {SourceId}", sourceId);
                await watcher.DisposeAsync();
            }
        }

        // Update pipeline references on existing watchers
        foreach (var (_, watcher) in _watchers)
        {
            watcher.UpdatePipelines(activePipelines);
        }
    }

    /// <summary>
    /// Reset a watcher by stopping it and restarting with a new generation.
    /// The new processorName causes CFP to create fresh lease docs and replay from the beginning.
    /// Old lease docs are abandoned in-place (harmless, can be cleaned up later).
    /// Called when a pipeline's reset_at changes.
    /// </summary>
    public async Task ResetWatcherAsync(string sourceId, Source source, List<Pipeline> activePipelines, CancellationToken ct)
    {
        var generation = GetGeneration(sourceId, activePipelines);
        _logger.LogInformation("Resetting watcher for source {SourceId} ({Name}), new generation={Generation}",
            sourceId, source.Name, generation);

        // Stop and remove existing watcher
        if (_watchers.TryRemove(sourceId, out var existingWatcher))
        {
            await existingWatcher.DisposeAsync();
        }

        // Delete lease container to clear all checkpoints — forces replay from beginning.
        // This is critical: the processorName is fixed per source, so clearing leases
        // is the only way to force the CFP to start over.
        try
        {
            await _leaseManager.DeleteLeaseContainerAsync(sourceId, ct);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Could not delete lease container for source {SourceId}, continuing anyway", sourceId);
        }

        var watcher = CreateWatcher(source, generation);
        watcher.UpdateDestinations(_destinations);
        watcher.UpdatePipelines(activePipelines);
        watcher.SkipContentHash = true; // After reset, reprocess all docs regardless of hash

        try
        {
            await watcher.StartAsync(ct);
            _watchers.TryAdd(sourceId, watcher);
            _logger.LogInformation("Watcher reset complete for source {SourceId} gen={Generation} — replaying from beginning",
                sourceId, generation);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to restart watcher for source {SourceId} after reset", sourceId);
            await watcher.DisposeAsync();
        }
    }

    /// <summary>
    /// Factory method: creates the right watcher type based on source.Type.
    /// </summary>
    private ISourceWatcher CreateWatcher(Source source, string generation)
    {
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
                sbPublisher: _sbPublisher),

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
    }
}
