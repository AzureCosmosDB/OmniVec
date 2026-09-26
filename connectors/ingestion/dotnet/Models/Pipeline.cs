using System.Text.Json.Serialization;

namespace OmniVec.ChangeFeed.Models;

public class Pipeline
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("status")]
    public string Status { get; set; } = "";

    [JsonPropertyName("sources")]
    public List<PipelineSource> Sources { get; set; } = new();

    [JsonPropertyName("docgrok_pipeline")]
    public string DocgrokPipeline { get; set; } = "";

    [JsonPropertyName("processing_mode")]
    public string ProcessingMode { get; set; } = "queue";

    [JsonPropertyName("content_strategy")]
    public string ContentStrategy { get; set; } = "truncate";

    [JsonPropertyName("chunk_config")]
    public Dictionary<string, object>? ChunkConfig { get; set; }

    [JsonPropertyName("destination_id")]
    public string DestinationId { get; set; } = "";

    [JsonPropertyName("reset_at")]
    public string? ResetAt { get; set; }

    [JsonPropertyName("generation")]
    public string Generation { get; set; } = "1";

    [JsonPropertyName("vector_index_path")]
    public string VectorIndexPath { get; set; } = "embedding";

    [JsonPropertyName("doc_id_pattern")]
    public string DocIdPattern { get; set; } = "{source}";

    [JsonPropertyName("partition_key_pattern")]
    public string PartitionKeyPattern { get; set; } = "{source_partition}";

    /// <summary>
    /// Optional opt-in to persist the (already-truncated) embedded text on the
    /// destination document alongside the vector. null = per-destination
    /// default (Postgres/MsSql write content; Cosmos does not). true = always
    /// write. false = never write.
    /// </summary>
    [JsonPropertyName("store_content")]
    public bool? StoreContent { get; set; }

    /// <summary>
    /// Destination field name that receives the embedded text when
    /// store_content is true. Cosmos only. Default "content".
    /// </summary>
    [JsonPropertyName("content_field")]
    public string? ContentField { get; set; }

    [JsonPropertyName("metadata_fields")]
    public List<string>? MetadataFields { get; set; }

    [JsonPropertyName("resource_policy")]
    public PipelineResourcePolicy ResourcePolicy { get; set; } = new();
}

public class PipelineResourcePolicy
{
    [JsonPropertyName("weight")]
    public int Weight { get; set; } = 10;

    [JsonPropertyName("max_concurrency_per_worker")]
    public int MaxConcurrencyPerWorker { get; set; } = 2;

    [JsonPropertyName("priority")]
    public string Priority { get; set; } = "normal";

    [JsonPropertyName("workload_class")]
    public string WorkloadClass { get; set; } = "shared";
}

public class PipelineSource
{
    [JsonPropertyName("source_id")]
    public string SourceId { get; set; } = "";

    [JsonPropertyName("content_fields")]
    public List<string> ContentFields { get; set; } = new() { "content" };

    [JsonPropertyName("content_mode")]
    public string ContentMode { get; set; } = "field";

    [JsonPropertyName("url_content_types")]
    public List<string> UrlContentTypes { get; set; } = new() { "txt", "json", "pdf" };

    [JsonPropertyName("content_type_field")]
    public string? ContentTypeField { get; set; }

    [JsonPropertyName("file_types")]
    public List<string> FileTypes { get; set; } = new() { "txt", "json", "pdf", "docx", "md", "csv" };

    [JsonPropertyName("sharepoint_identity")]
    public SharePointPipelineIdentity? SharePointIdentity { get; set; }
}

public class SharePointPipelineIdentity
{
    [JsonPropertyName("tenant_id")]
    public string TenantId { get; set; } = "";

    [JsonPropertyName("client_id")]
    public string ClientId { get; set; } = "";
}

public class PipelinesResponse
{
    [JsonPropertyName("pipelines")]
    public List<Pipeline> Pipelines { get; set; } = new();
}
