using System.Text.Json;
using Azure.Messaging.ServiceBus;
using Azure.Messaging.ServiceBus.Administration;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Lanes;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Publishes embedding messages to Azure Service Bus for queue-mode pipelines.
/// Replaces the legacy API job creation path.
/// </summary>
public class ServiceBusPublisher : IAsyncDisposable
{
    private readonly ServiceBusSender? _sender;
    private readonly ServiceBusClient? _client;
    private readonly PipelineLaneRegistry? _lanes;
    private readonly bool _laneMode;
    private readonly ServiceBusAdministrationClient? _adminClient;
    private readonly string _topicName;
    private readonly string _subscriptionName;
    private readonly int _backpressureThreshold;
    private readonly ILogger<ServiceBusPublisher> _logger;
    private readonly bool _enabled;

    public ServiceBusPublisher(IOptions<ChangeFeedOptions> options, ILogger<ServiceBusPublisher> logger)
    {
        _logger = logger;
        _enabled = options.Value.ServiceBusEnabled && !string.IsNullOrEmpty(options.Value.ServiceBusNamespace);
        _laneMode = options.Value.PipelineLanesEnabled;
        if (_laneMode)
        {
            PipelineLaneProtocol.ValidateTopicName(options.Value.PipelineLanesTopicName);
            if (!_enabled) throw new InvalidOperationException("Lane publishing requires Service Bus enabled and configured");
        }
        _topicName = _laneMode ? options.Value.PipelineLanesTopicName : options.Value.ServiceBusTopicName;
        _subscriptionName = "worker";
        _backpressureThreshold = options.Value.BackpressureMaxActiveMessages;
        if (_laneMode && _backpressureThreshold <= 0)
            throw new ArgumentOutOfRangeException(nameof(options), "Lane backpressure threshold must be positive");

        if (_enabled)
        {
            var credential = new Azure.Identity.DefaultAzureCredential();
            _client = new ServiceBusClient(
                options.Value.ServiceBusNamespace, credential);
            _sender = _client.CreateSender(_topicName);
            _adminClient = new ServiceBusAdministrationClient(
                options.Value.ServiceBusNamespace, credential);
            if (_laneMode) _lanes = new(_adminClient, _topicName);
            _logger.LogInformation("Service Bus publisher enabled: {Namespace}/{Topic}",
                options.Value.ServiceBusNamespace, _topicName);
        }
        else
        {
            _logger.LogInformation("Service Bus publisher disabled, using legacy API job creation");
        }
    }

    public bool IsEnabled => _enabled;
    public bool PipelineLanesEnabled => _laneMode;

    /// <summary>
    /// Publish a batch of embedding messages to Service Bus.
    /// Messages are self-contained with content and destination config.
    /// </summary>
    public async Task PublishBatchAsync(
        List<EmbeddingMessage> messages,
        CancellationToken ct)
    {
        if (messages.Count == 0) return;
        if (_sender is null)
            throw new InvalidOperationException("Service Bus publisher is not configured");
        if (_laneMode)
        {
            var errors = new List<Exception>();
            foreach (var group in messages.GroupBy(message => message.PipelineId))
            {
                try
                {
                    var head = group.First();
                    var metadata = new LaneMetadata(1, head.PipelineId, head.ResourceWeight,
                        head.MaxConcurrencyPerWorker, head.ResourcePriority, head.WorkloadClass);
                    if (group.Any(m => new LaneMetadata(1, m.PipelineId, m.ResourceWeight,
                        m.MaxConcurrencyPerWorker, m.ResourcePriority, m.WorkloadClass) != metadata))
                        throw new InvalidOperationException("Inconsistent resource policy within a pipeline publish group");
                    var subscription = await _lanes!.EnsureAsync(metadata, ct);
                    var runtime = await _adminClient!.GetSubscriptionRuntimePropertiesAsync(_topicName, subscription, ct);
                    if (runtime.Value.ActiveMessageCount >= _backpressureThreshold)
                        throw new InvalidOperationException($"Pipeline-local backpressure: {head.PipelineId}");
                    await SendBatchAsync(group.ToList(), ct);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Pipeline {PipelineId} publish failed; retain source checkpoint (healthy groups still attempted)", group.Key);
                    errors.Add(ex);
                }
            }
            if (errors.Count > 0) throw new AggregateException("Lane publish failed; source checkpoint must be retained", errors);
            return;
        }
        if (!await HasCapacityAsync(ct))
            throw new InvalidOperationException("Service Bus backpressure active; source checkpoint must be retained");

        await SendBatchAsync(messages, ct);
    }

