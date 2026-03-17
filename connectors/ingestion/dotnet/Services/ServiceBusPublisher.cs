using System.Text.Json;
using Azure.Messaging.ServiceBus;
using Azure.Messaging.ServiceBus.Administration;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using Microsoft.Extensions.Options;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Publishes embedding messages to Azure Service Bus for queue-mode pipelines.
/// Replaces the legacy API job creation path.
/// </summary>
public class ServiceBusPublisher : IAsyncDisposable
{
    private readonly ServiceBusSender? _sender;
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
        _topicName = options.Value.ServiceBusTopicName;
        _subscriptionName = "worker";
        _backpressureThreshold = options.Value.BackpressureMaxActiveMessages;

        if (_enabled)
        {
            var credential = new Azure.Identity.DefaultAzureCredential();
            var client = new ServiceBusClient(
                options.Value.ServiceBusNamespace, credential);
            _sender = client.CreateSender(options.Value.ServiceBusTopicName);
            _adminClient = new ServiceBusAdministrationClient(
                options.Value.ServiceBusNamespace, credential);
            _logger.LogInformation("Service Bus publisher enabled: {Namespace}/{Topic}",
                options.Value.ServiceBusNamespace, options.Value.ServiceBusTopicName);
        }
        else
        {
            _logger.LogInformation("Service Bus publisher disabled, using legacy API job creation");
        }
    }

    public bool IsEnabled => _enabled;

    /// <summary>
    /// Publish a batch of embedding messages to Service Bus.
    /// Messages are self-contained with content and destination config.
    /// </summary>
    public async Task PublishBatchAsync(
        List<EmbeddingMessage> messages,
        CancellationToken ct)
    {
        if (_sender is null || messages.Count == 0) return;

        // Use Service Bus batch to respect size limits
        using var batch = await _sender.CreateMessageBatchAsync(ct);
        var overflow = new List<EmbeddingMessage>();

        foreach (var msg in messages)
        {
            var json = JsonSerializer.Serialize(msg);
            var sbMsg = new ServiceBusMessage(json)
            {
                MessageId = msg.MessageId,
                ContentType = "application/json",
                Subject = msg.PipelineId,
            };

            if (!batch.TryAddMessage(sbMsg))
            {
                overflow.Add(msg);
            }
        }

        if (batch.Count > 0)
        {
            await _sender.SendMessagesAsync(batch, ct);
            _logger.LogInformation("Published {Count} messages to Service Bus", batch.Count);
        }

        // Send overflow in a second batch
        if (overflow.Count > 0)
        {
            using var batch2 = await _sender.CreateMessageBatchAsync(ct);
            foreach (var msg in overflow)
            {
                var json = JsonSerializer.Serialize(msg);
                var sbMsg = new ServiceBusMessage(json)
                {
                    MessageId = msg.MessageId,
                    ContentType = "application/json",
                    Subject = msg.PipelineId,
                };
                batch2.TryAddMessage(sbMsg);
            }
            if (batch2.Count > 0)
                await _sender.SendMessagesAsync(batch2, ct);
        }
    }

    /// <summary>
    /// Get the active message count on the worker subscription.
    /// Used for backpressure: if count exceeds threshold, pause enumeration.
    /// </summary>
    public async Task<long> GetActiveMessageCountAsync(CancellationToken ct)
    {
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
            return 0;
        }
    }

    /// <summary>
    /// Returns true if Service Bus active message count is below the backpressure threshold.
    /// </summary>
    public async Task<bool> HasCapacityAsync(CancellationToken ct)
    {
        var count = await GetActiveMessageCountAsync(ct);
        var hasCapacity = count < _backpressureThreshold;
        if (!hasCapacity)
            _logger.LogWarning("Backpressure active: {Count} messages in SB (threshold={Threshold})",
                count, _backpressureThreshold);
        return hasCapacity;
    }

    public async ValueTask DisposeAsync()
    {
        if (_sender is not null)
            await _sender.DisposeAsync();
    }
}
