using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Azure;
using Azure.Messaging.ServiceBus;
using Azure.Messaging.ServiceBus.Administration;

namespace OmniVec.Worker.Lanes;

public sealed record LaneMetadata(
    [property: JsonPropertyName("routing_version")] int RoutingVersion,
    [property: JsonPropertyName("pipeline_id")] string PipelineId,
    [property: JsonPropertyName("resource_weight")] int ResourceWeight,
    [property: JsonPropertyName("max_concurrency_per_worker")] int MaxConcurrencyPerWorker,
    [property: JsonPropertyName("resource_priority")] string ResourcePriority,
    [property: JsonPropertyName("workload_class")] string WorkloadClass);

public static class PipelineLaneProtocol
{
    public const string TopicMarker = "omnivec-pipeline-lanes-v1";
    public const int MaxLanes = 128;
    public static string SubscriptionName(string pipelineId)
    {
        if (string.IsNullOrWhiteSpace(pipelineId))
            throw new InvalidOperationException("A lane requires an exact nonempty pipeline ID");
        return "p-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(pipelineId)))[..32].ToLowerInvariant();
    }

    public static void ValidateTopicName(string topic)
    {
        if (string.IsNullOrWhiteSpace(topic) || string.Equals(topic, "embeddings", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Lane mode requires an explicitly configured separate marked topic, not embeddings");
    }

    public static void ValidateMetadata(string name, LaneMetadata metadata)
    {
        if (metadata.RoutingVersion != 1 || name != SubscriptionName(metadata.PipelineId)
            || metadata.ResourceWeight is < 1 or > 100 || metadata.MaxConcurrencyPerWorker is < 1 or > 32
            || metadata.ResourcePriority is not ("low" or "normal" or "high" or "critical")
            || metadata.WorkloadClass is not ("shared" or "burst" or "dedicated"))
            throw new InvalidOperationException($"Invalid lane ownership/metadata: {name}");
    }

    public static void ValidateRules(string pipelineId, IReadOnlyList<RuleProperties> rules)
    {
        if (rules.Count != 1 || rules[0].Filter is not CorrelationRuleFilter filter
            || filter.Subject != pipelineId || filter.CorrelationId != null || filter.MessageId != null
            || filter.To != null || filter.ReplyTo != null || filter.SessionId != null
            || filter.ReplyToSessionId != null || filter.ContentType != null || filter.ApplicationProperties.Count != 0
            || rules[0].Action is not null)
            throw new InvalidOperationException("Lane must have exactly one subject-only correlation rule and no rule action");
    }
}

/// <summary>Serialized, bounded, short-lived broker validation cache. Never repairs foreign entities.</summary>
public sealed class PipelineLaneRegistry(ServiceBusAdministrationClient admin, string topic)
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Dictionary<string, LaneMetadata> _cache = new();
    private DateTimeOffset _expires;
    public async Task<IReadOnlyDictionary<string, LaneMetadata>> DiscoverAsync(CancellationToken ct)
    {
        await _gate.WaitAsync(ct);
        try { return new Dictionary<string, LaneMetadata>(await RefreshAsync(ct)); }
        finally { _gate.Release(); }
    }

    private async Task<Dictionary<string, LaneMetadata>> RefreshAsync(CancellationToken ct)
    {
        PipelineLaneProtocol.ValidateTopicName(topic);
        if (DateTimeOffset.UtcNow < _expires) return _cache;
        var properties = await admin.GetTopicAsync(topic, ct);
        if (properties.Value.UserMetadata != PipelineLaneProtocol.TopicMarker)
            throw new InvalidOperationException("Lane topic is not marked omnivec-pipeline-lanes-v1");
        var discovered = new Dictionary<string, LaneMetadata>();
        await foreach (var subscription in admin.GetSubscriptionsAsync(topic, ct))
        {
            if (discovered.Count >= PipelineLaneProtocol.MaxLanes)
                throw new InvalidOperationException("Lane count exceeds stage-1 bounded receiver limit (128)");
            discovered.Add(subscription.SubscriptionName, await ValidateSubscriptionAsync(subscription, ct));
        }
        _cache = discovered;
        _expires = DateTimeOffset.UtcNow.AddSeconds(30);
        return _cache;
    }

    private async Task<LaneMetadata> ValidateSubscriptionAsync(SubscriptionProperties subscription, CancellationToken ct)
    {
        if (subscription.RequiresSession || !string.IsNullOrEmpty(subscription.ForwardTo)
            || !string.IsNullOrEmpty(subscription.ForwardDeadLetteredMessagesTo) || subscription.Status != EntityStatus.Active)
            throw new InvalidOperationException($"Lane must be active, non-session and non-forwarding: {subscription.SubscriptionName}");
        var metadata = JsonSerializer.Deserialize<LaneMetadata>(subscription.UserMetadata)
            ?? throw new InvalidOperationException($"Missing lane metadata: {subscription.SubscriptionName}");
        PipelineLaneProtocol.ValidateMetadata(subscription.SubscriptionName, metadata);
        var rules = new List<RuleProperties>();
        await foreach (var rule in admin.GetRulesAsync(topic, subscription.SubscriptionName, ct))
        {
            rules.Add(rule);
            if (rules.Count > 1) break;
        }
        PipelineLaneProtocol.ValidateRules(metadata.PipelineId, rules);
        return metadata;
    }

    public async Task<string> EnsureAsync(LaneMetadata desired, CancellationToken ct)
    {
        var name = PipelineLaneProtocol.SubscriptionName(desired.PipelineId);
        PipelineLaneProtocol.ValidateMetadata(name, desired);
        await _gate.WaitAsync(ct);
        try
        {
            var lanes = await RefreshAsync(ct);
            if (!lanes.TryGetValue(name, out var existing))
            {
                if (lanes.Count >= PipelineLaneProtocol.MaxLanes)
                    throw new InvalidOperationException("Lane count exceeds stage-1 limit (128)");
                try
                {
                    await admin.CreateSubscriptionAsync(new CreateSubscriptionOptions(topic, name)
                    {
                        UserMetadata = JsonSerializer.Serialize(desired),
                        LockDuration = TimeSpan.FromMinutes(5),
                        MaxDeliveryCount = 10,
                        DeadLetteringOnMessageExpiration = true,
                    }, new CreateRuleOptions("pipeline", new CorrelationRuleFilter { Subject = desired.PipelineId }), ct);
                }
                catch (ServiceBusException ex) when (ex.Reason == ServiceBusFailureReason.MessagingEntityAlreadyExists) { }
                catch (RequestFailedException ex) when (ex.Status == 409) { }
                var subscription = await admin.GetSubscriptionAsync(topic, name, ct);
                existing = await ValidateSubscriptionAsync(subscription.Value, ct);
                lanes[name] = existing;
            }
            if (existing != desired)
                throw new InvalidOperationException($"Existing lane metadata differs; explicit reconciled migration required: {name}");
            return name;
        }
        finally { _gate.Release(); }
    }
}
