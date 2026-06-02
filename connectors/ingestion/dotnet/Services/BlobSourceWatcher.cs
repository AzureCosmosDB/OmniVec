using System.Security.Cryptography;
using System.Text;
using Azure.Identity;
using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Watches an Azure Blob Storage container for PDFs (and other file types).
/// Two phases:
///   1. Prefill — paginated enumeration of existing blobs with backpressure control.
///   2. Live — polls for new/modified blobs since last enumeration.
/// Publishes blob references to Service Bus; DocGrok handles download + chunking + embedding.
/// </summary>
public class BlobSourceWatcher : ISourceWatcher
{
    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ContentHasher _hasher;
    private readonly ServiceBusPublisher? _sbPublisher;
    private readonly ILogger<BlobSourceWatcher> _logger;

    private CancellationTokenSource? _cts;
    private Task? _pollTask;

    private List<Pipeline> _activePipelines = new();
    private readonly object _pipelineLock = new();
    private List<Destination> _destinations = new();

    // Prefill state
    private bool _prefillDone;
    private string? _continuationToken;

    // Live polling state — track last enumeration time
    private DateTimeOffset? _lastPollTime;

    // Track which blobs we've already processed (by name+etag hash) to avoid re-publishing
    private readonly HashSet<string> _processedBlobs = new();

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public BlobSourceWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        ContentHasher hasher,
        ILogger<BlobSourceWatcher> logger,
        string? generation = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _apiClient = apiClient;
        _hasher = hasher;
        // Blob sources depend on Service Bus, but we must not throw here:
        // SourceWatcherManager will detect a disabled publisher and skip CreateWatcher.
        // Throwing would crash the entire ReconcileAsync pass and silently stall discovery.
        _sbPublisher = sbPublisher;
        _logger = logger;
        Generation = generation ?? "0";
    }

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock) { _activePipelines = new List<Pipeline>(pipelines); }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public async Task StartAsync(CancellationToken ct)
    {
        // Blob sources require a working Service Bus publisher. If it's not configured,
        // log a loud warning and stay idle rather than crashing the reconcile loop.
        if (_sbPublisher is null || !_sbPublisher.IsEnabled)
        {
            _logger.LogWarning(
                "Blob source {Source}: Service Bus publisher is not enabled — watcher will stay idle until SB is configured.",
                _source.Name);
            return;
        }

        // Verify connection
        var client = CreateBlobServiceClient();
        var container = client.GetBlobContainerClient(_source.BlobContainer);
        await container.GetPropertiesAsync(cancellationToken: ct);

        _logger.LogInformation(
            "Blob source connected: {Name} container={Container} prefix={Prefix} fileType={FileType}",
            _source.Name, _source.BlobContainer, _source.BlobPrefix, _source.BlobFileType);

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = RunAsync(_cts.Token);

        _logger.LogInformation("Started Blob watcher for {Source} gen={Gen}", _source.Name, Generation);
    }

    private BlobServiceClient CreateBlobServiceClient()
    {
        if (!string.IsNullOrEmpty(_source.BlobConnectionString))
            return new BlobServiceClient(_source.BlobConnectionString);
        if (!string.IsNullOrEmpty(_source.BlobAccountUrl))
            return new BlobServiceClient(new Uri(_source.BlobAccountUrl), new DefaultAzureCredential());
        throw new InvalidOperationException("Blob source requires either connection_string or account_url");
    }

    private async Task RunAsync(CancellationToken ct)
    {
        // Phase 1: Prefill — enumerate all existing blobs with backpressure
        while (!ct.IsCancellationRequested && !_prefillDone)
        {
            try
            {
                // Check backpressure before enumerating more
                if (!await _sbPublisher!.HasCapacityAsync(ct))
                {
                    _logger.LogInformation("Blob prefill paused (backpressure), waiting {Seconds}s",
                        _options.BackpressurePauseSeconds);
                    await Task.Delay(_options.BackpressurePauseSeconds * 1000, ct);
                    continue;
                }

                await EnumeratePageAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Blob prefill error for {Source}, backing off", _source.Name);
                try { await Task.Delay(_options.ErrorBackoffSeconds * 1000, ct); } catch { break; }
            }
        }

        if (_prefillDone)
            _logger.LogInformation("Blob prefill complete for {Source}, switching to live polling", _source.Name);

        // Phase 2: Live polling — check for new/modified blobs
        _lastPollTime = DateTimeOffset.UtcNow;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await PollForNewBlobsAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Blob live poll error for {Source}, backing off", _source.Name);
                try { await Task.Delay(_options.ErrorBackoffSeconds * 1000, ct); } catch { break; }
            }

            await Task.Delay(_options.FeedPollIntervalSeconds * 1000, ct);
        }
    }

    /// <summary>
    /// Enumerate one page of blobs from the container and publish to Service Bus.
    /// </summary>
    private async Task EnumeratePageAsync(CancellationToken ct)
    {
        var client = CreateBlobServiceClient();
        var container = client.GetBlobContainerClient(_source.BlobContainer);
        var prefix = _source.BlobPrefix ?? "";
        var fileExt = "." + (_source.BlobFileType ?? "pdf").TrimStart('.');

        var pageSize = _options.BlobEnumerationPageSize;
        var blobs = new List<BlobItem>();

        // Use AsPages for continuation token support
        var pages = container.GetBlobsAsync(prefix: prefix, cancellationToken: ct)
            .AsPages(_continuationToken, pageSize);

        await foreach (var page in pages)
        {
            foreach (var blob in page.Values)
            {
                // Filter by file type
                if (!blob.Name.EndsWith(fileExt, StringComparison.OrdinalIgnoreCase))
                    continue;

                blobs.Add(blob);
            }

            _continuationToken = page.ContinuationToken;
            break; // Process one page at a time
        }

        if (blobs.Count == 0 && string.IsNullOrEmpty(_continuationToken))
        {
            _prefillDone = true;
            return;
        }

        // Publish blob references to Service Bus
        await PublishBlobMessagesAsync(blobs, ct);

        _logger.LogInformation("Blob prefill page: {Count} blobs published for {Source}",
            blobs.Count, _source.Name);

        if (string.IsNullOrEmpty(_continuationToken))
            _prefillDone = true;
    }

    /// <summary>
    /// Poll for blobs modified since the last check.
    /// </summary>
    private async Task PollForNewBlobsAsync(CancellationToken ct)
    {
        var client = CreateBlobServiceClient();
        var container = client.GetBlobContainerClient(_source.BlobContainer);
        var prefix = _source.BlobPrefix ?? "";
        var fileExt = "." + (_source.BlobFileType ?? "pdf").TrimStart('.');

        var newBlobs = new List<BlobItem>();

        await foreach (var blob in container.GetBlobsAsync(prefix: prefix, cancellationToken: ct))
        {
            if (!blob.Name.EndsWith(fileExt, StringComparison.OrdinalIgnoreCase))
                continue;

            // Check if this blob is new or modified since last poll
            if (blob.Properties.LastModified.HasValue &&
                _lastPollTime.HasValue &&
                blob.Properties.LastModified.Value <= _lastPollTime.Value)
            {
                continue;
            }

            // Check if we've already processed this exact version
            var blobKey = $"{blob.Name}:{blob.Properties.ETag}";
            if (_processedBlobs.Contains(blobKey))
                continue;

            newBlobs.Add(blob);
        }

        if (newBlobs.Count > 0)
        {
            // Check backpressure before publishing
            if (!await _sbPublisher!.HasCapacityAsync(ct))
            {
                _logger.LogWarning("Blob live poll: {Count} new blobs found but backpressure active, will retry",
                    newBlobs.Count);
                return; // Don't update _lastPollTime so we retry next cycle
            }

            await PublishBlobMessagesAsync(newBlobs, ct);
            _logger.LogInformation("Blob live poll: {Count} new blobs published for {Source}",
                newBlobs.Count, _source.Name);
        }

        _lastPollTime = DateTimeOffset.UtcNow;
    }

    /// <summary>
    /// Build EmbeddingMessages with blob references and publish to Service Bus.
    /// </summary>
    private async Task PublishBlobMessagesAsync(List<BlobItem> blobs, CancellationToken ct)
    {
        List<Pipeline> pipelines;
        lock (_pipelineLock) { pipelines = new List<Pipeline>(_activePipelines); }

        var relevantPipelines = pipelines
            .Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id))
            .ToList();
        if (relevantPipelines.Count == 0) return;

        foreach (var pipeline in relevantPipelines)
        {
            var dest = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
            if (dest is null) continue;

            var messages = blobs.Select(blob =>
            {
                var blobKey = $"{blob.Name}:{blob.Properties.ETag}";
                _processedBlobs.Add(blobKey);

                // Content hash is based on blob name + etag (content fingerprint)
                var contentHash = _hasher.ComputeHash(blobKey);

                return new EmbeddingMessage
                {
                    PipelineId = pipeline.Id,
                    PipelineName = pipeline.Name,
                    DocgrokPipeline = pipeline.DocgrokPipeline,
                    SourceId = _source.Id,
                    SourceRef = blob.Name,
                    DestinationId = dest.Id,
                    DestinationType = dest.Type,
                    DestinationConfig = dest.Config,
                    Content = "", // No inline content — DocGrok downloads the blob
                    ContentHash = contentHash,
                    PartitionKeyValue = blob.Name,
                    PipelineGeneration = Generation,
                    StoreContent = pipeline.StoreContent,
                    MetadataFields = pipeline.MetadataFields,
                    ContentType = "blob_ref",
                    BlobAccountUrl = _source.BlobAccountUrl,
                    BlobConnectionString = _source.BlobConnectionString,
                    BlobContainer = _source.BlobContainer,
                    BlobName = blob.Name,
                };
            }).ToList();

            await _sbPublisher!.PublishBatchAsync(messages, ct);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _cts?.Cancel();
        if (_pollTask is not null)
            try { await _pollTask; } catch { }
        _cts?.Dispose();
    }
}
