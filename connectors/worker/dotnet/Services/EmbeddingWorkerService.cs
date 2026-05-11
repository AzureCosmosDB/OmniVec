using System.Diagnostics;
using System.Text.Json;
using Azure.Messaging.ServiceBus;
using Microsoft.Extensions.Options;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Models;

namespace OmniVec.Worker.Services;

/// <summary>
/// Core worker: pulls messages from Service Bus in batches, groups by model,
/// calls DocGrok /embed/batch, writes to destination, reports metrics.
/// Uses ServiceBusReceiver for explicit batch control and message completion.
/// </summary>
public class EmbeddingWorkerService : BackgroundService
{
    private readonly WorkerOptions _options;
    private readonly ServiceBusClient? _sbClient;
    private readonly DocGrokClient _docGrok;
    private readonly MetricsReporter _metrics;
    private readonly Dictionary<string, IDestinationWriter> _writers;
    private readonly ILogger<EmbeddingWorkerService> _logger;

    public EmbeddingWorkerService(
        IOptions<WorkerOptions> options,
        ServiceBusClient? sbClient,
        DocGrokClient docGrok,
        MetricsReporter metrics,
        IEnumerable<IDestinationWriter> writers,
        ILogger<EmbeddingWorkerService> logger)
    {
        _options = options.Value;
        _sbClient = sbClient;
        _docGrok = docGrok;
        _metrics = metrics;
        _writers = writers.ToDictionary(w => w.DestinationType);
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (_sbClient is null)
        {
            _logger.LogWarning("Service Bus not configured — worker idle. Set Worker__ServiceBusNamespace to enable queue processing.");
            // Mark ready so the pod becomes Ready in k8s (idle is a legitimate
            // steady state — helm --wait must not hang on an optional worker).
            WorkerHeartbeat.MarkReady();
            // Stay alive but idle — don't crash the pod
            await Task.Delay(Timeout.Infinite, ct);
            return;
        }

        _logger.LogInformation(
            "Embedding worker starting: topic={Topic}, sub={Sub}, batchSize={BatchSize}",
            _options.TopicName, _options.SubscriptionName, _options.EmbedBatchSize);

        var receiver = _sbClient.CreateReceiver(
            _options.TopicName,
            _options.SubscriptionName,
            new ServiceBusReceiverOptions
            {
                PrefetchCount = _options.EmbedBatchSize,
                ReceiveMode = ServiceBusReceiveMode.PeekLock,
            });

        // Receiver constructed successfully — we can serve. Marking ready here
        // (not on first message) so an empty queue does not keep the pod 0/1
        // and cause helm --wait to hang for 25 minutes.
        WorkerHeartbeat.MarkReady();

        // Run multiple concurrent receive loops
        var tasks = Enumerable.Range(0, _options.MaxConcurrentCalls)
            .Select(_ => ReceiveLoopAsync(receiver, ct))
            .ToArray();

        await Task.WhenAll(tasks);

        await receiver.DisposeAsync();
        _logger.LogInformation("Embedding worker stopped");
    }

    private async Task ReceiveLoopAsync(ServiceBusReceiver receiver, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                // Heartbeat: we're alive and about to receive. Keeps liveness probe happy
                // even when the subscription is empty.
                WorkerHeartbeat.Beat();

                // Pull a batch of messages
                var sbMessages = await receiver.ReceiveMessagesAsync(
                    _options.EmbedBatchSize,
                    TimeSpan.FromMilliseconds(_options.BatchAccumulateMs),
                    ct);

                if (sbMessages.Count == 0) continue;

                // First message received ever → mark ready.
                WorkerHeartbeat.MarkReceivedFirstMessage();

                // Deserialize and pair with SB messages
                var items = new List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)>();
                foreach (var sbMsg in sbMessages)
                {
                    try
                    {
                        var msg = JsonSerializer.Deserialize<EmbeddingMessage>(sbMsg.Body.ToString());
                        if (msg is null)
                        {
                            _logger.LogWarning("Could not deserialize message {MessageId}, dead-lettering", sbMsg.MessageId);
                            await receiver.DeadLetterMessageAsync(sbMsg, "InvalidMessage", "Could not deserialize", ct);
                            continue;
                        }
                        items.Add((msg, sbMsg));
                    }
                    catch (Exception ex)
                    {
                        _logger.LogWarning(ex, "Failed to deserialize message {MessageId}", sbMsg.MessageId);
                        await receiver.DeadLetterMessageAsync(sbMsg, "DeserializationError", ex.Message, ct);
                    }
                }

                if (items.Count == 0) continue;

                // Split blob_ref messages from text messages — they have different processing paths
                var blobItems = items.Where(i => i.msg.ContentType == "blob_ref").ToList();
                var textItems = items.Where(i => i.msg.ContentType != "blob_ref").ToList();

