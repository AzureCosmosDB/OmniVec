using System.Net.Http.Json;
using System.Text.Json;
using Azure.Identity;
using Microsoft.Azure.Cosmos;
using Newtonsoft.Json.Linq;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Wraps a single ChangeFeedProcessor instance watching one source CosmosDB container.
/// Two modes:
///   "queue"  → filters by content_field/content_hash, creates PENDING jobs via API
///   "inline" → calls DocGrok directly, patches source documents with embeddings (zero queue overhead)
/// </summary>
public class SourceWatcher : ISourceWatcher
{
    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly LeaseContainerManager _leaseManager;
    private readonly ContentHasher _hasher;
    private readonly ILogger<SourceWatcher> _logger;
    private readonly HttpClient _docGrokClient;
    private readonly ServiceBusPublisher? _sbPublisher;

    private ChangeFeedProcessor? _processor;
    private Container? _sourceContainer;
    private string _partitionKeyPath = "/id"; // Discovered from container properties
    private volatile bool _running;

    private const int MaxPatchRetries = 20; // Cap retries to prevent infinite loops on permanent errors

    // Global concurrency limiter — prevents overwhelming CosmosDB
    // when multiple partitions process simultaneously. 1000 concurrent patches
    // for 100K RU/s provisioned throughput (~10 RU per patch = 10K patches/sec max).
    private static readonly SemaphoreSlim _patchThrottle = new(3000, 3000);

    // Cached pipeline state (refreshed by SourceWatcherManager)
    private List<Pipeline> _activePipelines = new();
    private readonly object _pipelineLock = new();

    public string SourceId => _source.Id;

    /// <summary>
    /// Generation tag appended to processorName. When reset_at changes, the generation
    /// changes too, causing CFP to create fresh lease docs and replay from the beginning.
    /// Old lease docs are abandoned in-place (harmless).
    /// </summary>
    public string Generation { get; }

    /// <summary>
    /// When true, skip content_hash dedup — used after a pipeline reset so all
    /// existing documents get reprocessed even if content hasn't changed.
    /// </summary>
    public bool SkipContentHash { get; set; }

    // Destinations cache (populated by SourceWatcherManager)
    private List<Destination> _destinations = new();

    public void UpdateDestinations(List<Destination> destinations)
    {
        _destinations = destinations;
    }

