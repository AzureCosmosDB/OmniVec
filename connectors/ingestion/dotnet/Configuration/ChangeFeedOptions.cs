namespace OmniVec.ChangeFeed.Configuration;

public class ChangeFeedOptions
{
    /// <summary>OmniVec API base URL (internal K8s service)</summary>
    public string OmniVecApiBaseUrl { get; set; } = "http://omnivec-api:80";

    /// <summary>OmniVec CosmosDB endpoint (for lease containers)</summary>
    public string OmniVecCosmosEndpoint { get; set; } = "";

    /// <summary>OmniVec CosmosDB database name</summary>
    public string OmniVecDatabase { get; set; } = "omnivec";

    /// <summary>How often to poll the API for source/pipeline changes</summary>
    public int SourcePollIntervalSeconds { get; set; } = 30;

    /// <summary>Instance name for lease ownership (defaults to hostname)</summary>
    public string InstanceName { get; set; } = Environment.MachineName;

    /// <summary>Max items per change feed batch</summary>
    public int MaxItemsPerBatch { get; set; } = 500;

    /// <summary>Change feed poll interval</summary>
    public int FeedPollIntervalSeconds { get; set; } = 5;

    /// <summary>Max retries for job creation API calls</summary>
    public int MaxJobCreationRetries { get; set; } = 3;

    /// <summary>Backoff on errors (seconds)</summary>
    public int ErrorBackoffSeconds { get; set; } = 30;

    /// <summary>DocGrok base URL for inline processing</summary>
    public string DocGrokBaseUrl { get; set; } = "http://docgrok.omnivec.svc.cluster.local:80";

    /// <summary>Service Bus fully qualified namespace for queue mode</summary>
    public string ServiceBusNamespace { get; set; } = "";

    /// <summary>Service Bus topic name for embedding messages</summary>
    public string ServiceBusTopicName { get; set; } = "embeddings";

    /// <summary>Enable Service Bus publishing for queue mode (false = use legacy API job creation)</summary>
    public bool ServiceBusEnabled { get; set; } = false;

    /// <summary>Max active messages in Service Bus subscription before pausing blob enumeration</summary>
    public int BackpressureMaxActiveMessages { get; set; } = 5000;

    /// <summary>How long to wait (seconds) when backpressure threshold is hit before rechecking</summary>
    public int BackpressurePauseSeconds { get; set; } = 30;

    /// <summary>Page size for blob enumeration (number of blobs per page)</summary>
    public int BlobEnumerationPageSize { get; set; } = 500;
}
