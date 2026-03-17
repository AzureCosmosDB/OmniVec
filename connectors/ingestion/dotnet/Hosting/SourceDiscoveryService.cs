using System.Collections.Concurrent;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using OmniVec.ChangeFeed.Services;

namespace OmniVec.ChangeFeed.Hosting;

/// <summary>
/// Background service that polls the OmniVec API for source/pipeline configuration
/// and reconciles SourceWatchers accordingly.
///
/// Scaling behavior: Every instance of this service runs the same reconciliation loop,
/// creating the same set of SourceWatchers. The CFP SDK handles partition distribution
/// across instances via the shared lease container — no coordination needed here.
///
/// Reset detection: Tracks each pipeline's reset_at timestamp. When it changes,
/// stops the watcher, deletes the lease container, and restarts from the beginning.
/// </summary>
public class SourceDiscoveryService : BackgroundService
{
    private readonly OmniVecApiClient _apiClient;
    private readonly SourceWatcherManager _watcherManager;
    private readonly ChangeFeedOptions _options;
    private readonly ILogger<SourceDiscoveryService> _logger;

    // Track last-seen reset_at per pipeline to detect resets
    private readonly ConcurrentDictionary<string, string> _lastResetAt = new();
    private bool _initialLoadDone;

    public SourceDiscoveryService(
        OmniVecApiClient apiClient,
        SourceWatcherManager watcherManager,
        IOptions<ChangeFeedOptions> options,
        ILogger<SourceDiscoveryService> logger)
    {
        _apiClient = apiClient;
        _watcherManager = watcherManager;
        _options = options.Value;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        _logger.LogInformation(
            "Source discovery started (instance={Instance}, poll={Interval}s)",
            _options.InstanceName, _options.SourcePollIntervalSeconds);

        // Initial delay to let API come up
        await Task.Delay(TimeSpan.FromSeconds(5), ct);

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var sources = await _apiClient.GetSourcesByTypesAsync(
                    new[] { "cosmosdb", "mssql", "postgresql", "azure-blob" }, ct);
                var pipelines = await _apiClient.GetActivePipelinesAsync(ct);
                var destinations = await _apiClient.GetDestinationsAsync(ct);

                // Push destinations to watcher manager so watchers can build self-contained SB messages
                _watcherManager.UpdateDestinations(destinations);

                // Only watch sources that are referenced by at least one active pipeline
                var neededSourceIds = pipelines
                    .SelectMany(p => p.Sources)
                    .Select(ps => ps.SourceId)
                    .ToHashSet();

                var relevantSources = sources
                    .Where(s => neededSourceIds.Contains(s.Id))
                    .ToList();

                // Detect pipeline resets — if reset_at changed, reset affected source watchers
                var sourcesToReset = DetectResets(pipelines);
                if (sourcesToReset.Count > 0)
                {
                    var sourceMap = relevantSources.ToDictionary(s => s.Id);
                    foreach (var sourceId in sourcesToReset)
                    {
                        if (sourceMap.TryGetValue(sourceId, out var source))
                        {
                            await _watcherManager.ResetWatcherAsync(sourceId, source, pipelines, ct);
                        }
                    }
                }

                await _watcherManager.ReconcileAsync(relevantSources, pipelines, ct);

                _logger.LogDebug(
                    "Reconciled: {Sources} sources, {Pipelines} pipelines, {Watchers} watchers",
                    relevantSources.Count, pipelines.Count, _watcherManager.ActiveWatcherCount);
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                _logger.LogError(ex, "Source discovery error, backing off {Seconds}s",
                    _options.ErrorBackoffSeconds);
                await Task.Delay(TimeSpan.FromSeconds(_options.ErrorBackoffSeconds), ct);
                continue;
            }

            await Task.Delay(TimeSpan.FromSeconds(_options.SourcePollIntervalSeconds), ct);
        }
    }

    /// <summary>
    /// Compare each pipeline's reset_at with last known value.
    /// Returns the set of source IDs that need their watchers reset.
    /// On first poll after startup, just stores values without triggering resets.
    /// </summary>
    private HashSet<string> DetectResets(List<Pipeline> pipelines)
    {
        var sourcesToReset = new HashSet<string>();

        foreach (var pipeline in pipelines)
        {
            var currentResetAt = pipeline.ResetAt ?? "";

            if (!_initialLoadDone)
            {
                // First poll — just store current state, don't trigger resets
                _lastResetAt[pipeline.Id] = currentResetAt;
                continue;
            }

            var previousResetAt = _lastResetAt.GetOrAdd(pipeline.Id, "");

            if (currentResetAt != previousResetAt && !string.IsNullOrEmpty(currentResetAt))
            {
                _logger.LogInformation(
                    "Pipeline {PipelineId} ({Name}) reset detected: {Old} → {New}",
                    pipeline.Id, pipeline.Name, previousResetAt, currentResetAt);

                // Mark all sources in this pipeline for reset
                foreach (var ps in pipeline.Sources)
                {
                    sourcesToReset.Add(ps.SourceId);
                }
            }

            _lastResetAt[pipeline.Id] = currentResetAt;
        }

        if (!_initialLoadDone)
        {
            _initialLoadDone = true;
            _logger.LogInformation("Initial reset_at snapshot captured for {Count} pipelines", pipelines.Count);
        }

        return sourcesToReset;
    }
}