    public SourceWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        LeaseContainerManager leaseManager,
        ContentHasher hasher,
        ILogger<SourceWatcher> logger,
        string? generation = null,
        HttpClient? docGrokClient = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _apiClient = apiClient;
        _leaseManager = leaseManager;
        _hasher = hasher;
        _logger = logger;
        Generation = generation ?? "0";
        _docGrokClient = docGrokClient ?? new HttpClient { BaseAddress = new Uri(options.DocGrokBaseUrl) };
        _sbPublisher = sbPublisher;
    }

    public void UpdatePipelines(List<Pipeline> allPipelines)
    {
        var relevant = allPipelines
            .Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id))
            .ToList();
        lock (_pipelineLock)
        {
            _activePipelines = relevant;
        }
    }

    public async Task StartAsync(CancellationToken ct)
    {
        if (_running) return;

        var sourceClient = new CosmosClient(
            _source.Endpoint!,
            new DefaultAzureCredential(),
            new CosmosClientOptions
            {
                ConnectionMode = ConnectionMode.Direct,
                ConsistencyLevel = ConsistencyLevel.Eventual,
                MaxRetryAttemptsOnRateLimitedRequests = int.MaxValue,
                MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(300),
            });
        _sourceContainer = sourceClient
            .GetDatabase(_source.Database!)
            .GetContainer(_source.Container!);

        // Discover the partition key path from container properties
        try
        {
            var props = await _sourceContainer.ReadContainerAsync(cancellationToken: ct);
            var pkPath = props.Resource.PartitionKeyPath; // e.g. "/partition_id"
            _partitionKeyPath = pkPath;
            _logger.LogInformation("Source {SourceId} container PK path: {PkPath}", _source.Id, pkPath);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Could not read container PK path for source {SourceId}, defaulting to /id", _source.Id);
        }

        var leaseContainer = await _leaseManager.EnsureLeaseContainerAsync(_source.Id, ct);

        // Fixed processorName per source — no generation suffix.
        // Resets are handled by clearing lease documents, not creating new processor names.
        var processorName = $"omnivec-cf-{_source.Id}";
        _processor = _sourceContainer
            .GetChangeFeedProcessorBuilder<JObject>(
                processorName: processorName,
                onChangesDelegate: HandleChangesAsync)
            .WithInstanceName(_options.InstanceName)
            .WithLeaseContainer(leaseContainer)
            .WithMaxItems(_options.MaxItemsPerBatch)
            .WithPollInterval(TimeSpan.FromSeconds(_options.FeedPollIntervalSeconds))
            .WithStartTime(DateTime.MinValue.ToUniversalTime())
            .WithErrorNotification(HandleErrorAsync)
            .Build();

        await _processor.StartAsync();
        _running = true;
        _logger.LogInformation(
            "Started CFP for source {SourceId} ({Name}) gen={Generation} [{Endpoint}/{Database}/{Container}]",
            _source.Id, _source.Name, Generation, _source.Endpoint, _source.Database, _source.Container);
    }

    public async Task StopAsync()
    {
        if (!_running || _processor is null) return;
        await _processor.StopAsync();
        _running = false;
        _logger.LogInformation("Stopped CFP for source {SourceId} ({Name})", _source.Id, _source.Name);
    }

    private async Task HandleChangesAsync(
        ChangeFeedProcessorContext context,
        IReadOnlyCollection<JObject> changes,
        CancellationToken ct)
    {
        List<Pipeline> pipelines;
        lock (_pipelineLock)
        {
            pipelines = new List<Pipeline>(_activePipelines);
        }

        _logger.LogInformation(
            "CF source={SourceId}: {Count} changes on partition {Partition}, pipelines={PipelineCount}, skipHash={SkipHash}",
            _source.Id, changes.Count, context.LeaseToken, pipelines.Count, SkipContentHash);

        if (pipelines.Count == 0)
        {
            // No active pipelines — DO NOT checkpoint. Return without processing so these
            // changes are re-delivered when a pipeline becomes active. This ensures no
            // documents are silently dropped while pipelines are paused.
            _logger.LogWarning("No active pipelines for source {SourceId} — holding {Count} changes for re-delivery", _source.Id, changes.Count);
            throw new InvalidOperationException($"No active pipelines for source {_source.Id} — refusing to checkpoint to prevent document loss");
        }

        // Separate pipelines by processing mode
        var inlinePipelines = pipelines.Where(p => p.ProcessingMode == "inline").ToList();
        var queuePipelines = pipelines.Where(p => p.ProcessingMode != "inline").ToList();

        // Process documents per-pipeline (each pipeline may have different content_fields)
        int skippedNoContent = 0, skippedUnchanged = 0;
        var allEligibleDocs = new Dictionary<string, List<(string docId, string content, string contentHash, string pkValue, JObject doc, List<string> cfFields, Source.AttachmentRef? att)>>();

        // When the source opts into attachment mode, watcher emits one blob_ref message
        // per matching attachment (DocGrok pipeline-worker downloads the blob and embeds it)
        // instead of extracting inline text from content_fields.
        var attachmentMode = !string.IsNullOrEmpty(_source.AttachmentsField);

        foreach (var pipeline in pipelines)
        {
            var pipSrcOuter = pipeline.Sources.FirstOrDefault(ps => ps.SourceId == _source.Id);
            var cfFields = pipSrcOuter?.ContentFields ?? new List<string> { "content" };
            var eligible = new List<(string docId, string content, string contentHash, string pkValue, JObject doc, List<string> cfFields, Source.AttachmentRef? att)>();

            foreach (var doc in changes)
            {
                try
                {
                    if (doc is null) continue;

                    if (attachmentMode)
                    {
                        var atts = _source.ExtractAttachments(doc);
                        if (atts.Count == 0)
                        {
                            skippedNoContent++;
                            continue;
                        }
                        var aDocId = doc["id"]?.Value<string>() ?? "";
                        var aEtag = doc["_etag"]?.Value<string>() ?? "";
                        var aPkField = _partitionKeyPath.TrimStart('/');
                        var aPkValue = aPkField == "id" ? aDocId : (doc[aPkField]?.Value<string>() ?? "");
                        foreach (var att in atts)
                        {
                            var attHash = _hasher.ComputeHash($"{aDocId}::{aEtag}::{att.Name}::{att.Url}");
                            eligible.Add((aDocId, "", attHash, aPkValue, doc, cfFields, att));
                        }
                        continue;
                    }

                    if (!Source.HasContent(doc, cfFields))
                    {
                        skippedNoContent++;
                        continue;
                    }
                    var contentText = Source.ExtractContent(doc, cfFields);
                    if (string.IsNullOrEmpty(contentText))
                    {
                        skippedNoContent++;
                        continue;
                    }

                    // Idempotency guard: if this doc was already embedded for
                    // THIS pipeline at or after the current reset_at, skip it.
                    // This runs even when SkipContentHash=true (post-reset) so
                    // we don't infinite-loop on the PATCH-feedback change feed.
                    {
                        var existingEmbeddedAt = doc["embedded_at"];
                        var existingPipelineId = doc["pipeline_id"]?.Value<string>();
                        if (existingEmbeddedAt is not null && existingEmbeddedAt.Type != JTokenType.Null
                            && (string.IsNullOrEmpty(existingPipelineId) || existingPipelineId == pipeline.Id))
                        {
                            DateTime embeddedDt;
                            bool parsedEmbedded = false;
                            if (existingEmbeddedAt.Type == JTokenType.Date)
                            {
                                embeddedDt = existingEmbeddedAt.Value<DateTime>();
                                parsedEmbedded = true;
                            }
                            else
                            {
                                parsedEmbedded = DateTime.TryParse(existingEmbeddedAt.Value<string>(),
                                    null, System.Globalization.DateTimeStyles.AssumeUniversal | System.Globalization.DateTimeStyles.AdjustToUniversal,
                                    out embeddedDt);
                            }
                            if (parsedEmbedded)
                            {
                                bool afterReset = true;
                                if (!string.IsNullOrEmpty(pipeline.ResetAt) &&
                                    DateTime.TryParse(pipeline.ResetAt,
                                        null, System.Globalization.DateTimeStyles.AssumeUniversal | System.Globalization.DateTimeStyles.AdjustToUniversal,
                                        out var resetDt))
                                {
                                    afterReset = embeddedDt >= resetDt;
                                }
                                if (afterReset)
                                {
                                    skippedUnchanged++;
                                    continue;
                                }
                            }
                        }
                    }

                    // Content hash dedup (respects reset_at)
                    if (!SkipContentHash)
                    {
                        var existingHash = doc["content_hash"]?.Value<string>();
                        if (!string.IsNullOrEmpty(existingHash))
                        {
                            var currentHash = _hasher.ComputeHash(contentText);
                            if (currentHash == existingHash)
                            {
                                var embeddedAtToken = doc["embedded_at"];
                                bool needsReprocess = false;
                                if (embeddedAtToken is not null && embeddedAtToken.Type != JTokenType.Null)
                                {
                                    DateTime embeddedDt;
                                    if (embeddedAtToken.Type == JTokenType.Date)
                                        embeddedDt = embeddedAtToken.Value<DateTime>();
                                    else if (!DateTime.TryParse(embeddedAtToken.Value<string>(), out embeddedDt))
                                        needsReprocess = true;

                                    if (!needsReprocess)
                                    {
                                        if (!string.IsNullOrEmpty(pipeline.ResetAt) &&
                                            DateTime.TryParse(pipeline.ResetAt, out var resetDt) &&
                                            resetDt > embeddedDt)
                                        {
                                            needsReprocess = true;
                                        }
                                    }
                                }
                                if (!needsReprocess)
                                {
                                    skippedUnchanged++;
                                    continue;
                                }
                            }
                        }
                    }

                    var docId = doc["id"]?.Value<string>() ?? "";
                    var contentHash2 = _hasher.ComputeHash(contentText);
                    var pkField = _partitionKeyPath.TrimStart('/');
                    var pkValue = pkField == "id" ? docId : (doc[pkField]?.Value<string>() ?? "");

                    eligible.Add((docId, contentText, contentHash2, pkValue, doc, cfFields, null));
                }
                catch (Exception ex)
                {
                    var failDocId = doc?["id"]?.Value<string>() ?? "unknown";
                    _logger.LogError(ex, "CRITICAL: Error processing CF document {DocId} — aborting batch to prevent data loss.", failDocId);
                    throw new InvalidOperationException($"Failed to process document {failDocId}, aborting to prevent data loss", ex);
                }
            }
            allEligibleDocs[pipeline.Id] = eligible;
        }

        var totalEligible = allEligibleDocs.Values.Sum(e => e.Count);
        _logger.LogInformation(
            "CF source={SourceId} partition={Partition}: {Total} docs → {Eligible} eligible, skipped: {NoContent} no-content, {Unchanged} unchanged",
            _source.Id, context.LeaseToken, changes.Count, totalEligible, skippedNoContent, skippedUnchanged);

        // Report changefeed metrics (fire-and-forget)
        int jobsCreated = 0;

        if (totalEligible == 0)
        {
            _ = _apiClient.ReportChangeFeedMetricsAsync(
                _source.Id, changes.Count, 0, skippedNoContent, skippedUnchanged, 0, context.LeaseToken, CancellationToken.None);
            return;
        }

        // Handle INLINE pipelines
        if (inlinePipelines.Count > 0)
        {
            // Attachment-mode entries can't be processed inline (worker must download the
            // blob from Azure Storage), so they bypass the inline path and go to queue mode.
            var inlineEligible = inlinePipelines
                .Where(p => allEligibleDocs.ContainsKey(p.Id))
                .SelectMany(p => allEligibleDocs[p.Id].Where(e => e.att is null).Select(e => (e.docId, e.content, e.contentHash, e.pkValue, e.doc)))
                .ToList();
            if (inlineEligible.Count > 0)
                await ProcessInlineAsync(inlinePipelines, inlineEligible, context.LeaseToken, ct);
            if (attachmentMode && inlinePipelines.Any(p => allEligibleDocs.TryGetValue(p.Id, out var d) && d.Any(e => e.att is not null)))
            {
                _logger.LogWarning("Attachment-mode source {SourceId}: {N} pipeline(s) configured as 'inline' will be skipped. Set processing_mode='queue' to ingest attachments.",
                    _source.Id,
                    inlinePipelines.Count(p => allEligibleDocs.TryGetValue(p.Id, out var d) && d.Any(e => e.att is not null)));
            }
        }

        // Handle QUEUE pipelines: publish to Service Bus
        if (queuePipelines.Count > 0)
        {
            if (_sbPublisher?.IsEnabled == true)
            {
                var messages = new List<EmbeddingMessage>();
                foreach (var pipeline in queuePipelines)
                {
                    if (!allEligibleDocs.TryGetValue(pipeline.Id, out var pipelineDocs)) continue;
                    foreach (var (docId, content, contentHash, pkValue, doc, cfFields, att) in pipelineDocs)
                    {
                        var dest = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
                        var contentFields = new Dictionary<string, string>();
                        if (att is null)
                        {
                            foreach (var field in cfFields)
                            {
                                var token = doc[field];
                                if (token is not null && token.Type != Newtonsoft.Json.Linq.JTokenType.Null)
                                    contentFields[field] = token.Type == Newtonsoft.Json.Linq.JTokenType.String
                                        ? (string?)token ?? ""
                                        : token.ToString();
                            }
                        }

                        // Inject pipeline's vector_index_path into destination config
                        var destConfig = dest?.Config ?? new();
                        destConfig["vector_field"] = pipeline.VectorIndexPath;

                        var msg = new EmbeddingMessage
                        {
                            PipelineId = pipeline.Id,
                            PipelineName = pipeline.Name,
                            DocgrokPipeline = pipeline.DocgrokPipeline,
                            SourceId = _source.Id,
                            SourceRef = att is null ? docId : $"{docId}::{att.Name}",
                            DestinationId = pipeline.DestinationId,
                            DestinationType = dest?.Type ?? "cosmosdb-vector",
                            DestinationConfig = destConfig,
                            Content = content,
                            ContentHash = contentHash,
                            PartitionKeyValue = pkValue,
                            PipelineGeneration = pipeline.Generation,
                            SourceContentFields = contentFields,
                        };
                        if (att is not null)
                        {
                            msg.ContentType = "blob_ref";
                            msg.BlobAccountUrl = att.AccountUrl ?? _source.BlobAccountUrl;
                            msg.BlobConnectionString = _source.BlobConnectionString;
                            msg.BlobContainer = att.Container ?? _source.BlobContainer;
                            msg.BlobName = att.BlobName;
                        }
                        messages.Add(msg);
                    }
                }

                _logger.LogInformation("Service Bus: publishing {Count} messages for {Pipelines} pipeline(s)",
                    messages.Count, queuePipelines.Count);
                await _sbPublisher.PublishBatchAsync(messages, ct);
                jobsCreated = messages.Count;
            }
            else
            {
                // Legacy API job creation path
                var jobEntries = new List<CreateJobEntry>();
                foreach (var pipeline in queuePipelines)
                {
                    if (!allEligibleDocs.TryGetValue(pipeline.Id, out var pipelineDocs)) continue;
                    foreach (var (docId, content, contentHash, pkValue, doc, cfFields, att) in pipelineDocs)
                    {
                        var etag = doc["_etag"]?.Value<string>();
                        var metadata = new Dictionary<string, object>
                        {
                            ["trigger"] = "change_feed_dotnet",
                            ["_etag"] = etag ?? "",
                            ["partition"] = context.LeaseToken,
                            ["content"] = content,
                            ["content_hash"] = contentHash,
                            ["_pk_value"] = pkValue
                        };
                        if (att is not null)
                        {
                            metadata["content_type"] = "blob_ref";
                            metadata["blob_account_url"] = att.AccountUrl ?? _source.BlobAccountUrl ?? "";
                            metadata["blob_container"] = att.Container ?? _source.BlobContainer ?? "";
                            metadata["blob_name"] = att.BlobName;
                            metadata["attachment_name"] = att.Name;
                        }
                        jobEntries.Add(new CreateJobEntry
                        {
                            PipelineId = pipeline.Id,
                            SourceId = _source.Id,
                            SourceRef = att is null ? docId : $"{docId}::{att.Name}",
                            Metadata = metadata
                        });
                    }
                }

                _logger.LogInformation("Queue mode (legacy): {Jobs} jobs for {Pipelines} pipeline(s)",
                    jobEntries.Count, queuePipelines.Count);
                for (int i = 0; i < jobEntries.Count; i += 50)
                {
                    var batch = jobEntries.Skip(i).Take(50).ToList();
                    await CreateJobsWithRetryAsync(batch, ct);
                }
                jobsCreated = jobEntries.Count;
            }
        }

        // Report changefeed metrics after all processing
        _ = _apiClient.ReportChangeFeedMetricsAsync(
            _source.Id, changes.Count, totalEligible, skippedNoContent, skippedUnchanged,
            jobsCreated, context.LeaseToken, CancellationToken.None);
    }

    /// <summary>
    /// Inline processing: call DocGrok /embed/batch, then patch source documents directly.
    /// Completely bypasses the job queue for maximum throughput.
    /// Phase 1: Embed all docs in sub-batches of 100.
    /// Phase 2: Group by partition key and patch via TransactionalBatch with 429 retry.
    /// </summary>
    private async Task ProcessInlineAsync(
        List<Pipeline> inlinePipelines,
        List<(string docId, string content, string contentHash, string pkValue, JObject doc)> docs,
        string partition,
        CancellationToken ct)
    {
        foreach (var pipeline in inlinePipelines)
        {
            // Use smaller batches for external models to avoid rate limits
            // Native models (bge-small supports 256 per batch) can use larger batches
            int embedSubBatchSize = pipeline.DocgrokPipeline.StartsWith("mdl-ext-") ? 50 : 250;
            // Run multiple sub-batches concurrently so embed RTT + GPU work
            // overlaps across BGE replicas. With 3 BGE replicas this multiplies
            // per-partition throughput ~3-4x (was strictly serial before).
            int embedConcurrency = pipeline.DocgrokPipeline.StartsWith("mdl-ext-") ? 2 : 4;

            try
            {
                var sw = System.Diagnostics.Stopwatch.StartNew();
                // Phase 1: Embed all docs in sub-batches, collect results.
                // Use a SemaphoreSlim to cap concurrency, fire Tasks per sub-batch.
                var embeddedSlots = new (string docId, string pkValue, JsonElement embedding, string contentHash)?[docs.Count];
                var gate = new SemaphoreSlim(embedConcurrency, embedConcurrency);
                var subBatchTasks = new List<Task>();

                for (int offset = 0; offset < docs.Count; offset += embedSubBatchSize)
                {
                    int subOffset = offset;
                    var chunk = docs.Skip(subOffset).Take(embedSubBatchSize).ToList();
                    var chunkTexts = chunk.Select(d => d.content).ToList();

                    await gate.WaitAsync(ct);
                    subBatchTasks.Add(Task.Run(async () =>
                    {
                        try
                        {
                            var chunkSw = System.Diagnostics.Stopwatch.StartNew();
                            object embedReq;
                            if (pipeline.DocgrokPipeline.StartsWith("mdl-"))
                            {
                                embedReq = new { model_id = pipeline.DocgrokPipeline, texts = chunkTexts };
                            }
                            else
                            {
                                embedReq = new { pipeline = pipeline.DocgrokPipeline, texts = chunkTexts };
                            }

                            const int MaxEmbedRetries = 20;
                            HttpResponseMessage resp = null!;
                            for (int embedAttempt = 1; embedAttempt <= MaxEmbedRetries; embedAttempt++)
                            {
                                try
                                {
                                    resp = await _docGrokClient.PostAsJsonAsync("/embed/batch", embedReq, ct);
                                    if ((int)resp.StatusCode == 429)
                                    {
                                        var retryAfter = resp.Headers.RetryAfter?.Delta ?? TimeSpan.FromSeconds(Math.Min(Math.Pow(2, embedAttempt), 60));
                                        _logger.LogWarning("Embed 429, attempt {Attempt}/{MaxRetries}, retry after {Delay}s", embedAttempt, MaxEmbedRetries, retryAfter.TotalSeconds);
                                        await Task.Delay(retryAfter, ct);
                                        continue;
                                    }
                                    if ((int)resp.StatusCode >= 500)
                                    {
                                        var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, embedAttempt), 60));
                                        _logger.LogWarning("Embed {Status}, attempt {Attempt}/{MaxRetries}, retry after {Delay}s", resp.StatusCode, embedAttempt, MaxEmbedRetries, delay.TotalSeconds);
                                        await Task.Delay(delay, ct);
                                        continue;
                                    }
                                    break;
                                }
                                catch (Exception ex) when (ex is not OperationCanceledException)
                                {
                                    if (embedAttempt >= MaxEmbedRetries)
                                    {
                                        _logger.LogError(ex, "CRITICAL: Embed failed after {MaxRetries} attempts — documents will NOT be embedded", MaxEmbedRetries);
                                        throw;
                                    }
                                    var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, embedAttempt), 60));
                                    _logger.LogWarning("Embed call failed: {Error}, attempt {Attempt}/{MaxRetries}, retry after {Delay}s", ex.Message, embedAttempt, MaxEmbedRetries, delay.TotalSeconds);
                                    await Task.Delay(delay, ct);
                                }
                            }
                            resp.EnsureSuccessStatusCode();

                            var resultJson = await resp.Content.ReadAsStringAsync(ct);
                            using var resultDoc = JsonDocument.Parse(resultJson);
                            var outputs = resultDoc.RootElement.GetProperty("outputs");

                            if (outputs.GetArrayLength() != chunk.Count)
                            {
                                _logger.LogError("CRITICAL: Inline embed mismatch: sent {Sent}, got {Got} — aborting batch to prevent data loss",
                                    chunk.Count, outputs.GetArrayLength());
                                throw new InvalidOperationException(
                                    $"Embed returned {outputs.GetArrayLength()} results for {chunk.Count} inputs — batch will retry");
                            }

                            for (int i = 0; i < chunk.Count; i++)
                            {
                                embeddedSlots[subOffset + i] = (chunk[i].docId, chunk[i].pkValue, outputs[i].Clone(), chunk[i].contentHash);
                            }

                            // Streaming progress: report each embedded sub-batch immediately so the
                            // UI counter advances every ~3s instead of waiting for the full batch.
                            var subKey = $"{partition}:{docs[0].docId}:{docs.Count}:emb:{subOffset}";
                            _ = _apiClient.ReportInlineMetricsAsync(pipeline.Id, chunk.Count, 0, chunkSw.ElapsedMilliseconds, subKey, CancellationToken.None);
                        }
                        finally
                        {
                            gate.Release();
                        }
                    }, ct));
                }

                await Task.WhenAll(subBatchTasks);
                // Compact slots into a flat list (preserving order) for patching.
                var embedded = new List<(string docId, string pkValue, JsonElement embedding, string contentHash)>(docs.Count);
                for (int i = 0; i < embeddedSlots.Length; i++)
                {
                    if (embeddedSlots[i].HasValue) embedded.Add(embeddedSlots[i]!.Value);
                }

                // Phase 2: Group by partition key, patch via TransactionalBatch
                var (patched, failed) = await PatchByPartitionBatchAsync(embedded, pipeline, ct);

                sw.Stop();
                _logger.LogInformation(
                    "Inline complete: {Patched}/{Total} docs patched ({Failed} failed) for pipeline={Pipeline} on partition {Partition} in {Elapsed}ms",
                    patched, docs.Count, failed, pipeline.Name, partition, sw.ElapsedMilliseconds);

                // If any documents failed to patch, throw to prevent checkpoint — batch will retry
                if (failed > 0)
                {
                    throw new InvalidOperationException(
                        $"Patch failed for {failed}/{docs.Count} documents in pipeline {pipeline.Name} — aborting to prevent data loss");
                }

                // Report only failures at batch end. Successes were already streamed
                // per sub-batch above so the UI counter advances in near-real time.
                if (failed > 0)
                {
                    var batchKey = $"{partition}:{docs[0].docId}:{docs.Count}:fail";
                    _ = _apiClient.ReportInlineMetricsAsync(pipeline.Id, 0, failed, sw.ElapsedMilliseconds, batchKey, CancellationToken.None);
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Inline processing failed for pipeline {Pipeline} on partition {Partition}",
                    pipeline.Name, partition);
                // Re-throw so CFP does NOT checkpoint — batch will be retried
                throw;
            }
        }
    }

    /// <summary>
    /// Groups docs by partition key and patches each group via TransactionalBatch.
    /// Reduces network round-trips: N docs across P partitions → P requests instead of N.
    /// </summary>
    // TransactionalBatch limit: 100 operations per batch
    private const int MaxBatchOps = 100;

    private async Task<(int patched, int failed)> PatchByPartitionBatchAsync(
        List<(string docId, string pkValue, JsonElement embedding, string contentHash)> docs,
        Pipeline pipeline,
        CancellationToken ct)
    {
        if (_sourceContainer is null) return (0, docs.Count);

        // Group by partition key, then chunk into max 100 ops per batch
        var groups = docs.GroupBy(d => d.pkValue);
        var tasks = new List<Task<(int ok, int fail)>>();
        foreach (var g in groups)
        {
            var items = g.ToList();
            for (int i = 0; i < items.Count; i += MaxBatchOps)
            {
                var chunk = items.Skip(i).Take(MaxBatchOps).ToList();
                tasks.Add(PatchPartitionWithRetryAsync(g.Key, chunk, pipeline, ct));
            }
        }

        var results = await Task.WhenAll(tasks);
        int totalOk = results.Sum(r => r.ok);
        int totalFail = results.Sum(r => r.fail);
        return (totalOk, totalFail);
    }

    private async Task<(int ok, int fail)> PatchPartitionWithRetryAsync(
        string pkValue,
        List<(string docId, string pkValue, JsonElement embedding, string contentHash)> docs,
        Pipeline pipeline,
        CancellationToken ct)
    {
        var pk = new PartitionKey(pkValue);
        var now = DateTime.UtcNow.ToString("O");

        for (int attempt = 1; attempt <= MaxPatchRetries; attempt++)
        {
            await _patchThrottle.WaitAsync(ct);
            try
            {
                var batch = _sourceContainer!.CreateTransactionalBatch(pk);
                foreach (var (docId, _, embedding, contentHash) in docs)
                {
                    var floats = EmbeddingToFloatList(embedding);
                    var ops = new List<PatchOperation>
                    {
                        PatchOperation.Set($"/{pipeline.VectorIndexPath}", floats),                        PatchOperation.Set("/embedded_at", now),
                        PatchOperation.Set("/embedding_dims", floats.Count),
                        PatchOperation.Set("/pipeline_id", pipeline.Id),
                        PatchOperation.Set("/pipeline_name", pipeline.Name),
                        PatchOperation.Set("/content_hash", contentHash),
                    };
                    batch.PatchItem(docId, ops);
                }

                using var response = await batch.ExecuteAsync(ct);
                if (response.IsSuccessStatusCode)
                    return (docs.Count, 0);

                var statusCode = (int)response.StatusCode;
                if (statusCode == 429 || statusCode == 408 || statusCode == 503 || statusCode >= 500)
                {
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning(
                        "Batch {Status} pk={PK} ({Count} docs), attempt {Attempt}, retrying in {Delay}ms",
                        response.StatusCode, pkValue, docs.Count, attempt, delay.TotalMilliseconds);
                    await Task.Delay(delay, ct);
                    continue;
                }

                _logger.LogWarning("Batch failed (non-retryable): pk={PK}, status={Status}, {Count} docs",
                    pkValue, response.StatusCode, docs.Count);
                return (0, docs.Count);
            }
            catch (CosmosException ex) when (
                ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                ex.StatusCode == System.Net.HttpStatusCode.ServiceUnavailable ||
                (int)ex.StatusCode >= 500)
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Batch exception {Status} pk={PK}, attempt {Attempt}, retrying in {Delay}ms",
                    ex.StatusCode, pkValue, attempt, delay.TotalMilliseconds);
                await Task.Delay(delay, ct);
            }
            catch (Exception ex) when (ex is System.IO.IOException || ex is System.Net.Http.HttpRequestException || ex is TaskCanceledException)
            {
                // Transient network errors — retry with backoff
                var delay = TimeSpan.FromMilliseconds(Math.Min(1000 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Batch transient error pk={PK}: {Error}, attempt {Attempt}/{Max}, retrying in {Delay}ms",
                    pkValue, ex.Message, attempt, MaxPatchRetries, delay.TotalMilliseconds);
                await Task.Delay(delay, ct);
            }
            catch (Exception ex)
            {
                // Non-transient error — fail immediately, don't waste retries
                _logger.LogError(ex, "CRITICAL: Batch non-transient error pk={PK}, failing immediately", pkValue);
                _patchThrottle.Release();
                return (0, docs.Count);
            }
            finally
            {
                _patchThrottle.Release();
            }
        }
        _logger.LogError("CRITICAL: Patch failed after {MaxRetries} attempts for pk={PK}, {Count} documents NOT embedded",
            MaxPatchRetries, pkValue, docs.Count);
        return (0, docs.Count);
    }


    /// <summary>Convert embedding JsonElement to List of floats for CosmosDB PatchOperation.</summary>
    private static List<float> EmbeddingToFloatList(JsonElement embedding)
    {
        // Flatten: if [[...]], take inner array
        var arr = embedding;
        if (arr.ValueKind == JsonValueKind.Array && arr.GetArrayLength() > 0 &&
            arr[0].ValueKind == JsonValueKind.Array)
            arr = arr[0];

        var result = new List<float>(arr.GetArrayLength());
        foreach (var val in arr.EnumerateArray())
            result.Add(val.GetSingle());
        return result;
    }

    /// <summary>Returns true if patch succeeded, false if failed after retries.</summary>
    private async Task<bool> PatchDocumentAsync(
        string docId, string pkValue, JsonElement embedding,
        string contentHash, Pipeline pipeline, CancellationToken ct)
    {
        if (_sourceContainer is null) return false;

        var floats = EmbeddingToFloatList(embedding);

        var ops = new List<PatchOperation>
        {
            PatchOperation.Set($"/{pipeline.VectorIndexPath}", floats),
            PatchOperation.Set("/embedded_at", DateTime.UtcNow.ToString("O")),
            PatchOperation.Set("/embedding_dims", floats.Count),
            PatchOperation.Set("/pipeline_id", pipeline.Id),
            PatchOperation.Set("/pipeline_name", pipeline.Name),
            PatchOperation.Set("/content_hash", contentHash),
        };

        var pk = string.IsNullOrEmpty(pkValue) ? new PartitionKey(docId) : new PartitionKey(pkValue);

        await _patchThrottle.WaitAsync(ct);
        try
        {
            for (int attempt = 1; attempt <= MaxPatchRetries; attempt++)
            {
                try
                {
                    await _sourceContainer.PatchItemAsync<JObject>(docId, pk, ops, cancellationToken: ct);
                    return true;
                }
                catch (CosmosException ex) when (
                    ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                    ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                    ex.StatusCode == System.Net.HttpStatusCode.ServiceUnavailable ||
                    (int)ex.StatusCode >= 500)
                {
                    var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(200 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Patch {Status} doc={DocId}, attempt {Attempt}, retrying in {Delay}ms",
                        ex.StatusCode, docId, attempt, delay.TotalMilliseconds);
                    await Task.Delay(delay, ct);
                }
                catch (Exception ex)
                {
                    var delay = TimeSpan.FromMilliseconds(Math.Min(1000 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Patch error doc={DocId}: {Error}, attempt {Attempt}, retrying in {Delay}ms",
                        docId, ex.Message, attempt, delay.TotalMilliseconds);
                    await Task.Delay(delay, ct);
                }
            }
            return false;
        }
        finally
        {
            _patchThrottle.Release();
        }
    }

    private async Task CreateJobsWithRetryAsync(List<CreateJobEntry> entries, CancellationToken ct)
    {
        for (int attempt = 1; attempt <= _options.MaxJobCreationRetries; attempt++)
        {
            try
            {
                var (created, skipped) = await _apiClient.CreateJobsBulkAsync(
                    new CreateJobsRequest { Jobs = entries }, ct);
                _logger.LogDebug("Bulk jobs: created={Created}, skipped={Skipped}", created, skipped);
                return;
            }
            catch (Exception ex) when (attempt < _options.MaxJobCreationRetries)
            {
                _logger.LogWarning(ex,
                    "Job creation attempt {Attempt}/{Max} failed, retrying",
                    attempt, _options.MaxJobCreationRetries);
                await Task.Delay(TimeSpan.FromSeconds(attempt * 2), ct);
            }
        }
        // All retries failed — MUST throw to prevent checkpoint and document loss
        _logger.LogError("CRITICAL: Job creation failed after {Max} attempts — throwing to prevent checkpoint",
            _options.MaxJobCreationRetries);
        throw new InvalidOperationException(
            $"Job creation failed after {_options.MaxJobCreationRetries} attempts for {entries.Count} documents — aborting to prevent data loss");
    }

    private Task HandleErrorAsync(string leaseToken, Exception exception)
    {
        _logger.LogError(exception,
            "CFP error on source {SourceId}, partition {Partition}",
            _source.Id, leaseToken);
        return Task.CompletedTask;
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
    }
}
