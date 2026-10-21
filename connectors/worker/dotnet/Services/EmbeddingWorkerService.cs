using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure.Messaging.ServiceBus;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Options;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Models;
using OmniVec.Worker.Lanes;
using Azure.Messaging.ServiceBus.Administration;

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
    private readonly SharePointContentClient _sharePoint;
    private readonly MetricsReporter _metrics;
    private readonly Dictionary<string, IDestinationWriter> _writers;
    private readonly ILogger<EmbeddingWorkerService> _logger;
    private readonly SemaphoreSlim _documentGate;
    private readonly SemaphoreSlim _sharePointGate;
    private readonly SemaphoreSlim _textGate;
    private readonly SemaphoreSlim _queueEnvelopeGate;
    private readonly PipelineFairScheduler _pipelineScheduler;
    internal TimeSpan ProcessingBudget { get; set; }

    public EmbeddingWorkerService(
        IOptions<WorkerOptions> options,
        ServiceBusClient? sbClient,
        DocGrokClient docGrok,
        SharePointContentClient sharePoint,
        MetricsReporter metrics,
        IEnumerable<IDestinationWriter> writers,
        ILogger<EmbeddingWorkerService> logger)
    {
        _options = options.Value;
        if (_options.PipelineLanesEnabled)
        {
            PipelineLaneProtocol.ValidateTopicName(_options.PipelineLanesTopicName);
            if (sbClient is null || string.IsNullOrWhiteSpace(_options.ServiceBusNamespace))
                throw new InvalidOperationException("Lane worker requires a configured Service Bus client and namespace");
        }
        if (_options.EmbedBatchSize <= 0 || _options.MaxReceiveBatchSize <= 0 || _options.MaxConcurrentCalls <= 0
            || _options.ReceivePumpCount <= 0 || _options.PrefetchCount < 0
            || _options.BlobConcurrency <= 0 || _options.BlobBatchSize <= 0
            || _options.SharePointConcurrency <= 0
            || _options.MaxProcessingMinutes <= 0 || _options.MaxLockRenewalMinutes <= 0
            || _options.MaxBatchTokens <= 0 || _options.MaxSingleTextTokens <= 0 || _options.BatchAccumulateMs <= 0
            || _options.DocGrokRequestTimeoutSeconds <= 0)
            throw new ArgumentOutOfRangeException(nameof(options), "Worker concurrency, batch sizes and processing budgets must be positive");
        _documentGate = new(_options.BlobConcurrency, _options.BlobConcurrency);
        _sharePointGate = new(_options.SharePointConcurrency, _options.SharePointConcurrency);
        _textGate = new(_options.MaxConcurrentCalls, _options.MaxConcurrentCalls);
        _queueEnvelopeGate = new(_options.MaxConcurrentCalls, _options.MaxConcurrentCalls);
        _pipelineScheduler = new(_options.MaxConcurrentCalls);
        ProcessingBudget = TimeSpan.FromMinutes(_options.MaxProcessingMinutes);
        _sbClient = sbClient;
        _docGrok = docGrok;
        _sharePoint = sharePoint;
        _metrics = metrics;
        _writers = writers.ToDictionary(w => w.DestinationType);
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (_options.PipelineLanesEnabled)
        {
            await RunLanesAsync(ct);
            return;
        }
        if (_sbClient is null)
        {
            _logger.LogWarning("Service Bus not configured — worker idle. Set Worker__ServiceBusNamespace to enable queue processing.");
            // Mark ready so the pod becomes Ready in k8s (idle is a legitimate
            // steady state — helm --wait must not hang on an optional worker).
            WorkerHeartbeat.MarkReady();
            // Stay alive but idle — don't crash the pod
            await RunIdleAsync(ct, TimeSpan.FromSeconds(30));
            return;
        }

        _logger.LogInformation(
            "Embedding worker starting: topic={Topic}, sub={Sub}, batchSize={BatchSize}, pumps={Pumps}, prefetch={Prefetch}",
            _options.TopicName, _options.SubscriptionName, _options.EmbedBatchSize,
            _options.ReceivePumpCount, _options.PrefetchCount);
        var pumps = Enumerable.Range(0, _options.ReceivePumpCount)
            .Select(index => RunReceivePumpAsync(index, ct)).ToArray();
        await Task.WhenAll(pumps);
        _logger.LogInformation("Embedding worker stopped");
    }

    private async Task RunReceivePumpAsync(int pumpIndex, CancellationToken ct)
    {
        await using var receiver = _sbClient!.CreateReceiver(
            _options.TopicName, _options.SubscriptionName,
            new ServiceBusReceiverOptions
            {
                PrefetchCount = _options.PrefetchCount,
                ReceiveMode = ServiceBusReceiveMode.PeekLock,
                Identifier = $"worker-{Environment.MachineName}-{pumpIndex}",
            });
        await ReceiveLoopAsync(receiver, ct);
    }

    private async Task RunLanesAsync(CancellationToken ct)
    {
        var registry = new PipelineLaneRegistry(new ServiceBusAdministrationClient(
            _options.ServiceBusNamespace, new Azure.Identity.DefaultAzureCredential()), _options.PipelineLanesTopicName);
        var running = new Dictionary<string, (LaneMetadata Metadata, CancellationTokenSource Stop, Task Task)>();
        try
        {
            while (!ct.IsCancellationRequested)
            {
                WorkerHeartbeat.Beat();
                // Invalid ownership/rules and discovery failures are fatal, not an empty discovery result.
                var lanes = await registry.DiscoverAsync(ct);
                foreach (var name in running.Keys.ToArray())
                {
                    var lane = running[name];
                    if (lane.Task.IsCompleted) await lane.Task;
                    if (!lanes.TryGetValue(name, out var metadata) || metadata != lane.Metadata)
                    {
                        lane.Stop.Cancel();
                        await lane.Task;
                        lane.Stop.Dispose();
                        running.Remove(name);
                    }
                }
                foreach (var (name, metadata) in lanes)
                {
                    if (running.ContainsKey(name)) continue;
                    var stop = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    running.Add(name, (metadata, stop, RunLaneAsync(name, metadata, stop.Token)));
                }
                // Successful marked-topic/rule discovery establishes broker readiness, including zero lanes.
                WorkerHeartbeat.MarkReady();
                await Task.Delay(TimeSpan.FromSeconds(5), ct);
            }
        }
        finally
        {
            WorkerHeartbeat.MarkNotReady();
            foreach (var lane in running.Values) lane.Stop.Cancel();
            try { await Task.WhenAll(running.Values.Select(lane => lane.Task)); }
            finally { foreach (var lane in running.Values) lane.Stop.Dispose(); }
        }
    }

    private async Task RunLaneAsync(string subscription, LaneMetadata lane, CancellationToken ct)
    {
        await using var receiver = _sbClient!.CreateReceiver(_options.PipelineLanesTopicName, subscription,
            new ServiceBusReceiverOptions { PrefetchCount = 0, ReceiveMode = ServiceBusReceiveMode.PeekLock });
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await LaneDispatch.RunOnceAsync(_pipelineScheduler, lane, async token =>
                {
                    WorkerHeartbeat.Beat();
                    var messages = await receiver.ReceiveMessagesAsync(_options.MaxReceiveBatchSize,
                        TimeSpan.FromMilliseconds(Math.Min(100, _options.BatchAccumulateMs)), token);
                    if (messages.Count == 0) return;
                    await using var scope = new MessageProcessingScope(receiver, messages,
                        TimeSpan.FromMinutes((long)_options.MaxProcessingMinutes + _options.MaxLockRenewalMinutes), token);
                    await ProcessLaneBatchAsync(scope.Receiver, messages, scope.Token, lane.PipelineId);
                }, ct);
                // Empty lanes release permits before delay; at most one pending admission per lane.
                await Task.Delay(TimeSpan.FromMilliseconds(100), ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Lane {PipelineId} receive/processing failed; unfinished messages remain broker-owned", lane.PipelineId);
                try { await Task.Delay(TimeSpan.FromSeconds(5), ct); }
                catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            }
        }
    }

    internal static async Task RunIdleAsync(CancellationToken ct, TimeSpan interval)
    {
        while (!ct.IsCancellationRequested)
        {
            WorkerHeartbeat.Beat();
            await Task.Delay(interval, ct);
        }
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
                    _options.MaxReceiveBatchSize,
                    TimeSpan.FromMilliseconds(_options.BatchAccumulateMs),
                    ct);

                WorkerHeartbeat.MarkReady();
                if (sbMessages.Count == 0) continue;

                await using var scope = new MessageProcessingScope(receiver, sbMessages,
                    TimeSpan.FromMinutes((long)_options.MaxProcessingMinutes + _options.MaxLockRenewalMinutes), ct);
                await ProcessReceivedBatchAsync(scope.Receiver, sbMessages, scope.Token);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (OperationCanceledException)
            {
                _logger.LogWarning("Received batch exceeded processing/lock budget; unfinished messages will be redelivered");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error in receive loop, backing off 5s");
                try { await Task.Delay(5000, ct); } catch { break; }
            }
        }
    }

    private async Task ProcessReceivedBatchAsync(ServiceBusReceiver receiver,
        IReadOnlyList<ServiceBusReceivedMessage> sbMessages, CancellationToken ct)
        => await ProcessLaneBatchAsync(receiver, sbMessages, ct, null);

    private async Task ProcessLaneBatchAsync(ServiceBusReceiver receiver,
        IReadOnlyList<ServiceBusReceivedMessage> sbMessages, CancellationToken ct, string? admittedPipelineId)
    {
        var legacy = new List<ServiceBusReceivedMessage>();
        var envelopes = new List<ServiceBusReceivedMessage>();
        var deadLetters = new List<(ServiceBusReceivedMessage Message, string Reason, string? Description)>();
        foreach (var message in sbMessages)
        {
            if (message.ApplicationProperties.TryGetValue(QueueEnvelopeReceiver.DeadLetterReasonProperty, out var reason)
                && reason is string deadLetterReason)
            {
                var description = message.ApplicationProperties.TryGetValue(
                    QueueEnvelopeReceiver.DeadLetterDescriptionProperty, out var detail) ? detail as string : null;
                deadLetters.Add((message, deadLetterReason, description));
                continue;
            }
            try
            {
                using var body = JsonDocument.Parse(message.Body);
                if (body.RootElement.ValueKind == JsonValueKind.Object
                    && body.RootElement.TryGetProperty("queue_envelope_version", out _))
                {
                    envelopes.Add(message);
                    continue;
                }
            }
            catch (JsonException) { /* The legacy validator records malformed deliveries. */ }
            legacy.Add(message);
        }
        var tasks = envelopes.Select(message => ProcessQueueEnvelopeAsync(
            receiver, message, ct, admittedPipelineId)).Concat(deadLetters.Select(item =>
                receiver.DeadLetterMessageAsync(item.Message, item.Reason, item.Description, ct))).ToList();
        if (legacy.Count > 0) tasks.Add(ProcessDocumentBatchAsync(receiver, legacy, ct, admittedPipelineId));
        await Task.WhenAll(tasks);
    }

    private async Task ProcessQueueEnvelopeAsync(ServiceBusReceiver receiver, ServiceBusReceivedMessage parent,
        CancellationToken ct, string? admittedPipelineId)
    {
        EmbeddingMessage head;
        int count;
        try
        {
            using var body = JsonDocument.Parse(parent.Body);
            var documents = body.RootElement.GetProperty("documents");
            if (documents.ValueKind != JsonValueKind.Array || documents.GetArrayLength() is < 1 or > 1000)
                throw new JsonException("Queue envelope document count must be from 1 to 1000");
            count = documents.GetArrayLength();
            head = documents[0].Deserialize<EmbeddingMessage>() ?? throw new JsonException("Invalid envelope document");
            if (string.IsNullOrWhiteSpace(head.PipelineId)) throw new JsonException("Envelope requires a pipeline ID");
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException)
        {
            _logger.LogError(ex, "Invalid queue envelope {MessageId}", parent.MessageId);
            await receiver.DeadLetterMessageAsync(parent, "InvalidQueueEnvelope", ex.Message, ct);
            return;
        }
        // Acquire pipeline admission before expansion: queued envelopes cannot hoard
        // document memory or consume the permits needed by an unrelated healthy pipeline.
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(TimeSpan.FromMinutes(_options.MaxLockRenewalMinutes));
        await using var admission = admittedPipelineId is null
            ? await _pipelineScheduler.AcquireAsync(head.PipelineId, head.ResourceWeight,
                head.MaxConcurrencyPerWorker, head.ResourcePriority, head.WorkloadClass, count, budget.Token)
            : null;
        using var processing = CancellationTokenSource.CreateLinkedTokenSource(ct);
        processing.CancelAfter(ProcessingBudget);
        await _queueEnvelopeGate.WaitAsync(processing.Token);
        try
        {
            QueueEnvelopeReceiver envelope;
            try { envelope = new QueueEnvelopeReceiver(receiver, parent, HandoffQueueDeadLetterAsync); }
            catch (JsonException ex)
            {
                _logger.LogError(ex, "Invalid queue envelope {MessageId}", parent.MessageId);
                await receiver.DeadLetterMessageAsync(parent, "InvalidQueueEnvelope", ex.Message, ct);
                return;
            }
            await ProcessDocumentBatchAsync(envelope, envelope.Documents, processing.Token,
                admittedPipelineId ?? head.PipelineId);
            await envelope.SettleParentAsync(processing.Token);
        }
        finally { _queueEnvelopeGate.Release(); }
    }

    private async Task HandoffQueueDeadLetterAsync(ServiceBusMessage message, CancellationToken ct)
    {
        if (_sbClient is null)
            throw new InvalidOperationException("Queue envelope dead-letter isolation requires a configured Service Bus client");
        var topic = _options.PipelineLanesEnabled ? _options.PipelineLanesTopicName : _options.TopicName;
        await using var sender = _sbClient.CreateSender(topic);
        await sender.SendMessageAsync(message, ct);
    }

    private async Task ProcessDocumentBatchAsync(ServiceBusReceiver receiver,
        IReadOnlyList<ServiceBusReceivedMessage> sbMessages, CancellationToken ct, string? admittedPipelineId)
    {
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
                            await DeadLetterWithMetricsAsync(receiver, sbMsg, "InvalidMessage", "Could not deserialize", ct);
                            continue;
                        }
                        if (admittedPipelineId is not null
                            && (msg.PipelineId != admittedPipelineId || sbMsg.Subject != admittedPipelineId))
                        {
                            await DeadLetterWithMetricsAsync(receiver, sbMsg, "InvalidLaneRouting",
                                "Body pipeline ID and broker subject must exactly match the admitted lane", ct);
                            continue;
                        }
                        if (string.IsNullOrWhiteSpace(msg.DestinationType) || !_writers.ContainsKey(msg.DestinationType))
                        {
                            await DeadLetterWithMetricsAsync(receiver, sbMsg, "UnknownDestinationType",
                                $"No writer for {msg.DestinationType}", ct);
                            continue;
                        }
                        ValidateRequestLimits(msg);
                        msg.PartitionKeyValue = IdentityTemplateRenderer.RenderPartitionKey(msg);
                        items.Add((msg, sbMsg));
                    }
                    catch (ArgumentException ex)
                    {
                        _logger.LogWarning(ex, "Invalid identity policy on message {MessageId}", sbMsg.MessageId);
                        await DeadLetterWithMetricsAsync(receiver, sbMsg, "InvalidIdentityPolicy", ex.Message, ct);
                    }
                    catch (JsonException ex)
                    {
                        _logger.LogWarning(ex, "Failed to deserialize message {MessageId}", sbMsg.MessageId);
                        await DeadLetterWithMetricsAsync(receiver, sbMsg, "DeserializationError", ex.Message, ct);
                    }
                }

                if (items.Count == 0) return;
                if (admittedPipelineId is not null)
                {
                    using var laneBudget = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    laneBudget.CancelAfter(ProcessingBudget);
                    await ProcessItemsAsync(receiver, items, laneBudget.Token);
                    return;
                }
                // A slow/unavailable pipeline must not hold unrelated pipelines in
                // the same receive batch behind its retries.
                await Task.WhenAll(items.GroupBy(item => item.msg.PipelineId)
                    .Select(async group =>
                    {
                        var groupedItems = group.ToList();
                        var head = groupedItems[0].msg;
                        using var admissionBudget = CancellationTokenSource.CreateLinkedTokenSource(ct);
                        admissionBudget.CancelAfter(TimeSpan.FromMinutes(_options.MaxLockRenewalMinutes));
                        await using var admission = await _pipelineScheduler.AcquireAsync(
                            head.PipelineId,
                            head.ResourceWeight,
                            head.MaxConcurrencyPerWorker,
                            head.ResourcePriority,
                            head.WorkloadClass,
                            groupedItems.Count,
                            admissionBudget.Token);
                        using var processingBudget = CancellationTokenSource.CreateLinkedTokenSource(ct);
                        processingBudget.CancelAfter(ProcessingBudget);
                        await ProcessItemsAsync(receiver, groupedItems, processingBudget.Token);
                    }));
    }

    private async Task ProcessItemsAsync(ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> items, CancellationToken ct)
    {
                // Split out delete messages (BlobDeleted via Event Grid). Deletes
                // are grouped by destination and dispatched to the writer's
                // DeleteByRefAsync — no embedding / DocGrok call needed.
                // SharePoint deletes use the same revision-fenced replacement path as upserts.
                var deleteItems = items.Where(i => i.msg.ContentType != "sharepoint_ref" && string.Equals(i.msg.MessageType, "delete",
                    StringComparison.OrdinalIgnoreCase)).ToList();
                items = items.Except(deleteItems).ToList();
                if (deleteItems.Count > 0)
                {
                    await ProcessDeleteBatchAsync(receiver, deleteItems, ct);
                }

                if (items.Count == 0) return;

                var sharePointItems = items.Where(i => i.msg.ContentType == "sharepoint_ref").ToList();
                items = items.Where(i => i.msg.ContentType != "sharepoint_ref").ToList();
                if (sharePointItems.Count > 0)
                {
                    var gate = _documentGate;
                    var tasks = sharePointItems.Select(async item =>
                    {
                        await _sharePointGate.WaitAsync(ct);
                        try
                        {
                            await gate.WaitAsync(ct);
                            try { await ProcessSharePointMessageAsync(receiver, item, ct); }
                            finally { gate.Release(); }
                        }
                        finally { _sharePointGate.Release(); }
                    });
                    await Task.WhenAll(tasks);
                }

                // Split blob_ref messages from text messages — they have different processing paths
                var blobItems = items.Where(i => i.msg.ContentType == "blob_ref").ToList();
                var textItems = items.Where(i => i.msg.ContentType != "blob_ref").ToList();

                // Bulk image (blob_ref) path. Group by (pipeline, account, container),
                // chunk into BlobBatchSize-sized batches, and post each batch as
                // a single bulk call (downloads + GPU run all happen server-side
                // in parallel). Falls back to the per-message ProcessBlobMessageAsync
                // path for any item that has no blob_name (e.g. PDFs where the
                // router needs to do OCR + chunking — those still go individually).
                if (blobItems.Count > 0)
                {
                    var batchable = blobItems.Where(i =>
                        !string.IsNullOrEmpty(i.msg.BlobName) &&
                        !string.IsNullOrEmpty(i.msg.BlobContainer) &&
                        !string.IsNullOrEmpty(i.msg.BlobAccountUrl) &&
                        !string.IsNullOrEmpty(i.msg.DocgrokPipeline) &&
                        i.msg.DocgrokPipeline.StartsWith("mdl-native-")).ToList();
                    var individual = blobItems.Except(batchable).ToList();

                    var groups = batchable.GroupBy(i => (
                        i.msg.DocgrokPipeline,
                        i.msg.BlobAccountUrl!,
                        i.msg.BlobContainer!
                    )).ToList();

                    var batchTasks = new List<Task>();
                    var gate = _documentGate;
                    foreach (var g in groups)
                    {
                        var list = g.ToList();
                        for (int i = 0; i < list.Count; i += _options.BlobBatchSize)
                        {
                            var slice = list.Skip(i).Take(_options.BlobBatchSize).ToList();
                            await gate.WaitAsync(ct);
                            batchTasks.Add(Task.Run(async () =>
                            {
                                try { await ProcessBlobBatchAsync(receiver, slice, ct); }
                                finally { gate.Release(); }
                            }));
                        }
                    }
                    foreach (var item in individual)
                    {
                        await gate.WaitAsync(ct);
                        batchTasks.Add(Task.Run(async () =>
                        {
                            try { await ProcessBlobMessageAsync(receiver, item, ct); }
                            finally { gate.Release(); }
                        }));
                    }
                    await Task.WhenAll(batchTasks);
                }

                // Chunk before the legacy truncation/token-packing path. One source
                // message is settled only after all its chunks and cleanup succeed.
                foreach (var item in textItems.Where(i => i.msg.ContentStrategy == "chunk"))
                {
                    await _textGate.WaitAsync(ct);
                    try { await ProcessTextChunksAsync(receiver, item, ct); }
                    finally { _textGate.Release(); }
                }
                textItems = textItems.Where(i => i.msg.ContentStrategy != "chunk").ToList();

                // Group text messages by model for batch embedding
                var byModel = textItems.GroupBy(i => i.msg.DocgrokPipeline).ToList();
                var textBatchTasks = new List<Task>();

                foreach (var group in byModel)
                {
                    // Pre-validate each message against single-text token limit,
                    // then pack into token-bounded sub-batches before sending.
                    var validated = await ValidateAndTruncateAsync(receiver, group.ToList(), ct);
                    foreach (var subBatch in PackByTokenBudget(validated))
                    {
                        await _textGate.WaitAsync(ct);
                        textBatchTasks.Add(ProcessTextBatchWithGateAsync(
                            receiver, subBatch, ct));
                    }
                }
                await Task.WhenAll(textBatchTasks);
    }

    private async Task ProcessTextBatchWithGateAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> batch,
        CancellationToken ct)
    {
        try { await ProcessBatchAsync(receiver, batch, ct); }
        finally { _textGate.Release(); }
    }

    private async Task ProcessTextChunksAsync(ServiceBusReceiver receiver,
        (EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg) item, CancellationToken ct)
    {
        var msg = item.msg;
        try
        {
            if (msg.DestinationType != "cosmosdb-vector" || !_writers.TryGetValue(msg.DestinationType, out var writer))
                throw new NotSupportedException("Text chunking requires a Cosmos vector destination");
            var config = msg.ChunkConfig ?? new();
            var texts = CosmosTextChunker.Split(msg.Content, config);
            var contextLimit = EffectiveTokenLimit(_options.MaxSingleTextTokens, msg.ModelContextWindowTokens);
            var batchTokenLimit = EffectiveTokenLimit(_options.MaxBatchTokens, msg.ModelMaxBatchTokens);
            var batchSize = EffectiveBatchSize(msg);
            // Reject unsafe IDs and oversized chunks before making any model calls.
            for (var i = 0; i < texts.Count; i++)
            {
                _ = CosmosTextChunker.DocId(msg, config, i);
                if (TokenEstimator.Estimate(texts[i]) > Math.Min(contextLimit, batchTokenLimit))
                    throw new ArgumentException("Configured chunk exceeds worker token limits; reduce chunk_size");
            }
            var results = new List<EmbeddingResult>();
            for (int start = 0; start < texts.Count;)
            {
                var inputs = new List<string>();
                var budget = 0;
                while (start + inputs.Count < texts.Count && inputs.Count < batchSize)
                {
                    var text = texts[start + inputs.Count];
                    var tokens = TokenEstimator.Estimate(text);
                    if (inputs.Count > 0 && budget + tokens > batchTokenLimit) break;
                    inputs.Add(text);
                    budget += tokens;
                }
                var vectors = await _docGrok.EmbedBatchAsync(
                    msg.DocgrokPipeline, inputs, ct, msg.PipelineId,
                    msg.SourceId, msg.DestinationId, msg.SourceRef);
                if (vectors.Count != inputs.Count)
                    throw new InvalidOperationException("Chunk embedding count mismatch");
                for (var i = 0; i < inputs.Count; i++)
                    results.Add(CosmosTextChunker.Result(msg, config, inputs[i], vectors[i], start + i, texts.Count));
                start += inputs.Count;
            }
            if (results.Select(r => r.Embedding.Length).Distinct().Count() > 1)
                throw new InvalidOperationException("Chunk embedding dimensions are inconsistent");
            await writer.ReplaceTextChunksAsync(msg.DestinationConfig,
                new(msg.SourceId, msg.SourceRef, msg.PartitionKeyValue, msg.PipelineId,
                    ChunkCleanupOrder: config.CleanupOrder), results, ct);
            await _metrics.ReportInlineMetricsAsync(
                msg.PipelineId, 1, 0, 0, $"chunk:{msg.MessageId}", ct,
                sourceId: msg.SourceId, destinationId: msg.DestinationId,
                modelId: msg.DocgrokPipeline,
                tokensUsed: TokenEstimator.Estimate(msg.Content),
                inputBytes: Encoding.UTF8.GetByteCount(msg.Content),
                queueWaitMs: Math.Max(
                    0, (DateTimeOffset.UtcNow - item.sbMsg.EnqueuedTime).TotalMilliseconds),
                lastDocument: msg.SourceRef);
            await receiver.CompleteMessageAsync(item.sbMsg, ct);
            _logger.LogInformation("Cosmos text chunked source={SourceId}/{SourceRef} pipeline={Pipeline}: {Count} chunks",
                msg.SourceId, msg.SourceRef, msg.PipelineId, results.Count);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Cosmos text chunking failed for {SourceRef}", msg.SourceRef);
            await SettleFailureAsync(receiver, item.sbMsg, ex, ct);
        }
    }

    private async Task DeadLetterWithMetricsAsync(ServiceBusReceiver receiver,
        ServiceBusReceivedMessage message, string reason, string description, CancellationToken ct)
    {
        await receiver.DeadLetterMessageAsync(message, reason, description, ct);
        _logger.LogError("Dead-lettered message {MessageId}: {Reason}: {Description}",
            message.MessageId, reason, description);
        EmbeddingMessage? failedMessage = null;
        try
        {
            failedMessage = JsonSerializer.Deserialize<EmbeddingMessage>(message.Body.ToString());
        }
        catch (JsonException) { }
        // Only confirmed explicit DLQ settlement is counted as a terminal failure.
        // Broker-automatic dead-lettering still requires Service Bus monitoring.
        if (failedMessage is not null && !string.IsNullOrWhiteSpace(failedMessage.PipelineId))
            await _metrics.ReportInlineMetricsAsync(
                failedMessage.PipelineId, 0, 1, 0,
                $"worker-dlq:{message.MessageId}", ct,
                sourceId: failedMessage.SourceId,
                destinationId: failedMessage.DestinationId,
                modelId: failedMessage.DocgrokPipeline,
                inputBytes: Encoding.UTF8.GetByteCount(failedMessage.Content),
                queueWaitMs: Math.Max(
                    0, (DateTimeOffset.UtcNow - message.EnqueuedTime).TotalMilliseconds),
                errorCategory: reason,
                lastDocument: failedMessage.SourceRef);
    }

    private async Task SettleFailureAsync(ServiceBusReceiver receiver,
        ServiceBusReceivedMessage message, Exception error, CancellationToken ct)
    {
        try
        {
            if (error is NotSupportedException or ArgumentException
                || error is EmbeddingClientException http && DocGrokClient.IsInputError(http.StatusCode))
                await DeadLetterWithMetricsAsync(receiver, message, "PermanentProcessingFailure",
                    error.Message[..Math.Min(4000, error.Message.Length)], ct);
            else
                await receiver.AbandonMessageAsync(message, cancellationToken: ct);
        }
        catch (Exception settlementError)
        {
            _logger.LogWarning(settlementError, "Could not settle failed message {MessageId}; lock expiry will redeliver",
                message.MessageId);
        }
    }

    private async Task ProcessDeleteBatchAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> items,
        CancellationToken ct)
    {
        var byDest = items.GroupBy(i => i.msg.DestinationId).ToList();
        foreach (var grp in byDest)
        {
            var sample = grp.First().msg;
            if (!_writers.TryGetValue(sample.DestinationType, out var writer))
            {
                _logger.LogWarning(
                    "No writer for destination type {Type} — dead-lettering {Count} delete msg(s)",
                    sample.DestinationType, grp.Count());
                foreach (var (_, sbMsg) in grp)
                    try { await DeadLetterWithMetricsAsync(receiver, sbMsg, "UnknownDestinationType",
                        $"No writer for {sample.DestinationType}", ct); } catch { }
                continue;
            }

            var requests = grp.Select(i => new DeleteRequest(
                SourceId: i.msg.SourceId,
                SourceRef: i.msg.SourceRef,
                PartitionKeyValue: string.IsNullOrEmpty(i.msg.PartitionKeyValue) ? i.msg.SourceRef : i.msg.PartitionKeyValue,
                PipelineId: i.msg.PipelineId,
                SourceVersion: i.msg.SourceVersion,
                PipelineRevision: i.msg.PipelineRevision,
                DocumentId: RenderDocumentId(i.msg))).ToList();

            try
            {
                await writer.DeleteByRefAsync(sample.DestinationConfig, requests, ct);
                foreach (var (_, sbMsg) in grp)
                    await receiver.CompleteMessageAsync(sbMsg, ct);
                _logger.LogInformation(
                    "Delete: processed {Count} ref(s) on dest {Dest}",
                    requests.Count, sample.DestinationId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Delete batch failed for dest {Dest}, abandoning {Count} msg(s)",
                    sample.DestinationId, grp.Count());
                foreach (var (_, sbMsg) in grp)
                    await SettleFailureAsync(receiver, sbMsg, ex, ct);
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
                ct,
                msg.PipelineId,
                msg.SourceId,
                msg.DestinationId);

            if (chunks.Count == 0)
            {
                _logger.LogInformation("DocGrok explicitly skipped blob {BlobName}", msg.BlobName);
                await receiver.CompleteMessageAsync(item.sbMsg, ct);
                return;
            }

            // Build results — one per chunk, with chunk index in doc ID
            var results = new List<EmbeddingResult>();
            for (int i = 0; i < chunks.Count; i++)
            {
                var (chunkText, embedding) = chunks[i];
                var baseDocId = RenderDocumentId(msg);
                var docId = chunks.Count == 1
                    ? baseDocId
                    : ValidateDocumentId($"{baseDocId}-chunk-{i}");

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
                    SourceId: msg.SourceId,
                    StoreContent: msg.StoreContent,
                    MetadataFields: msg.MetadataFields,
                    ContentField: msg.ContentField,
                    SourceVersion: msg.SourceVersion,
                    PipelineRevision: msg.PipelineRevision));
            }

            // Write to destination
            if (_writers.TryGetValue(msg.DestinationType, out var writer))
            {
                await writer.WriteBatchAsync(msg.DestinationConfig, results, ct);
            }
            else
            {
                throw new NotSupportedException($"No writer for destination type {msg.DestinationType}");
            }

            sw.Stop();
            await _metrics.ReportInlineMetricsAsync(
                msg.PipelineId, chunks.Count, 0, sw.ElapsedMilliseconds,
                $"blob:{msg.MessageId}", ct,
                sourceId: msg.SourceId, destinationId: msg.DestinationId,
                modelId: msg.DocgrokPipeline,
                queueWaitMs: Math.Max(
                    0, (DateTimeOffset.UtcNow - item.sbMsg.EnqueuedTime).TotalMilliseconds),
                lastDocument: msg.SourceRef);
            await receiver.CompleteMessageAsync(item.sbMsg, ct);
            _logger.LogInformation(
                "Blob processed: {BlobName} → {ChunkCount} chunks for pipeline={Pipeline} in {Elapsed}ms",
                msg.BlobName, chunks.Count, msg.PipelineName, sw.ElapsedMilliseconds);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to process blob {BlobName}", msg.BlobName);
            await SettleFailureAsync(receiver, item.sbMsg, ex, ct);
        }
    }

    private async Task ProcessSharePointMessageAsync(
        ServiceBusReceiver receiver,
        (EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg) item,
        CancellationToken ct)
    {
        var msg = item.msg;
        try
        {
            var identity = SharePointIdentity.Create(msg);
            if (msg.SharePointRevision <= 0)
                throw new InvalidOperationException("Legacy SharePoint message has no synchronization revision; drain old messages before migration");
            if (!_writers.TryGetValue(msg.DestinationType, out var writer))
                throw new NotSupportedException($"No writer for destination type {msg.DestinationType}");
            var deleted = string.Equals(msg.MessageType, "delete", StringComparison.OrdinalIgnoreCase);
            var chunks = new List<(string ChunkText, float[] Embedding)>();
            if (!deleted)
            {
                if (string.IsNullOrWhiteSpace(msg.SharePointETag))
                    throw new InvalidOperationException("SharePoint upsert has no Graph eTag");
                var bytes = await _sharePoint.DownloadAsync(
                    msg.SharePointSiteId!, msg.SharePointDriveId!, msg.SharePointItemId!,
                    msg.SharePointMaxFileSizeBytes, ct, msg.SharePointETag,
                    msg.SharePointGraphTenantId, msg.SharePointGraphClientId, msg.PipelineId);
                // A zero-byte file is an explicit empty document, not a malformed processor response.
                if (bytes.Length > 0)
                    chunks = await _docGrok.EmbedDataAsync(
                        msg.DocgrokPipeline, bytes, msg.SharePointFileName ?? msg.SourceRef, ct,
                        msg.PipelineId, msg.SourceId, msg.DestinationId);
            }

            var results = chunks.Select((chunk, index) => new EmbeddingResult(
                DocId: $"{identity}-{msg.SharePointRevision}-chunk-{index}",
                SourceRef: msg.SourceRef,
                Embedding: chunk.Embedding,
                ContentHash: msg.ContentHash,
                PartitionKeyValue: identity,
                PipelineId: msg.PipelineId,
                PipelineName: msg.PipelineName,
                PipelineGeneration: msg.PipelineGeneration,
                Content: chunk.ChunkText,
                SourceContentFields: new Dictionary<string, string>(),
                SourceId: msg.SourceId,
                StoreContent: msg.StoreContent,
                MetadataFields: msg.MetadataFields,
                ContentField: msg.ContentField,
                SourceVersion: msg.SourceVersion,
                PipelineRevision: msg.PipelineRevision)).ToList();

            var applied = await writer.ReplaceSharePointAsync(msg.DestinationConfig,
                new SharePointReplacement(identity, msg.SharePointRevision, msg.SourceId,
                    msg.PipelineId, msg.SourceRef, results), ct);

            await _metrics.ReportInlineMetricsAsync(
                msg.PipelineId, applied ? results.Count : 0, 0, 0,
                $"sharepoint:{identity}:{msg.SharePointRevision}", ct,
                sourceId: msg.SourceId, destinationId: msg.DestinationId,
                modelId: msg.DocgrokPipeline,
                queueWaitMs: Math.Max(
                    0, (DateTimeOffset.UtcNow - item.sbMsg.EnqueuedTime).TotalMilliseconds),
                lastDocument: msg.SourceRef);
            await receiver.CompleteMessageAsync(item.sbMsg, ct);
            _logger.LogInformation("SharePoint {Identity} revision={Revision}: {Outcome}, chunks={Count}",
                identity, msg.SharePointRevision, !applied ? "superseded" : deleted ? "deleted" : results.Count == 0 ? "skipped (empty/filtered)" : "indexed",
                results.Count);
        }
        catch (OperationCanceledException) { throw; }
        catch (SharePointVersionChangedException ex)
        {
            // The watcher will publish the newer Graph version (or deletion).
            // Never label this an indexed success or erase the last good vectors.
            _logger.LogInformation("SharePoint item {ItemId} superseded: {Reason}", msg.SharePointItemId, ex.Message);
            await receiver.CompleteMessageAsync(item.sbMsg, ct);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to process SharePoint item {SourceRef}", msg.SourceRef);
            await SettleFailureAsync(receiver, item.sbMsg, ex, ct);
        }
    }

    /// <summary>
    /// Bulk-embed a batch of blob_ref messages (e.g. CLIP images) in a single
    /// HTTP call to the docgrok router. Vastly higher throughput than the
    /// per-message path because images are downloaded in parallel server-side
    /// and run through a single batched forward pass.
    /// </summary>
    private async Task ProcessBlobBatchAsync(
        ServiceBusReceiver receiver,
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> batch,
        CancellationToken ct)
    {
        if (batch.Count == 0) return;
        var sw = Stopwatch.StartNew();
        var head = batch[0].msg;
        var blobNames = batch.Select(b => b.msg.BlobName ?? "").ToList();

        List<float[]> embeddings;
        try
        {
            embeddings = await _docGrok.EmbedBlobBatchAsync(
                head.DocgrokPipeline,
                head.BlobAccountUrl ?? "",
                head.BlobContainer ?? "",
                blobNames,
                ct,
                head.PipelineId,
                head.SourceId,
                head.DestinationId);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Bulk blob embed failed for {Count} blobs on pipeline {Pipeline}; abandoning batch",
                batch.Count, head.PipelineName);
            foreach (var it in batch)
            {
                await SettleFailureAsync(receiver, it.sbMsg, ex, ct);
            }
            return;
        }

        // Group results by destination so each unique destination gets one
        // write call. In the common case all messages in a batch share a
        // destination, so this is just a single WriteBatchAsync.
        var resultsByDest = new Dictionary<string, (Dictionary<string, object> cfg, List<EmbeddingResult> docs)>();
        for (int i = 0; i < batch.Count; i++)
        {
            var msg = batch[i].msg;
            var emb = embeddings[i];
            var res = new EmbeddingResult(
                DocId: RenderDocumentId(msg),
                SourceRef: msg.SourceRef,
                Embedding: emb,
                ContentHash: msg.ContentHash,
                PartitionKeyValue: msg.PartitionKeyValue,
                PipelineId: msg.PipelineId,
                PipelineName: msg.PipelineName,
                PipelineGeneration: msg.PipelineGeneration,
                Content: "",
                SourceContentFields: new Dictionary<string, string>(),
                SourceId: msg.SourceId,
                StoreContent: msg.StoreContent,
                MetadataFields: msg.MetadataFields,
                ContentField: msg.ContentField,
                ModelName: msg.DocgrokPipeline,
                SourceVersion: msg.SourceVersion,
                PipelineRevision: msg.PipelineRevision);
            var key = $"{msg.DestinationType}|{msg.DestinationId}";
            if (!resultsByDest.TryGetValue(key, out var bucket))
            {
                bucket = (msg.DestinationConfig, new List<EmbeddingResult>());
                resultsByDest[key] = bucket;
            }
            bucket.docs.Add(res);
        }

        await Parallel.ForEachAsync(
            resultsByDest,
            new ParallelOptions { MaxDegreeOfParallelism = 8, CancellationToken = ct },
            async (kvp, token) =>
            {
                var destType = kvp.Key.Split('|', 2)[0];
                if (!_writers.TryGetValue(destType, out var writer))
                    throw new NotSupportedException($"No writer for destination type {destType}");
                try { await writer.WriteBatchAsync(kvp.Value.cfg, kvp.Value.docs, token); }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Writer {Type} failed on bulk blob batch ({Count} docs)",
                        destType, kvp.Value.docs.Count);
                    throw;
                }
            });

        sw.Stop();
        _logger.LogInformation(
            "Bulk blob batch: {Count} blobs for pipeline={Pipeline} in {Elapsed}ms ({Rate:F1} img/s)",
            batch.Count, head.PipelineName, sw.ElapsedMilliseconds,
            batch.Count * 1000.0 / Math.Max(sw.ElapsedMilliseconds, 1));

        await _metrics.ReportInlineMetricsAsync(
            head.PipelineId, batch.Count, 0, sw.ElapsedMilliseconds,
            BuildMetricsBatchKey(head.PipelineId, batch.Select(item => item.msg)), ct,
            sourceId: head.SourceId, destinationId: head.DestinationId,
            modelId: head.DocgrokPipeline,
            queueWaitMs: batch.Average(item =>
                Math.Max(0, (DateTimeOffset.UtcNow - item.sbMsg.EnqueuedTime).TotalMilliseconds)),
            lastDocument: batch[^1].msg.SourceRef);

        // Settle broker deliveries concurrently only after metrics are durable.
        try
        {
            await Parallel.ForEachAsync(
                batch,
                new ParallelOptions { MaxDegreeOfParallelism = 16, CancellationToken = ct },
                async (item, token) => await receiver.CompleteMessageAsync(item.sbMsg, token));
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Batch completion failed for {Count} blob messages", batch.Count);
        }
    }

    private static string RenderDocumentId(EmbeddingMessage message)
        => IdentityTemplateRenderer.RenderDocumentId(message);

    private static string ValidateDocumentId(string rendered)
        => IdentityTemplateRenderer.ValidateDocumentId(rendered);

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
                    DocId: RenderDocumentId(msg),
                    SourceRef: msg.SourceRef,
                    Embedding: embedding,
                    ContentHash: msg.ContentHash,
                    PartitionKeyValue: msg.PartitionKeyValue,
                    PipelineId: msg.PipelineId,
                    PipelineName: msg.PipelineName,
                    PipelineGeneration: msg.PipelineGeneration,
                    Content: msg.Content,
                    SourceContentFields: msg.SourceContentFields,
                    SourceId: msg.SourceId,
                    StoreContent: msg.StoreContent,
                    MetadataFields: msg.MetadataFields,
                    ContentField: msg.ContentField,
                    ModelName: msg.DocgrokPipeline,
                    SourceVersion: msg.SourceVersion,
                    PipelineRevision: msg.PipelineRevision);

                var destKey = msg.DestinationId;
                if (!resultsByDest.ContainsKey(destKey))
                    resultsByDest[destKey] = (msg.DestinationType, msg.DestinationConfig, new List<EmbeddingResult>());
                resultsByDest[destKey].results.Add(result);
            }

            // Phase 3: Write to destinations
            await Parallel.ForEachAsync(
                resultsByDest,
                new ParallelOptions { MaxDegreeOfParallelism = 8, CancellationToken = ct },
                async (kvp, token) =>
                {
                    var (_, (destType, config, results)) = kvp;
                    if (!_writers.TryGetValue(destType, out var writer))
                        throw new NotSupportedException($"No writer for destination type {destType}");
                    await writer.WriteBatchAsync(config, results, token);
                });

            sw.Stop();
            _logger.LogInformation(
                "Worker batch complete: {Count} docs for pipeline={Pipeline} in {Elapsed}ms",
                batch.Count, batch[0].msg.PipelineName, sw.ElapsedMilliseconds);

            // Phase 4: Queue best-effort metrics without waiting for HTTP delivery.
            var batchKey = BuildMetricsBatchKey(pipelineId, batch.Select(item => item.msg));
            var tokensUsed = texts.Sum(TokenEstimator.Estimate);
            var inputBytes = texts.Sum(text => Encoding.UTF8.GetByteCount(text));
            var queueWaitMs = batch.Average(item =>
                Math.Max(0, (DateTimeOffset.UtcNow - item.sbMsg.EnqueuedTime).TotalMilliseconds));
            await _metrics.ReportInlineMetricsAsync(
                pipelineId, batch.Count, 0, sw.ElapsedMilliseconds, batchKey, ct,
                sourceId: batch[0].msg.SourceId,
                destinationId: batch[0].msg.DestinationId,
                modelId: modelKey,
                tokensUsed: tokensUsed,
                inputBytes: inputBytes,
                queueWaitMs: queueWaitMs,
                lastDocument: batch[^1].msg.SourceRef,
                resourceWeight: batch[0].msg.ResourceWeight,
                maxConcurrencyPerWorker: batch[0].msg.MaxConcurrencyPerWorker,
                resourcePriority: batch[0].msg.ResourcePriority,
                workloadClass: batch[0].msg.WorkloadClass);

            // Phase 5: Settle broker deliveries concurrently; redelivery is safe
            // because destination writes and metric batch keys are idempotent.
            try
            {
                await Parallel.ForEachAsync(
                    batch,
                    new ParallelOptions { MaxDegreeOfParallelism = 16, CancellationToken = ct },
                    async (item, token) => await receiver.CompleteMessageAsync(item.sbMsg, token));
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Could not complete message batch of {Count}", batch.Count);
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to process batch of {Count} for model={Model}", batch.Count, modelKey);
            // Abandon all messages so Service Bus retries them
            foreach (var (_, sbMsg) in batch)
            {
                await SettleFailureAsync(receiver, sbMsg, ex, ct);
            }
        }
    }

    private static string BuildMetricsBatchKey(string pipelineId, IEnumerable<EmbeddingMessage> messages)
    {
        var identities = messages
            .Select(message => string.IsNullOrWhiteSpace(message.MessageId)
                ? $"{message.SourceId}:{message.SourceRef}:{message.ContentHash}:{message.PipelineGeneration}"
                : message.MessageId)
            .OrderBy(identity => identity, StringComparer.Ordinal);
        var digest = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("\n", identities))))
            .ToLowerInvariant();
        return $"worker:{pipelineId}:{digest}";
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
            var contextLimit = Math.Min(
                EffectiveTokenLimit(_options.MaxSingleTextTokens, item.msg.ModelContextWindowTokens),
                EffectiveTokenLimit(_options.MaxBatchTokens, item.msg.ModelMaxBatchTokens));
            if (tokens <= contextLimit)
            {
                kept.Add(item);
                continue;
            }

            if (_options.TruncateOversized)
            {
                try
                {
                    item.msg.Content = TruncateToTokenBudget(content, contextLimit);
                }
                catch (ArgumentException ex)
                {
                    _logger.LogWarning("Input {SourceRef} cannot fit its token budget: {Reason}", item.msg.SourceRef, ex.Message);
                    await DeadLetterWithMetricsAsync(receiver, item.sbMsg, "TokenLimitExceeded", ex.Message, ct);
                    continue;
                }
                var newLen = item.msg.Content.Length;
                _logger.LogWarning(
                    "Truncated oversized input {SourceRef}: {OrigChars} chars (~{OrigTokens} tok) → {NewChars} chars",
                    item.msg.SourceRef, content.Length, tokens, newLen);
                kept.Add(item);
            }
            else
            {
                _logger.LogWarning(
                    "Dead-lettering oversized input {SourceRef}: ~{Tokens} tokens > {Max}",
                    item.msg.SourceRef, tokens, contextLimit);
                try
                {
                    await DeadLetterWithMetricsAsync(
                        receiver, item.sbMsg,
                        "TokenLimitExceeded",
                        $"Estimated {tokens} tokens exceeds effective model context limit={contextLimit}",
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
    /// (pipeline and model limits) and the effective request token budget.
    /// </summary>
    private IEnumerable<List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)>> PackByTokenBudget(
        List<(EmbeddingMessage msg, ServiceBusReceivedMessage sbMsg)> items)
    {
        var current = new List<(EmbeddingMessage, ServiceBusReceivedMessage)>();
        int currentTokens = 0;
        int batchSize = int.MaxValue;
        int batchTokenLimit = EffectiveTokenLimit(_options.MaxBatchTokens, null);

        foreach (var item in items)
        {
            int t = TokenEstimator.Estimate(item.msg.Content);
            var nextBatchSize = Math.Min(batchSize, EffectiveBatchSize(item.msg));
            var nextTokenLimit = Math.Min(batchTokenLimit,
                EffectiveTokenLimit(_options.MaxBatchTokens, item.msg.ModelMaxBatchTokens));
            bool wouldOverflow =
                current.Count >= nextBatchSize ||
                (current.Count > 0 && currentTokens + t > nextTokenLimit);

            if (wouldOverflow)
            {
                yield return current;
                current = new List<(EmbeddingMessage, ServiceBusReceivedMessage)>();
                currentTokens = 0;
                batchSize = int.MaxValue;
                batchTokenLimit = EffectiveTokenLimit(_options.MaxBatchTokens, null);
            }

            batchSize = Math.Min(batchSize, EffectiveBatchSize(item.msg));
            batchTokenLimit = Math.Min(batchTokenLimit,
                EffectiveTokenLimit(_options.MaxBatchTokens, item.msg.ModelMaxBatchTokens));
            current.Add(item);
            currentTokens += t;
        }

        if (current.Count > 0) yield return current;
    }

    private int EffectiveBatchSize(EmbeddingMessage message)
    {
        var pipelineLimit = message.EmbedBatchSize is >= 1 and <= 4096
            ? message.EmbedBatchSize.Value
            : _options.EmbedBatchSize;
        var modelLimit = message.ModelMaxBatchSize is >= 1 and <= 4096
            ? message.ModelMaxBatchSize.Value
            : int.MaxValue;
        return Math.Min(pipelineLimit, modelLimit);
    }

    private static int EffectiveTokenLimit(int workerLimit, int? modelLimit)
    {
        var hardLimit = modelLimit is > 0 ? Math.Min(workerLimit, modelLimit.Value) : workerLimit;
        return Math.Max(1, (int)Math.Floor(hardLimit * 0.95));
    }

    private static string TruncateToTokenBudget(string content, int tokenLimit)
    {
        while (TokenEstimator.Estimate(content) > tokenLimit)
        {
            var nextLength = Math.Min(content.Length - 1,
                (int)Math.Floor(content.Length * (double)tokenLimit / TokenEstimator.Estimate(content)));
            if (nextLength <= 0)
                throw new ArgumentException("Input cannot fit the effective model token budget");
            if (char.IsHighSurrogate(content[nextLength - 1])) nextLength--;
            if (nextLength <= 0)
                throw new ArgumentException("Input cannot fit the effective model token budget");
            content = content.Substring(0, nextLength);
        }
        return content;
    }

    private static void ValidateRequestLimits(EmbeddingMessage message)
    {
        if (message.EmbedBatchSize is < 1 or > 4096
            || message.ModelMaxBatchSize is < 1 or > 4096
            || message.ModelContextWindowTokens is < 1 or > 2_000_000
            || message.ModelMaxBatchTokens is < 1 or > 2_000_000)
            throw new ArgumentException("Queued pipeline/model request limits are outside supported ranges");
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
        var head = batch[0].msg;
        if (head.BenchmarkMockEmbedding)
        {
            if (!_options.EnableBenchmarkMockEmbedding)
                throw new InvalidOperationException(
                    "Benchmark mock embedding is disabled on this worker");
            if (batch.Any(item =>
                !item.msg.BenchmarkMockEmbedding
                || item.msg.BenchmarkMockEmbeddingDimensions
                    != head.BenchmarkMockEmbeddingDimensions))
                throw new InvalidOperationException(
                    "Benchmark mock embedding settings must match within a batch");
            var vector = Enumerable.Repeat(
                0.01f, head.BenchmarkMockEmbeddingDimensions).ToArray();
            return batch.Select(_ => vector).ToList();
        }

        try
        {
            return await _docGrok.EmbedBatchAsync(
                modelKey, texts, ct, head.PipelineId,
                head.SourceId, head.DestinationId, head.SourceRef);
        }
        catch (EmbeddingClientException ex) when (DocGrokClient.IsInputError(ex.StatusCode))
        {
            if (batch.Count == 1)
            {
                var only = batch[0];

                if (_options.TruncateOversized && IsSizeLimitError(ex) && only.msg.Content?.Length > 1)
                {
                    for (var reduction = 1; reduction <= 6; reduction++)
                    {
                        var orig = only.msg.Content;
                        var reportedLimit = ReportedContextLimit(ex.ResponseBody);
                        var targetTokens = reportedLimit is > 0
                            ? Math.Max(1, (int)Math.Floor(reportedLimit.Value * 0.95))
                            : Math.Max(1, TokenEstimator.Estimate(orig) / 2);
                        var ratio = Math.Min(0.8, (double)targetTokens / Math.Max(1, TokenEstimator.Estimate(orig)));
                        var newLength = Math.Max(1, (int)Math.Floor(orig.Length * ratio));
                        if (newLength >= orig.Length) newLength = orig.Length - 1;
                        only.msg.Content = orig.Substring(0, newLength);
                        _logger.LogWarning(
                            "Embed size limit for {SourceRef}; reducing {OrigLen}→{NewLen} chars (attempt {Attempt}/6).",
                            only.msg.SourceRef, orig.Length, newLength, reduction);
                        try
                        {
                            return await _docGrok.EmbedBatchAsync(
                                modelKey, new List<string> { only.msg.Content }, ct,
                                only.msg.PipelineId, only.msg.SourceId,
                                only.msg.DestinationId, only.msg.SourceRef);
                        }
                        catch (EmbeddingClientException ex2) when (IsSizeLimitError(ex2))
                        {
                            ex = ex2;
                            if (only.msg.Content.Length <= 1) break;
                        }
                        catch (EmbeddingClientException ex2) when (DocGrokClient.IsInputError(ex2.StatusCode))
                        {
                            ex = ex2;
                            break;
                        }
                    }
                }

                _logger.LogError(
                    "Embed rejected single message {SourceRef} with {Status}: {Msg}. Dead-lettering.",
                    only.msg.SourceRef, ex.StatusCode, ex.Message);
                try
                {
                    await DeadLetterWithMetricsAsync(
                        receiver, only.sbMsg,
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

    private static bool IsSizeLimitError(EmbeddingClientException error)
    {
        if (error.StatusCode == 413) return true;
        var body = error.ResponseBody ?? "";
        return error.StatusCode is 400 or 422
            && (body.Contains("context_length_exceeded", StringComparison.OrdinalIgnoreCase)
                || body.Contains("context window", StringComparison.OrdinalIgnoreCase)
                || body.Contains("maximum context length", StringComparison.OrdinalIgnoreCase)
                || body.Contains("context length exceeded", StringComparison.OrdinalIgnoreCase)
                || body.Contains("too many tokens", StringComparison.OrdinalIgnoreCase)
                || body.Contains("token limit", StringComparison.OrdinalIgnoreCase)
                || body.Contains("token budget", StringComparison.OrdinalIgnoreCase)
                || body.Contains("batch maximum", StringComparison.OrdinalIgnoreCase)
                || body.Contains("too large", StringComparison.OrdinalIgnoreCase));
    }

    private static int? ReportedContextLimit(string? body)
    {
        if (string.IsNullOrWhiteSpace(body)) return null;
        var match = Regex.Match(
            body,
            @"(?:context window is|maximum context length is|context_window_tokens["":\s]+)\s*(\d+)",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        return match.Success && int.TryParse(match.Groups[1].Value, out var limit) ? limit : null;
    }
}
