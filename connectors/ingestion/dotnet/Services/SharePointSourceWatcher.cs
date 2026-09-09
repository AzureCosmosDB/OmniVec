using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;
using Microsoft.Azure.Cosmos;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Polls a SharePoint Online document library through Microsoft Graph delta queries.
/// Queue messages contain only stable Graph identifiers; the worker downloads content.
/// </summary>
public sealed class SharePointSourceWatcher : ISourceWatcher
{
    private static readonly string[] GraphScopes = ["https://graph.microsoft.com/.default"];

    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly LeaseContainerManager _stateStore;
    private readonly ContentHasher _hasher;
    private readonly ServiceBusPublisher _sbPublisher;
    private readonly ILogger<SharePointSourceWatcher> _logger;
    private readonly DefaultAzureCredential _credential = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(60) };
    private readonly Dictionary<string, string> _knownRefs = new(StringComparer.Ordinal);
    private readonly object _pipelineLock = new();

    private List<Pipeline> _activePipelines = new();
    private HashSet<string> _pipelineIds = new(StringComparer.Ordinal);
    private bool _pipelineStateInitialized;
    private bool _resetDeltaForNewPipeline;
    private List<Destination> _destinations = new();
    private CancellationTokenSource? _cts;
    private Task? _pollTask;
    private Container? _stateContainer;
    private string? _deltaUrl;
    private SharePointStateDocument _state = new();
    private string? _stateEtag;

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public SharePointSourceWatcher(
        Source source,
        ChangeFeedOptions options,
        LeaseContainerManager stateStore,
        ContentHasher hasher,
        ILogger<SharePointSourceWatcher> logger,
        string? generation = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _stateStore = stateStore;
        _hasher = hasher;
        _logger = logger;
        _sbPublisher = sbPublisher
            ?? throw new InvalidOperationException("SharePoint sources require Service Bus");
        Generation = generation ?? "0";
    }

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock)
        {
            _activePipelines = new List<Pipeline>(pipelines);
            var nextPipelineIds = pipelines
                .Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id))
                .Select(p => p.Id)
                .ToHashSet(StringComparer.Ordinal);
            if (_pipelineStateInitialized && nextPipelineIds.Except(_pipelineIds).Any())
                _resetDeltaForNewPipeline = true;
            _pipelineIds = nextPipelineIds;
        }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public async Task StartAsync(CancellationToken ct)
    {
        if (!_sbPublisher.IsEnabled)
            throw new InvalidOperationException("SharePoint sources require an enabled Service Bus publisher");
        if (string.IsNullOrWhiteSpace(_source.SharePointSiteId)
            || string.IsNullOrWhiteSpace(_source.SharePointDriveId))
            throw new InvalidOperationException("SharePoint source requires site_id and drive_id");

        _stateContainer = await _stateStore.EnsureLeaseContainerAsync(_source.Id, ct);
        await LoadStateAsync(ct);
        _deltaUrl ??= BuildInitialDeltaUrl();
        await ValidateDriveAsync(ct);

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = RunAsync(_cts.Token);
        _logger.LogInformation(
            "Started SharePoint watcher for {Source} site={SiteId} drive={DriveId} folder={Folder}",
            _source.Name, _source.SharePointSiteId, _source.SharePointDriveId,
            _source.SharePointFolderPath);
    }

    private string BuildInitialDeltaUrl()
    {
        var siteId = Uri.EscapeDataString(_source.SharePointSiteId!);
        var driveId = Uri.EscapeDataString(_source.SharePointDriveId!);
        var folder = _source.SharePointFolderPath.Trim().Trim('/');
        if (string.IsNullOrEmpty(folder))
            return $"https://graph.microsoft.com/v1.0/sites/{siteId}/drives/{driveId}/root/delta";

        var encodedFolder = string.Join('/',
            folder.Split('/', StringSplitOptions.RemoveEmptyEntries).Select(Uri.EscapeDataString));
        return $"https://graph.microsoft.com/v1.0/sites/{siteId}/drives/{driveId}/root:/{encodedFolder}:/delta";
    }

    private async Task ValidateDriveAsync(CancellationToken ct)
    {
        var siteId = Uri.EscapeDataString(_source.SharePointSiteId!);
        var driveId = Uri.EscapeDataString(_source.SharePointDriveId!);
        using var response = await GraphGetAsync(
            $"https://graph.microsoft.com/v1.0/sites/{siteId}/drives/{driveId}", ct);
        response.EnsureSuccessStatusCode();
    }

    private async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (!await _sbPublisher.HasCapacityAsync(ct))
                {
                    await Task.Delay(TimeSpan.FromSeconds(_options.BackpressurePauseSeconds), ct);
                    continue;
                }

                await PollDeltaAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "SharePoint delta poll failed for {Source}", _source.Name);
                await Task.Delay(TimeSpan.FromSeconds(_options.ErrorBackoffSeconds), ct);
            }

            await Task.Delay(
                TimeSpan.FromSeconds(Math.Max(10, _source.SharePointPollIntervalSeconds)), ct);
        }
    }

    private async Task PollDeltaAsync(CancellationToken ct)
    {
        // Reload after every failure, including an ambiguous state-write result.
        // A persisted outbox is always replayed before reading another Graph page.
        await LoadStateAsync(ct);
        if (!string.IsNullOrEmpty(_state.PendingJson))
            await FlushPendingAsync(ct);
        if (_state.Reconcile)
            await ReconcileScanAsync(ct);
        bool reset;
        lock (_pipelineLock)
        {
            reset = _resetDeltaForNewPipeline || _state.SchemaVersion < 2 || string.IsNullOrEmpty(_deltaUrl)
                || _state.Generation != Generation || SkipContentHash;
            _resetDeltaForNewPipeline = false;
        }
        if (reset)
        {
            await StartScanAsync(ct);
            SkipContentHash = false;
        }
        var resetExpiredDelta = false;
        while (!string.IsNullOrEmpty(_deltaUrl))
        {
            using var response = await GraphGetAsync(_deltaUrl, ct);
            if (response.StatusCode == HttpStatusCode.Gone && !resetExpiredDelta)
            {
                resetExpiredDelta = true;
                await StartScanAsync(ct);
                _logger.LogWarning(
                    "SharePoint delta token expired for {Source}; restarting a full enumeration",
                    _source.Name);
                continue;
            }
            response.EnsureSuccessStatusCode();
            using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
            var root = document.RootElement;

            var changes = new List<SharePointChange>();
            if (root.TryGetProperty("value", out var values))
            {
                // Graph can repeat an item within a page; its last occurrence wins.
                foreach (var item in values.EnumerateArray()
                    .GroupBy(item => GetString(item, "id")).Select(group => group.Last()))
                {
                    changes.AddRange(await ParseChangesAsync(item, ct));
                }
            }

            var nextUrl = root.TryGetProperty("@odata.nextLink", out var next)
                ? next.GetString()
                : null;
            var isDelta = string.IsNullOrEmpty(nextUrl);
            if (isDelta && root.TryGetProperty("@odata.deltaLink", out var delta))
                nextUrl = delta.GetString();
            if (string.IsNullOrEmpty(nextUrl))
                throw new InvalidOperationException("Graph delta response has no continuation or checkpoint");

            await StagePageAsync(changes, nextUrl, isDelta, false, ct);
            await FlushPendingAsync(ct);
            if (isDelta)
            {
                if (_state.Reconcile) await ReconcileScanAsync(ct);
                return;
            }
        }
    }

    private async Task StartScanAsync(CancellationToken ct)
    {
        _state.SchemaVersion = 2;
        _state.Generation = Generation;
        _state.ScanId = ++_state.Revision;
        _state.FullScan = true;
        _state.Reconcile = false;
        lock (_pipelineLock) { _state.PipelineIds = _pipelineIds.Order().ToList(); }
        _deltaUrl = BuildInitialDeltaUrl();
        await PersistDeltaUrlAsync(ct);
    }

    private async Task StagePageAsync(
        List<SharePointChange> changes, string nextUrl, bool isDelta, bool endsScan, CancellationToken ct)
    {
        ++_state.Revision;
        var page = new PendingPage
        {
            Changes = changes,
            Messages = BuildMessages(changes),
            NextUrl = nextUrl,
            IsDelta = isDelta,
            EndsScan = endsScan,
        };
        _state.PendingJson = JsonSerializer.Serialize(page);
        // No messages can be sent until their revision, targets and event IDs are durable.
        await PersistDeltaUrlAsync(ct);
    }

    private async Task FlushPendingAsync(CancellationToken ct)
    {
        var page = JsonSerializer.Deserialize<PendingPage>(_state.PendingJson)
            ?? throw new InvalidOperationException("Invalid SharePoint outbox");
        await _sbPublisher.PublishBatchAsync(page.Messages, ct);
        await PersistReferencesAsync(page.Changes, ct);
        _deltaUrl = page.NextUrl;
        _state.PendingJson = "";
        if (page.EndsScan)
        {
            _state.FullScan = false;
            _state.Reconcile = false;
        }
        else if (page.IsDelta && _state.FullScan)
            _state.Reconcile = true;
        await PersistDeltaUrlAsync(ct);
    }

    private async Task ReconcileScanAsync(CancellationToken ct)
    {
        var missing = new List<SharePointChange>();
        if (_stateContainer is not null)
        {
            var query = new QueryDefinition(
                "SELECT * FROM c WHERE IS_DEFINED(c.itemId) " +
                "AND (NOT IS_DEFINED(c.deleted) OR c.deleted = false) " +
                "AND (NOT IS_DEFINED(c.scanId) OR c.scanId < @scan)")
                .WithParameter("@scan", _state.ScanId);
            using var iterator = _stateContainer.GetItemQueryIterator<SharePointReferenceDocument>(query);
            while (iterator.HasMoreResults)
                foreach (var item in await iterator.ReadNextAsync(ct))
                    missing.Add(new SharePointChange(item.ItemId, item.SourceRef, "", 0, true));
        }
        // Bound the outbox even for a large expired-token reconciliation.
        foreach (var batch in missing.Chunk(100))
        {
            await StagePageAsync(batch.ToList(), _deltaUrl!, true, false, ct);
            await FlushPendingAsync(ct);
        }
        await StagePageAsync([], _deltaUrl!, true, true, ct);
        await FlushPendingAsync(ct);
    }

    private async Task<List<SharePointChange>> ParseChangesAsync(
        JsonElement item,
        CancellationToken ct)
    {
        var id = GetString(item, "id");
        if (string.IsNullOrEmpty(id)) return [];

        if (!_knownRefs.TryGetValue(id, out var previousRef))
            previousRef = await LoadReferenceAsync(id, ct);

        if (item.TryGetProperty("deleted", out _))
        {
            // Identity no longer depends on remembering the last display path.
            return [new SharePointChange(id, previousRef ?? "", "", 0, true)];
        }

        if (!item.TryGetProperty("file", out _))
            return DeletePreviousReference(id, previousRef);

        var name = GetString(item, "name");
        if (string.IsNullOrEmpty(name) || !IsAllowedFile(name))
            return DeletePreviousReference(id, previousRef);

        var size = item.TryGetProperty("size", out var sizeElement) && sizeElement.TryGetInt64(out var parsedSize)
            ? parsedSize
            : 0;
        if (size > _source.SharePointMaxFileSizeBytes)
        {
            _logger.LogWarning(
                "Skipping SharePoint file {Name}: {Size} bytes exceeds limit {Limit}",
                name, size, _source.SharePointMaxFileSizeBytes);
            return DeletePreviousReference(id, previousRef);
        }

        var parentPath = item.TryGetProperty("parentReference", out var parent)
            ? GetString(parent, "path")
            : "";
        var marker = parentPath.IndexOf("root:", StringComparison.OrdinalIgnoreCase);
        var relativeParent = marker >= 0 ? parentPath[(marker + 5)..].Trim('/') : "";
        var sourceRef = string.IsNullOrEmpty(relativeParent) ? name : $"{relativeParent}/{name}";
        var etag = GetString(item, "eTag");
        // A rename is an update of the same identity, never a path-based delete.
        return [new SharePointChange(id, sourceRef, etag, size, false)];
    }

    private static List<SharePointChange> DeletePreviousReference(
        string itemId,
        string? previousRef)
        => string.IsNullOrEmpty(previousRef)
            ? []
            : [new SharePointChange(itemId, previousRef, "", 0, true)];

    private async Task LoadStateAsync(CancellationToken ct)
    {
        if (_stateContainer is null) return;
        try
        {
            var response = await _stateContainer.ReadItemAsync<SharePointStateDocument>(
                SharePointStateDocument.StateId,
                new PartitionKey(SharePointStateDocument.StateId),
                cancellationToken: ct);
            _deltaUrl = response.Resource.DeltaUrl;
            _state = response.Resource;
            _stateEtag = response.ETag;
            _knownRefs.Clear();
            lock (_pipelineLock)
            {
                if (_pipelineIds.Except(response.Resource.PipelineIds).Any())
                    _resetDeltaForNewPipeline = true;
                _pipelineStateInitialized = true;
            }
        }
        catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            _deltaUrl = null;
            _state = new();
            _stateEtag = null;
            lock (_pipelineLock) { _pipelineStateInitialized = true; }
        }
    }

    private async Task<string?> LoadReferenceAsync(string itemId, CancellationToken ct)
    {
        if (_stateContainer is null) return null;
        var id = GetReferenceDocumentId(itemId);
        try
        {
            var response = await _stateContainer.ReadItemAsync<SharePointReferenceDocument>(
                id, new PartitionKey(id), cancellationToken: ct);
            _knownRefs[itemId] = response.Resource.SourceRef;
            return response.Resource.SourceRef;
        }
        catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            return null;
        }
    }

    private async Task PersistReferencesAsync(
        IEnumerable<SharePointChange> changes,
        CancellationToken ct)
    {
        foreach (var change in changes)
        {
            var id = GetReferenceDocumentId(change.ItemId);
            if (_stateContainer is not null)
            {
                // Concurrent/replayed publishers must not regress a reference.
                while (true)
                {
                    ItemResponse<SharePointReferenceDocument>? old = null;
                    try { old = await _stateContainer.ReadItemAsync<SharePointReferenceDocument>(
                        id, new PartitionKey(id), cancellationToken: ct); }
                    catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound) { }
                    if (old is not null && old.Resource.Revision > _state.Revision) break;
                    var reference = new SharePointReferenceDocument
                    {
                        Id = id, ItemId = change.ItemId, SourceRef = change.SourceRef,
                        Deleted = change.Deleted, Revision = _state.Revision, ScanId = _state.ScanId,
                    };
                    try
                    {
                        if (old is null)
                            await _stateContainer.CreateItemAsync(reference, new PartitionKey(id), cancellationToken: ct);
                        else
                            await _stateContainer.ReplaceItemAsync(reference, id, new PartitionKey(id),
                                new ItemRequestOptions { IfMatchEtag = old.ETag }, ct);
                        break;
                    }
                    catch (CosmosException ex) when (ex.StatusCode is HttpStatusCode.Conflict or HttpStatusCode.PreconditionFailed) { }
                }
            }
            if (change.Deleted) _knownRefs.Remove(change.ItemId);
            else _knownRefs[change.ItemId] = change.SourceRef;
        }
    }

    private async Task PersistDeltaUrlAsync(CancellationToken ct)
    {
        if (_stateContainer is null || string.IsNullOrEmpty(_deltaUrl)) return;
        _state.DeltaUrl = _deltaUrl;
        var pk = new PartitionKey(SharePointStateDocument.StateId);
        var response = _stateEtag is null
            ? await _stateContainer.CreateItemAsync(_state, pk, cancellationToken: ct)
            : await _stateContainer.ReplaceItemAsync(_state, SharePointStateDocument.StateId, pk,
                new ItemRequestOptions { IfMatchEtag = _stateEtag }, ct);
        _stateEtag = response.ETag;
    }

    private static string GetReferenceDocumentId(string itemId)
    {
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(itemId));
        return $"sharepoint-ref-{Convert.ToHexString(hash).ToLowerInvariant()}";
    }

    private bool IsAllowedFile(string name)
    {
        var configured = _source.SharePointFileTypes;
        if (configured.Count == 0) return true;
        var extension = Path.GetExtension(name).TrimStart('.');
        return configured.Any(value =>
            string.Equals(value.Trim().TrimStart('.'), extension, StringComparison.OrdinalIgnoreCase));
    }

    private List<EmbeddingMessage> BuildMessages(List<SharePointChange> changes)
    {
        var allMessages = new List<EmbeddingMessage>();
        List<Pipeline> pipelines;
        lock (_pipelineLock) { pipelines = new List<Pipeline>(_activePipelines); }

        foreach (var pipeline in pipelines.Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id)))
        {
            var destination = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
            if (destination is null)
                throw new InvalidOperationException($"SharePoint pipeline {pipeline.Id} has no destination");

            var messages = changes.Select(change => new EmbeddingMessage
            {
                MessageId = HashIdentity(_source.Id, _source.SharePointSiteId!, _source.SharePointDriveId!,
                    change.ItemId, pipeline.Id, _state.Revision.ToString(System.Globalization.CultureInfo.InvariantCulture)),
                PipelineId = pipeline.Id,
                PipelineName = pipeline.Name,
                DocgrokPipeline = pipeline.DocgrokPipeline,
                SourceId = _source.Id,
                SourceRef = change.SourceRef,
                DestinationId = destination.Id,
                DestinationType = destination.Type,
                DestinationConfig = destination.Config,
                ContentHash = _hasher.ComputeHash($"{change.ItemId}:{change.ETag}"),
                PartitionKeyValue = HashIdentity(_source.Id, _source.SharePointSiteId!,
                    _source.SharePointDriveId!, change.ItemId, pipeline.Id),
                PipelineGeneration = Generation,
                StoreContent = pipeline.StoreContent,
                ContentField = pipeline.ContentField,
                MetadataFields = pipeline.MetadataFields,
                ContentType = "sharepoint_ref",
                MessageType = change.Deleted ? "delete" : "upsert",
                SharePointSiteId = _source.SharePointSiteId,
                SharePointDriveId = _source.SharePointDriveId,
                SharePointItemId = change.ItemId,
                SharePointRevision = _state.Revision,
                SharePointETag = change.ETag,
                SharePointFileName = Path.GetFileName(change.SourceRef),
                SharePointMaxFileSizeBytes = _source.SharePointMaxFileSizeBytes,
            }).ToList();

            allMessages.AddRange(messages);
        }
        return allMessages;
    }

    private static string HashIdentity(params string[] parts)
        => "sp-" + Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(JsonSerializer.Serialize(parts)))).ToLowerInvariant();

    private async Task<HttpResponseMessage> GraphGetAsync(string url, CancellationToken ct)
    {
        var token = await _credential.GetTokenAsync(new TokenRequestContext(GraphScopes), ct);
        var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        request.Headers.TryAddWithoutValidation("Prefer", "odata.maxpagesize=100");
        return await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
    }

    private static string GetString(JsonElement element, string property)
        => element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";

    public async ValueTask DisposeAsync()
    {
        _cts?.Cancel();
        if (_pollTask is not null)
            try { await _pollTask; } catch { }
        _cts?.Dispose();
        _http.Dispose();
    }

    private sealed record SharePointChange(
        string ItemId,
        string SourceRef,
        string ETag,
        long Size,
        bool Deleted);

    private sealed class SharePointStateDocument
    {
        public const string StateId = "sharepoint-state";

        [Newtonsoft.Json.JsonProperty("id")]
        public string Id { get; set; } = StateId;

        [Newtonsoft.Json.JsonProperty("deltaUrl")]
        public string DeltaUrl { get; set; } = "";

        [Newtonsoft.Json.JsonProperty("pipelineIds")]
        public List<string> PipelineIds { get; set; } = new();

        public int SchemaVersion { get; set; }
        public string Generation { get; set; } = "";
        public long Revision { get; set; }
        public long ScanId { get; set; }
        public bool FullScan { get; set; }
        public bool Reconcile { get; set; }
        public string PendingJson { get; set; } = "";
    }

    private sealed class PendingPage
    {
        public List<SharePointChange> Changes { get; set; } = new();
        public List<EmbeddingMessage> Messages { get; set; } = new();
        public string NextUrl { get; set; } = "";
        public bool IsDelta { get; set; }
        public bool EndsScan { get; set; }
    }

    private sealed class SharePointReferenceDocument
    {
        [Newtonsoft.Json.JsonProperty("id")]
        public string Id { get; set; } = "";

        [Newtonsoft.Json.JsonProperty("itemId")]
        public string ItemId { get; set; } = "";

        [Newtonsoft.Json.JsonProperty("sourceRef")]
        public string SourceRef { get; set; } = "";

        [Newtonsoft.Json.JsonProperty("deleted")]
        public bool Deleted { get; set; }

        [Newtonsoft.Json.JsonProperty("revision")]
        public long Revision { get; set; }

        [Newtonsoft.Json.JsonProperty("scanId")]
        public long ScanId { get; set; }
    }
}