                // Process blob messages individually (each blob = multiple chunks)
                foreach (var item in blobItems)
                {
                    await ProcessBlobMessageAsync(receiver, item, ct);
                }

                // Group text messages by model for batch embedding
                var byModel = textItems.GroupBy(i => i.msg.DocgrokPipeline).ToList();

                foreach (var group in byModel)
                {
                    // Pre-validate each message against single-text token limit,
                    // then pack into token-bounded sub-batches before sending.
                    var validated = await ValidateAndTruncateAsync(receiver, group.ToList(), ct);
                    foreach (var subBatch in PackByTokenBudget(validated))
                    {
                        await ProcessBatchAsync(receiver, subBatch, ct);
                    }
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error in receive loop, backing off 5s");
                try { await Task.Delay(5000, ct); } catch { break; }
            }
        }
    }

    /// <summary>
    /// Process a single blob_ref message: send blob reference to DocGrok,
    /// which downloads + chunks + embeds the PDF, then write all chunks to destination.
    /// </summary>
    private async Task ProcessBlobMessageAsync(
        ServiceBusReceiver receiver,
        (EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg) item,
        CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var msg = item.msg;

        try
        {
            _logger.LogInformation("Processing blob: {BlobName} for pipeline={Pipeline}",
                msg.BlobName, msg.PipelineName);

            var chunks = await _docGrok.EmbedBlobAsync(
                msg.DocgrokPipeline,
                msg.BlobAccountUrl ?? "",
                msg.BlobConnectionString,
                msg.BlobContainer ?? "",
                msg.BlobName ?? "",
                ct);

            if (chunks.Count == 0)
            {
                _logger.LogWarning("DocGrok returned 0 chunks for blob {BlobName}, completing message", msg.BlobName);
                await receiver.CompleteMessageAsync(item.sbMsg, ct);
                return;
            }

            // Build results — one per chunk, with chunk index in doc ID
            var results = new List<EmbeddingResult>();
            for (int i = 0; i < chunks.Count; i++)
            {
                var (chunkText, embedding) = chunks[i];
                var docId = chunks.Count == 1
                    ? msg.SourceRef
                    : $"{msg.SourceRef}#chunk{i}";

                results.Add(new EmbeddingResult(
                    DocId: docId,
                    SourceRef: msg.SourceRef,
                    Embedding: embedding,
                    ContentHash: msg.ContentHash,
                    PartitionKeyValue: msg.PartitionKeyValue,
                    PipelineId: msg.PipelineId,
                    PipelineName: msg.PipelineName,
                    PipelineGeneration: msg.PipelineGeneration,
                    Content: chunkText,
                    SourceContentFields: new Dictionary<string, string>(),
                    SourceId: msg.SourceId));
            }

            // Write to destination
            if (_writers.TryGetValue(msg.DestinationType, out var writer))
            {
                await writer.WriteBatchAsync(msg.DestinationConfig, results, ct);
            }
            else
            {
                _logger.LogError("No writer for destination type {Type}", msg.DestinationType);
            }

            await receiver.CompleteMessageAsync(item.sbMsg, ct);

            sw.Stop();
            _logger.LogInformation(
                "Blob processed: {BlobName} → {ChunkCount} chunks for pipeline={Pipeline} in {Elapsed}ms",
                msg.BlobName, chunks.Count, msg.PipelineName, sw.ElapsedMilliseconds);

            _ = _metrics.ReportInlineMetricsAsync(
                msg.PipelineId, chunks.Count, 0, sw.ElapsedMilliseconds,
                $"blob:{msg.SourceRef}:{chunks.Count}");
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to process blob {BlobName}", msg.BlobName);
            try { await receiver.AbandonMessageAsync(item.sbMsg, cancellationToken: ct); }
            catch { /* ignore */ }
        }
    }

    private async Task ProcessBatchAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> batch,
        CancellationToken ct)
    {
        if (batch.Count == 0) return;

        var sw = Stopwatch.StartNew();
        var modelKey = batch[0].msg.DocgrokPipeline;
        var pipelineId = batch[0].msg.PipelineId;

        try
        {
            // Phase 1: Batch embed with bisect-on-4xx (oversized inputs that
            // slipped past pre-validation are isolated and dead-lettered, rather
            // than poisoning the whole batch).
            var texts = batch.Select(b => b.msg.Content).ToList();
            _logger.LogInformation(
                "Embedding {Count} docs (~{Tokens} tokens) for model={Model}, pipeline={Pipeline}",
                texts.Count, texts.Sum(TokenEstimator.Estimate), modelKey, batch[0].msg.PipelineName);

            var embeddings = await EmbedWithBisectAsync(receiver, batch, ct);
            if (embeddings is null) return; // entire batch rejected & dead-lettered

            if (embeddings.Count != batch.Count)
            {
                _logger.LogError("Embed count mismatch: sent {Sent}, got {Got}. Abandoning batch.", batch.Count, embeddings.Count);
                foreach (var (_, sbMsg) in batch)
                {
                    try { await receiver.AbandonMessageAsync(sbMsg, cancellationToken: ct); }
                    catch { /* ignore */ }
                }
                return;
            }

            // Phase 2: Build results and group by destination
            var resultsByDest = new Dictionary<string, (string destType, Dictionary<string, object> config, List<EmbeddingResult> results)>();

            for (int i = 0; i < batch.Count; i++)
            {
                var msg = batch[i].msg;
                var embedding = embeddings[i];

                var result = new EmbeddingResult(
                    DocId: msg.SourceRef,
                    SourceRef: msg.SourceRef,
                    Embedding: embedding,
                    ContentHash: msg.ContentHash,
                    PartitionKeyValue: msg.PartitionKeyValue,
                    PipelineId: msg.PipelineId,
                    PipelineName: msg.PipelineName,
                    PipelineGeneration: msg.PipelineGeneration,
                    Content: msg.Content,
                    SourceContentFields: msg.SourceContentFields,
                    SourceId: msg.SourceId);

                var destKey = msg.DestinationId;
                if (!resultsByDest.ContainsKey(destKey))
                    resultsByDest[destKey] = (msg.DestinationType, msg.DestinationConfig, new List<EmbeddingResult>());
                resultsByDest[destKey].results.Add(result);
            }

            // Phase 3: Write to destinations
            foreach (var (destId, (destType, config, results)) in resultsByDest)
            {
                if (!_writers.TryGetValue(destType, out var writer))
                {
                    _logger.LogError("No writer for destination type {Type}", destType);
                    continue;
                }

                await writer.WriteBatchAsync(config, results, ct);
            }

            // Phase 4: Complete all messages
            foreach (var (_, sbMsg) in batch)
            {
                try { await receiver.CompleteMessageAsync(sbMsg, ct); }
                catch (Exception ex) { _logger.LogWarning("Could not complete message: {Error}", ex.Message); }
            }

            sw.Stop();
            _logger.LogInformation(
                "Worker batch complete: {Count} docs for pipeline={Pipeline} in {Elapsed}ms",
                batch.Count, batch[0].msg.PipelineName, sw.ElapsedMilliseconds);

            // Phase 5: Report metrics
            var batchKey = $"worker:{batch[0].msg.SourceRef}:{batch.Count}:{sw.ElapsedMilliseconds}";
            _ = _metrics.ReportInlineMetricsAsync(pipelineId, batch.Count, 0, sw.ElapsedMilliseconds, batchKey);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to process batch of {Count} for model={Model}", batch.Count, modelKey);
            // Abandon all messages so Service Bus retries them
            foreach (var (_, sbMsg) in batch)
            {
                try { await receiver.AbandonMessageAsync(sbMsg, cancellationToken: ct); }
                catch { /* ignore */ }
            }
        }
    }

    /// <summary>
    /// Pre-validate text messages against the per-input token ceiling. Oversized
    /// inputs are either truncated (proportional to estimated tokens) or
    /// dead-lettered immediately, so they never enter the batch packer or
    /// trigger a 4xx round-trip.
    /// </summary>
    private async Task<List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)>> ValidateAndTruncateAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> items,
        CancellationToken ct)
    {
        var kept = new List<(EmbeddingMessage, ServiceBusReceivedMessage)>(items.Count);
        foreach (var item in items)
        {
            var content = item.msg.Content ?? "";
            var tokens = TokenEstimator.Estimate(content);
            if (tokens <= _options.MaxSingleTextTokens)
            {
                kept.Add(item);
                continue;
            }

            if (_options.TruncateOversized)
            {
                // Truncate by chars, scaled to fit under the limit with 5% slack.
                var ratio = (double)_options.MaxSingleTextTokens * 0.95 / tokens;
                var newLen = Math.Max(1, (int)(content.Length * ratio));
                item.msg.Content = content.Substring(0, newLen);
                _logger.LogWarning(
                    "Truncated oversized input {SourceRef}: {OrigChars} chars (~{OrigTokens} tok) → {NewChars} chars",
                    item.msg.SourceRef, content.Length, tokens, newLen);
                kept.Add(item);
            }
            else
            {
                _logger.LogWarning(
                    "Dead-lettering oversized input {SourceRef}: ~{Tokens} tokens > {Max}",
                    item.msg.SourceRef, tokens, _options.MaxSingleTextTokens);
                try
                {
                    await receiver.DeadLetterMessageAsync(
                        item.sbMsg,
                        "TokenLimitExceeded",
                        $"Estimated {tokens} tokens exceeds MaxSingleTextTokens={_options.MaxSingleTextTokens}",
                        ct);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning("Failed to dead-letter {Id}: {Error}", item.sbMsg.MessageId, ex.Message);
                }
            }
        }
        return kept;
    }

    /// <summary>
    /// Pack messages into sub-batches that respect both the message-count cap
    /// (EmbedBatchSize) and the token budget (MaxBatchTokens). A single
    /// oversized message that already passed validation always forms its own
    /// sub-batch.
    /// </summary>
    private IEnumerable<List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)>> PackByTokenBudget(
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> items)
    {
        var current = new List<(EmbeddingMessage, ServiceBusReceivedMessage)>();
        int currentTokens = 0;

        foreach (var item in items)
        {
            int t = TokenEstimator.Estimate(item.msg.Content);
            bool wouldOverflow =
                current.Count >= _options.EmbedBatchSize ||
                (current.Count > 0 && currentTokens + t > _options.MaxBatchTokens);

            if (wouldOverflow)
            {
                yield return current;
                current = new List<(EmbeddingMessage, ServiceBusReceivedMessage)>();
                currentTokens = 0;
            }

            current.Add(item);
            currentTokens += t;
        }

        if (current.Count > 0) yield return current;
    }

    /// <summary>
    /// Call /embed/batch with bisect-and-dead-letter on non-retryable client
    /// errors. If the embed endpoint returns 4xx for the batch, we split it
    /// in half and recurse; a single-message batch that still fails is
    /// dead-lettered (and removed from <paramref name="batch"/>) so the rest
    /// of the workflow can proceed.
    /// Returns the embeddings aligned 1:1 with the (possibly shrunk)
    /// <paramref name="batch"/>, or null if every message was dead-lettered.
    /// </summary>
    private async Task<List<float[]>?> EmbedWithBisectAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> batch,
        CancellationToken ct)
    {
        var modelKey = batch[0].msg.DocgrokPipeline;
        var texts = batch.Select(b => b.msg.Content).ToList();

        try
        {
            return await _docGrok.EmbedBatchAsync(modelKey, texts, ct);
        }
        catch (EmbeddingClientException ex)
        {
            if (batch.Count == 1)
            {
                var only = batch[0];

                // Most common cause of a single-message 400 is "context length
                // exceeded" — our token estimate was off. Try truncating to half
                // and re-embedding once before giving up.
                if (_options.TruncateOversized && only.msg.Content?.Length > 1)
                {
                    var orig = only.msg.Content;
                    only.msg.Content = orig.Substring(0, orig.Length / 2);
                    _logger.LogWarning(
                        "Embed rejected {SourceRef} with {Status}; truncating {OrigLen}→{NewLen} chars and retrying once.",
                        only.msg.SourceRef, ex.StatusCode, orig.Length, only.msg.Content.Length);
                    try
                    {
                        return await _docGrok.EmbedBatchAsync(modelKey, new List<string> { only.msg.Content }, ct);
                    }
                    catch (EmbeddingClientException ex2)
                    {
                        ex = ex2; // fall through to dead-letter with the second error
                    }
                }

                _logger.LogError(
                    "Embed rejected single message {SourceRef} with {Status}: {Msg}. Dead-lettering.",
                    only.msg.SourceRef, ex.StatusCode, ex.Message);
                try
                {
                    await receiver.DeadLetterMessageAsync(
                        only.sbMsg,
                        $"EmbedRejected{ex.StatusCode}",
                        ex.Message.Length > 4000 ? ex.Message.Substring(0, 4000) : ex.Message,
                        ct);
                }
                catch (Exception dlqEx)
                {
                    _logger.LogWarning("Failed to dead-letter {Id}: {Error}",
                        only.sbMsg.MessageId, dlqEx.Message);
                }
                batch.Clear();
                return null;
            }

            // Bisect: split, recurse on each half, stitch results back together
            // in original order. Any message that gets dead-lettered is removed
            // from its half, and we mirror those removals in `batch`.
            int mid = batch.Count / 2;
            var left = batch.GetRange(0, mid);
            var right = batch.GetRange(mid, batch.Count - mid);

            _logger.LogWarning(
                "Embed returned {Status} for batch of {Count}; bisecting into {Left}+{Right}",
                ex.StatusCode, batch.Count, left.Count, right.Count);

            var leftEmb = await EmbedWithBisectAsync(receiver, left, ct);
            var rightEmb = await EmbedWithBisectAsync(receiver, right, ct);

            // Rebuild batch from the surviving items of each half
            batch.Clear();
            batch.AddRange(left);
            batch.AddRange(right);
            if (batch.Count == 0) return null;

            var combined = new List<float[]>(batch.Count);
            if (leftEmb is not null) combined.AddRange(leftEmb);
            if (rightEmb is not null) combined.AddRange(rightEmb);
            return combined;
        }
    }
}
