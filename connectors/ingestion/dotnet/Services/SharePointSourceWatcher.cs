using System.Net.Http.Headers;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;
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
    private readonly ContentHasher _hasher;
    private readonly ServiceBusPublisher _sbPublisher;
    private readonly ILogger<SharePointSourceWatcher> _logger;
    private readonly DefaultAzureCredential _credential = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(60) };
    private readonly Dictionary<string, string> _knownRefs = new(StringComparer.Ordinal);
    private readonly object _pipelineLock = new();

    private List<Pipeline> _activePipelines = new();
    private List<Destination> _destinations = new();
    private CancellationTokenSource? _cts;
    private Task? _pollTask;
    private string? _deltaUrl;

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public SharePointSourceWatcher(
        Source source,
        ChangeFeedOptions options,
        ContentHasher hasher,
        ILogger<SharePointSourceWatcher> logger,
        string? generation = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _hasher = hasher;
        _logger = logger;
        _sbPublisher = sbPublisher
            ?? throw new InvalidOperationException("SharePoint sources require Service Bus");
        Generation = generation ?? "0";
    }

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock) { _activePipelines = new List<Pipeline>(pipelines); }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public async Task StartAsync(CancellationToken ct)
    {
        if (!_sbPublisher.IsEnabled)
            throw new InvalidOperationException("SharePoint sources require an enabled Service Bus publisher");
        if (string.IsNullOrWhiteSpace(_source.SharePointSiteId)
            || string.IsNullOrWhiteSpace(_source.SharePointDriveId))
            throw new InvalidOperationException("SharePoint source requires site_id and drive_id");

        _deltaUrl = BuildInitialDeltaUrl();
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
        var nextUrl = _deltaUrl ?? BuildInitialDeltaUrl();
        while (!string.IsNullOrEmpty(nextUrl))
        {
            using var response = await GraphGetAsync(nextUrl, ct);
            response.EnsureSuccessStatusCode();
            using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
            var root = document.RootElement;

            var changes = new List<SharePointChange>();
            if (root.TryGetProperty("value", out var values))
            {
                foreach (var item in values.EnumerateArray())
                {
                    var change = ParseChange(item);
                    if (change is not null) changes.Add(change);
                }
            }

            if (changes.Count > 0)
                await PublishChangesAsync(changes, ct);

            nextUrl = root.TryGetProperty("@odata.nextLink", out var next)
                ? next.GetString()
                : null;
            if (string.IsNullOrEmpty(nextUrl)
                && root.TryGetProperty("@odata.deltaLink", out var delta))
                _deltaUrl = delta.GetString();
        }
    }

    private SharePointChange? ParseChange(JsonElement item)
    {
        var id = GetString(item, "id");
        if (string.IsNullOrEmpty(id)) return null;

        if (item.TryGetProperty("deleted", out _))
        {
            return _knownRefs.TryGetValue(id, out var deletedRef)
                ? new SharePointChange(id, deletedRef, "", 0, true)
                : null;
        }

        if (!item.TryGetProperty("file", out _)) return null;
        var name = GetString(item, "name");
        if (string.IsNullOrEmpty(name) || !IsAllowedFile(name)) return null;

        var size = item.TryGetProperty("size", out var sizeElement) && sizeElement.TryGetInt64(out var parsedSize)
            ? parsedSize
            : 0;
        if (size > _source.SharePointMaxFileSizeBytes)
        {
            _logger.LogWarning(
                "Skipping SharePoint file {Name}: {Size} bytes exceeds limit {Limit}",
                name, size, _source.SharePointMaxFileSizeBytes);
            return null;
        }

        var parentPath = item.TryGetProperty("parentReference", out var parent)
            ? GetString(parent, "path")
            : "";
        var marker = parentPath.IndexOf("root:", StringComparison.OrdinalIgnoreCase);
        var relativeParent = marker >= 0 ? parentPath[(marker + 5)..].Trim('/') : "";
        var sourceRef = string.IsNullOrEmpty(relativeParent) ? name : $"{relativeParent}/{name}";
        var etag = GetString(item, "eTag");
        _knownRefs[id] = sourceRef;
        return new SharePointChange(id, sourceRef, etag, size, false);
    }

    private bool IsAllowedFile(string name)
    {
        var configured = _source.SharePointFileTypes;
        if (configured.Count == 0) return true;
        var extension = Path.GetExtension(name).TrimStart('.');
        return configured.Any(value =>
            string.Equals(value.Trim().TrimStart('.'), extension, StringComparison.OrdinalIgnoreCase));
    }

    private async Task PublishChangesAsync(List<SharePointChange> changes, CancellationToken ct)
    {
        List<Pipeline> pipelines;
        lock (_pipelineLock) { pipelines = new List<Pipeline>(_activePipelines); }

        foreach (var pipeline in pipelines.Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id)))
        {
            var destination = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
            if (destination is null) continue;

            var messages = changes.Select(change => new EmbeddingMessage
            {
                PipelineId = pipeline.Id,
                PipelineName = pipeline.Name,
                DocgrokPipeline = pipeline.DocgrokPipeline,
                SourceId = _source.Id,
                SourceRef = change.SourceRef,
                DestinationId = destination.Id,
                DestinationType = destination.Type,
                DestinationConfig = destination.Config,
                ContentHash = _hasher.ComputeHash($"{change.ItemId}:{change.ETag}"),
                PartitionKeyValue = change.SourceRef,
                PipelineGeneration = Generation,
                StoreContent = pipeline.StoreContent,
                ContentField = pipeline.ContentField,
                MetadataFields = pipeline.MetadataFields,
                ContentType = "sharepoint_ref",
                MessageType = change.Deleted ? "delete" : "upsert",
                SharePointSiteId = _source.SharePointSiteId,
                SharePointDriveId = _source.SharePointDriveId,
                SharePointItemId = change.ItemId,
                SharePointFileName = Path.GetFileName(change.SourceRef),
                SharePointMaxFileSizeBytes = _source.SharePointMaxFileSizeBytes,
            }).ToList();

            await _sbPublisher.PublishBatchAsync(messages, ct);
        }
    }

    private async Task<HttpResponseMessage> GraphGetAsync(string url, CancellationToken ct)
    {
        var token = await _credential.GetTokenAsync(new TokenRequestContext(GraphScopes), ct);
        var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
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
}
