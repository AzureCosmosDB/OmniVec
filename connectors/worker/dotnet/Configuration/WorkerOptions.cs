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

    /// <summary>
    /// Maximum estimated tokens per /embed/batch call. Batches are packed
    /// up to this budget using a script-aware estimator. Default 6000 leaves
    /// ~25% headroom under the common 8191-token limit (text-embedding-3-*,
    /// ada-002). Lower this for models with smaller context (e.g. BGE-base = 512).
    /// </summary>
    public int MaxBatchTokens { get; set; } = 6000;

    /// <summary>
    /// Maximum estimated tokens for a single text. Messages exceeding this
    /// are truncated (if TruncateOversized=true) or dead-lettered. Default
    /// 7500 = soft per-input ceiling for 8191-token models.
    /// </summary>
    public int MaxSingleTextTokens { get; set; } = 7500;

    /// <summary>
    /// If true, oversized single texts are truncated (by chars, proportional to
    /// estimated tokens) and embedded. If false, they are dead-lettered.
    /// </summary>
    public bool TruncateOversized { get; set; } = true;

    /// <summary>
    /// Max concurrent blob_ref (image/PDF) embed calls per worker replica.
    /// Each call hits docgrok router → CLIP/pipeline-worker. Higher values
    /// let CLIP's server-side dynamic batcher fill its window (CLIP_DYN_BATCH_MAX)
    /// — required to push image throughput past the single-call latency floor.
    /// Default 32 × 8 worker replicas ≈ 256 in-flight, comfortably saturating
    /// a CLIP batch of 64.
    /// </summary>
    public int BlobConcurrency { get; set; } = 32;

    /// <summary>
    /// Number of blob_ref messages bundled into a single bulk /embed call.
    /// Each batch becomes one HTTP request that the docgrok router forwards
    /// to the backend's /v1/embeddings (parallel download + single batched
    /// forward pass). 128 is a good fit for CLIP — large enough to amortize
    /// HTTP + GPU launch overhead, small enough that one slow image doesn't
    /// stall too many siblings.
    /// </summary>
    public int BlobBatchSize { get; set; } = 128;
}
