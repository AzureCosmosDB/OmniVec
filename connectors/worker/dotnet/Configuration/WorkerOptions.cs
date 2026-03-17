namespace OmniVec.Worker.Configuration;

public class WorkerOptions
{
    /// <summary>Service Bus fully qualified namespace (e.g., omnivec-sb-xxx.servicebus.windows.net)</summary>
    public string ServiceBusNamespace { get; set; } = "";

    /// <summary>Topic name for embedding messages</summary>
    public string TopicName { get; set; } = "embeddings";

    /// <summary>Subscription name for this worker</summary>
    public string SubscriptionName { get; set; } = "worker";

    /// <summary>DocGrok base URL for embedding calls</summary>
    public string DocGrokBaseUrl { get; set; } = "http://docgrok:80";

    /// <summary>OmniVec API base URL for metrics reporting</summary>
    public string OmniVecApiBaseUrl { get; set; } = "http://omnivec-api:80";

    /// <summary>Max concurrent Service Bus message processing</summary>
    public int MaxConcurrentCalls { get; set; } = 10;

    /// <summary>How many texts to batch per /embed/batch call</summary>
    public int EmbedBatchSize { get; set; } = 50;

    /// <summary>How long to wait to accumulate a micro-batch (ms)</summary>
    public int BatchAccumulateMs { get; set; } = 500;

    /// <summary>Auto-lock renewal duration for long-running processing</summary>
    public int MaxLockRenewalMinutes { get; set; } = 10;
}
