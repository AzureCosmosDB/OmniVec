using System.Collections.Concurrent;
using System.Text.Json;
using Azure.Identity;
using Azure.Messaging.ServiceBus;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Consumes Event Grid blob events from the configured Service Bus queue
/// (default: "blob-events"), maps each event to active azure-blob sources +
/// queue-mode pipelines, and republishes self-contained <see cref="EmbeddingMessage"/>s
/// onto the existing embeddings topic for the worker to process.
///
/// Replaces the BlobSourceWatcher Phase 2 polling loop for live updates.
/// (Phase 1 prefill still runs once on watcher start to backfill existing blobs.)
///
/// Event schema: Azure Event Grid native schema delivered to Service Bus.
/// Each SB message body is a JSON array with a single EG event (Azure default).
/// </summary>
public class BlobEventConsumer : BackgroundService
{
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ServiceBusPublisher _publisher;
    private readonly ContentHasher _hasher;
    private readonly ILogger<BlobEventConsumer> _logger;

    private ServiceBusClient? _client;
    private ServiceBusProcessor? _processor;

    // Live snapshots refreshed by background polling. Volatile reads are fine
    // for whole-reference swaps.
    private volatile List<Source> _blobSources = new();
    private volatile List<Pipeline> _pipelines = new();
    private volatile List<Destination> _destinations = new();

    // Recent (url + etag) dedupe — protects against duplicate EG redeliveries
    // and prefill/live overlap. Capacity-bounded.
    private readonly ConcurrentDictionary<string, byte> _recent = new();
    private const int RecentCap = 50_000;

    public BlobEventConsumer(
        IOptions<ChangeFeedOptions> options,
        OmniVecApiClient apiClient,
        ServiceBusPublisher publisher,
        ContentHasher hasher,
        ILogger<BlobEventConsumer> logger)
    {
        _options = options.Value;
        _apiClient = apiClient;
        _publisher = publisher;
        _hasher = hasher;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (!_options.BlobEventConsumerEnabled)
        {
            _logger.LogInformation("BlobEventConsumer disabled (ChangeFeed__BlobEventConsumerEnabled=false)");
            await Task.Delay(Timeout.Infinite, ct);
            return;
        }
        if (string.IsNullOrWhiteSpace(_options.ServiceBusNamespace))
        {
            _logger.LogWarning("BlobEventConsumer enabled but ServiceBusNamespace is empty — staying idle");
            await Task.Delay(Timeout.Infinite, ct);
            return;
        }

        await Task.Delay(TimeSpan.FromSeconds(5), ct); // let API come up

        _client = new ServiceBusClient(_options.ServiceBusNamespace, new DefaultAzureCredential());
        _processor = _client.CreateProcessor(_options.BlobEventQueueName, new ServiceBusProcessorOptions
        {
            MaxConcurrentCalls = Math.Max(1, _options.BlobEventConsumerConcurrency),
            AutoCompleteMessages = false,
            ReceiveMode = ServiceBusReceiveMode.PeekLock,
            PrefetchCount = 32,
        });
        _processor.ProcessMessageAsync += HandleMessageAsync;
        _processor.ProcessErrorAsync += HandleErrorAsync;

        _ = Task.Run(() => RefreshLoopAsync(ct), ct);

        await _processor.StartProcessingAsync(ct);
        _logger.LogInformation(
            "BlobEventConsumer started: {Ns}/{Queue} (concurrency={C})",
            _options.ServiceBusNamespace, _options.BlobEventQueueName,
            _options.BlobEventConsumerConcurrency);

        try { await Task.Delay(Timeout.Infinite, ct); }
        catch (OperationCanceledException) { }
        finally
        {
            try { await _processor.StopProcessingAsync(CancellationToken.None); } catch { }
            try { await _processor.DisposeAsync(); } catch { }
            try { await _client.DisposeAsync(); } catch { }
        }
    }