    private async Task SendBatchAsync(List<EmbeddingMessage> messages, CancellationToken ct)
    {
        foreach (var message in messages) QueueEnvelopeBuilder.Validate(message);
        var published = 0;

        foreach (var group in messages.GroupBy(message =>
                     (message.QueueSendBatchMaxBytes, message.QueuePublishBatchSize)))
        foreach (var chunk in group.Chunk(group.Key.QueuePublishBatchSize))
        {
            var pending = new Queue<ServiceBusMessage>(QueueEnvelopeBuilder.Build(chunk));
            while (pending.Count > 0)
            {
                using var batch = await _sender!.CreateMessageBatchAsync(
                    new CreateMessageBatchOptions { MaxSizeInBytes = group.Key.QueueSendBatchMaxBytes }, ct);
                while (pending.Count > 0)
                {
                    var sbMsg = pending.Peek();
                    if (!batch.TryAddMessage(sbMsg))
                    {
                        if (batch.Count == 0)
                            throw new InvalidOperationException($"Message {sbMsg.MessageId} exceeds the Service Bus batch size limit.");
                        break;
                    }
                    pending.Dequeue();
                }
                await _sender.SendMessagesAsync(batch, ct);
                published += batch.Count;
            }
        }

        _logger.LogInformation("Published {Documents} documents in {Messages} Service Bus messages", messages.Count, published);
    }

    /// <summary>
    /// Get the active message count on the worker subscription.
    /// Used for backpressure: if count exceeds threshold, pause enumeration.
    /// </summary>
    public async Task<long> GetActiveMessageCountAsync(CancellationToken ct)
    {
        if (_laneMode) throw new InvalidOperationException("Lane mode has no global worker subscription count");
        if (_adminClient is null) return 0;
        try
        {
            var props = await _adminClient.GetSubscriptionRuntimePropertiesAsync(
                _topicName, _subscriptionName, ct);
            return props.Value.ActiveMessageCount;
        }
        catch (Exception ex)
        {
            _logger.LogWarning("Could not get SB active message count: {Error}", ex.Message);
            throw;
        }
    }

    /// <summary>
    /// Returns true if Service Bus active message count is below the backpressure threshold.
    /// </summary>
    public async Task<bool> HasCapacityAsync(CancellationToken ct)
    {
        if (_laneMode) throw new InvalidOperationException("Lane mode requires scoped capacity or publish-time per-pipeline checks");
        var count = await GetActiveMessageCountAsync(ct);
        var hasCapacity = count < _backpressureThreshold;
        if (!hasCapacity)
            _logger.LogWarning("Backpressure active: {Count} messages in SB (threshold={Threshold})",
                count, _backpressureThreshold);
        return hasCapacity;
    }

    /// <summary>Source fanout can proceed if any lane has capacity; publish still checks each exact pipeline.</summary>
    public async Task<bool> HasSourceCapacityAsync(IEnumerable<string> pipelineIds, CancellationToken ct)
    {
        if (!_laneMode) return await HasCapacityAsync(ct);
        var lanes = await _lanes!.DiscoverAsync(ct);
        foreach (var id in pipelineIds.Distinct())
        {
            var name = PipelineLaneProtocol.SubscriptionName(id);
            if (!lanes.ContainsKey(name)) return true; // Provisioning happens before publish.
            var count = await _adminClient!.GetSubscriptionRuntimePropertiesAsync(_topicName, name, ct);
            if (count.Value.ActiveMessageCount < _backpressureThreshold) return true;
        }
        return false;
    }

    public async ValueTask DisposeAsync()
    {
        if (_sender is not null)
            await _sender.DisposeAsync();
        if (_client is not null)
            await _client.DisposeAsync();
    }
}
