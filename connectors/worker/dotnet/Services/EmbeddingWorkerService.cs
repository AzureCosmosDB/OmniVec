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
                    await ProcessBatchAsync(receiver, group.ToList(), ct);
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
                    SourceContentFields: new Dictionary<string, string>()));
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
        var sw = Stopwatch.StartNew();
        var modelKey = batch[0].msg.DocgrokPipeline;
        var pipelineId = batch[0].msg.PipelineId;

        try
        {
            // Phase 1: Batch embed
            var texts = batch.Select(b => b.msg.Content).ToList();
            _logger.LogInformation("Embedding {Count} docs for model={Model}, pipeline={Pipeline}",
                texts.Count, modelKey, batch[0].msg.PipelineName);

            var embeddings = await _docGrok.EmbedBatchAsync(modelKey, texts, ct);

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
                    SourceContentFields: msg.SourceContentFields);

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
}