    private async Task RefreshLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var sources = await _apiClient.GetSourcesByTypesAsync(new[] { "azure-blob" }, ct);
                var pipelines = await _apiClient.GetActivePipelinesAsync(ct);
                var destinations = await _apiClient.GetDestinationsAsync(ct);
                _blobSources = sources;
                _pipelines = pipelines;
                _destinations = destinations;
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "BlobEventConsumer snapshot refresh failed");
            }
            try { await Task.Delay(TimeSpan.FromSeconds(_options.SourcePollIntervalSeconds), ct); }
            catch (OperationCanceledException) { break; }
        }
    }

    private Task HandleErrorAsync(ProcessErrorEventArgs args)
    {
        _logger.LogError(args.Exception,
            "BlobEventConsumer SB error src={Source} ent={Entity}",
            args.ErrorSource, args.EntityPath);
        return Task.CompletedTask;
    }

    private async Task HandleMessageAsync(ProcessMessageEventArgs args)
    {
        var ct = args.CancellationToken;
        try
        {
            var body = args.Message.Body.ToString();
            var events = ParseEvents(body);
            if (events.Count == 0)
            {
                await args.CompleteMessageAsync(args.Message, ct);
                return;
            }

            var batch = new List<EmbeddingMessage>();
            foreach (var ev in events)
            {
                batch.AddRange(BuildEmbeddingMessages(ev));
            }

            if (batch.Count > 0)
            {
                await _publisher.PublishBatchAsync(batch, ct);
                _logger.LogInformation(
                    "BlobEventConsumer fanned out {Count} embedding message(s) from {Events} blob event(s) [msgId={Id}]",
                    batch.Count, events.Count, args.Message.MessageId);
            }
            else
            {
                _logger.LogDebug("BlobEventConsumer: no matching sources/pipelines for {Events} event(s) [msgId={Id}]",
                    events.Count, args.Message.MessageId);
            }

            await args.CompleteMessageAsync(args.Message, ct);
        }
        catch (OperationCanceledException) { /* shutdown */ }
        catch (Exception ex)
        {
            _logger.LogError(ex, "BlobEventConsumer handler failed for msg {Id}, abandoning", args.Message.MessageId);
            try { await args.AbandonMessageAsync(args.Message, cancellationToken: ct); } catch { }
        }
    }

    /// <summary>
    /// SB queue receives Event Grid native schema. Body can be either a single
    /// EG event object or an array of events (Azure currently sends array form
    /// for system topic SB deliveries). Also supports CloudEvents 1.0 schema as
    /// a fallback in case the subscription is reconfigured.
    /// </summary>
    internal static List<BlobEventGridEvent> ParseEvents(string body)
    {
        var results = new List<BlobEventGridEvent>();
        if (string.IsNullOrWhiteSpace(body)) return results;

        try
        {
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (root.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in root.EnumerateArray())
                {
                    var ev = ParseOne(item);
                    if (ev is not null) results.Add(ev);
                }
            }
            else if (root.ValueKind == JsonValueKind.Object)
            {
                var ev = ParseOne(root);
                if (ev is not null) results.Add(ev);
            }
        }
        catch (JsonException) { /* malformed payload — caller logs */ }
        return results;
    }

    private static BlobEventGridEvent? ParseOne(JsonElement el)
    {
        // EG schema fields: eventType, subject, data.{url,api,eTag,contentType,...}
        // CloudEvents schema fields: type, subject, data.{...}
        string? eventType = TryGetString(el, "eventType") ?? TryGetString(el, "type");
        string? subject = TryGetString(el, "subject");
        if (string.IsNullOrEmpty(eventType) || string.IsNullOrEmpty(subject)) return null;
        if (!el.TryGetProperty("data", out var data) || data.ValueKind != JsonValueKind.Object)
            return null;

        return new BlobEventGridEvent
        {
            EventType = eventType!,
            Subject = subject!,
            Url = TryGetString(data, "url") ?? "",
            ETag = TryGetString(data, "eTag") ?? "",
            Api = TryGetString(data, "api") ?? "",
            ContentType = TryGetString(data, "contentType") ?? "",
        };
    }

    private static string? TryGetString(JsonElement el, string name)
        => el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private List<EmbeddingMessage> BuildEmbeddingMessages(BlobEventGridEvent ev)
    {
        var output = new List<EmbeddingMessage>();
        if (string.IsNullOrEmpty(ev.Url)) return output;

        // Derive (account, container, blobName) from data.url
        // e.g. https://acct.blob.core.windows.net/container/path/to/blob.pdf
        if (!Uri.TryCreate(ev.Url, UriKind.Absolute, out var uri)) return output;
        var accountName = uri.Host.Split('.').FirstOrDefault() ?? "";
        var segments = uri.AbsolutePath.TrimStart('/').Split('/', 2);
        if (segments.Length < 2 || string.IsNullOrEmpty(accountName)) return output;
        var container = segments[0];
        var blobName = Uri.UnescapeDataString(segments[1]);

        var isDelete = string.Equals(ev.EventType, "Microsoft.Storage.BlobDeleted", StringComparison.OrdinalIgnoreCase);
        var isRename = string.Equals(ev.EventType, "Microsoft.Storage.BlobRenamed", StringComparison.OrdinalIgnoreCase);

        // Dedupe upsert events by (url + etag). Delete events always pass.
        if (!isDelete)
        {
            var key = $"{ev.Url}:{ev.ETag}";
            if (!_recent.TryAdd(key, 1)) return output;
            if (_recent.Count > RecentCap)
            {
                // Cheap pruning: clear half on overflow (no LRU ordering needed)
                foreach (var k in _recent.Keys.Take(RecentCap / 2))
                    _recent.TryRemove(k, out _);
            }
        }

        // Find matching sources (account_url host startsWith accountName + container match + optional prefix match)
        var matchingSources = new List<Source>();
        foreach (var s in _blobSources)
        {
            if (!s.Enabled) continue;
            if (!string.Equals(s.BlobContainer, container, StringComparison.OrdinalIgnoreCase)) continue;
            if (!string.IsNullOrEmpty(s.BlobAccountUrl) &&
                !s.BlobAccountUrl.Contains(accountName + ".", StringComparison.OrdinalIgnoreCase))
                continue;
            var prefix = s.BlobPrefix ?? "";
            if (!string.IsNullOrEmpty(prefix) &&
                !blobName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                continue;
            var fileExt = "." + (s.BlobFileType ?? "pdf").TrimStart('.');
            if (!blobName.EndsWith(fileExt, StringComparison.OrdinalIgnoreCase)) continue;
            matchingSources.Add(s);
        }
        if (matchingSources.Count == 0) return output;

        foreach (var src in matchingSources)
        {
            var relevantPipelines = _pipelines
                .Where(p => string.Equals(p.ProcessingMode, "queue", StringComparison.OrdinalIgnoreCase))
                .Where(p => p.Sources.Any(ps => ps.SourceId == src.Id))
                .ToList();
            foreach (var pipeline in relevantPipelines)
            {
                var dest = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
                if (dest is null) continue;

                var blobKey = $"{blobName}:{ev.ETag}";
                var contentHash = _hasher.ComputeHash(blobKey);

                output.Add(new EmbeddingMessage
                {
                    PipelineId = pipeline.Id,
                    PipelineName = pipeline.Name,
                    DocgrokPipeline = pipeline.DocgrokPipeline,
                    SourceId = src.Id,
                    SourceRef = blobName,
                    DestinationId = dest.Id,
                    DestinationType = dest.Type,
                    DestinationConfig = dest.Config,
                    Content = "",
                    ContentHash = contentHash,
                    PartitionKeyValue = blobName,
                    PipelineGeneration = "eventgrid",
                    StoreContent = pipeline.StoreContent,
                    ContentField = pipeline.ContentField,
                    MetadataFields = pipeline.MetadataFields,
                    ContentType = "blob_ref",
                    BlobAccountUrl = src.BlobAccountUrl,
                    BlobConnectionString = src.BlobConnectionString,
                    BlobContainer = src.BlobContainer,
                    BlobName = blobName,
                    MessageType = isDelete ? "delete" : "upsert",
                });
            }
        }

        if (isRename)
        {
            // BlobRenamed delivers the new url; if we wanted to also delete the
            // old doc we'd need data.destinationUrl/sourceUrl handling. Skipping
            // the old-name delete for now — orphans get cleaned by user reset.
        }

        return output;
    }
}

/// <summary>Minimal projection of an Event Grid blob event needed by the consumer.</summary>
internal class BlobEventGridEvent
{
    public string EventType { get; set; } = "";
    public string Subject { get; set; } = "";
    public string Url { get; set; } = "";
    public string ETag { get; set; } = "";
    public string Api { get; set; } = "";
    public string ContentType { get; set; } = "";
}
