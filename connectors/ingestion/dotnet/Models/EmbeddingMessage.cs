using System.Text.Json.Serialization;

namespace OmniVec.ChangeFeed.Models;

public class EmbeddingMessage
{
    [JsonPropertyName("message_id")]
    public string MessageId { get; set; } = Guid.NewGuid().ToString();

    [JsonPropertyName("pipeline_id")]
    public string PipelineId { get; set; } = "";

    [JsonPropertyName("pipeline_name")]
    public string PipelineName { get; set; } = "";

    [JsonPropertyName("docgrok_pipeline")]
    public string DocgrokPipeline { get; set; } = "";

    [JsonPropertyName("source_id")]
    public string SourceId { get; set; } = "";

    [JsonPropertyName("source_ref")]
    public string SourceRef { get; set; } = "";

    [JsonPropertyName("destination_id")]
    public string DestinationId { get; set; } = "";

    [JsonPropertyName("destination_type")]
    public string DestinationType { get; set; } = "";

    [JsonPropertyName("destination_config")]
    public Dictionary<string, object> DestinationConfig { get; set; } = new();

    [JsonPropertyName("content")]
    public string Content { get; set; } = "";

    [JsonPropertyName("content_hash")]
    public string ContentHash { get; set; } = "";

    [JsonPropertyName("partition_key_value")]
    public string PartitionKeyValue { get; set; } = "";

    [JsonPropertyName("content_strategy")]
    public string ContentStrategy { get; set; } = "truncate";

    [JsonPropertyName("doc_id_pattern")]
    public string DocIdPattern { get; set; } = "{source}";

    [JsonPropertyName("pipeline_generation")]
    public string PipelineGeneration { get; set; } = "";

    /// <summary>
    /// Content fields from the source document, keyed by their original field names.
    /// Used by the worker to preserve original field names when writing to a separate destination.
    /// </summary>
    [JsonPropertyName("source_content_fields")]
    public Dictionary<string, string> SourceContentFields { get; set; } = new();

    [JsonPropertyName("enqueued_at")]
    public string EnqueuedAt { get; set; } = DateTime.UtcNow.ToString("O");

    /// <summary>Pipeline-level flag: persist the (possibly-truncated) content on the destination document alongside the vector.</summary>
    [JsonPropertyName("store_content")]
    public bool? StoreContent { get; set; }

    /// <summary>Destination field name receiving the embedded text (Cosmos only; default "content").</summary>
    [JsonPropertyName("content_field")]
    public string? ContentField { get; set; }

    /// <summary>Pipeline-level: subset of optional metadata fields to write on destination docs (null = all, empty = none).</summary>
    [JsonPropertyName("metadata_fields")]
    public List<string>? MetadataFields { get; set; }

    /// <summary>"text", "blob_ref", or "sharepoint_ref".</summary>
    [JsonPropertyName("content_type")]
    public string ContentType { get; set; } = "text";

    /// <summary>Blob storage account URL (for managed identity access)</summary>
    [JsonPropertyName("blob_account_url")]
    public string? BlobAccountUrl { get; set; }

    /// <summary>Blob storage connection string (for key-based access)</summary>
    [JsonPropertyName("blob_connection_string")]
    public string? BlobConnectionString { get; set; }

    /// <summary>Blob container name</summary>
    [JsonPropertyName("blob_container")]
    public string? BlobContainer { get; set; }

    /// <summary>Blob name (path within container)</summary>
    [JsonPropertyName("blob_name")]
    public string? BlobName { get; set; }

    [JsonPropertyName("sharepoint_site_id")]
    public string? SharePointSiteId { get; set; }

    [JsonPropertyName("sharepoint_drive_id")]
    public string? SharePointDriveId { get; set; }

    [JsonPropertyName("sharepoint_item_id")]
    public string? SharePointItemId { get; set; }

    [JsonPropertyName("sharepoint_file_name")]
    public string? SharePointFileName { get; set; }

    [JsonPropertyName("sharepoint_max_file_size_bytes")]
    public long SharePointMaxFileSizeBytes { get; set; } = 50L * 1024 * 1024;

    /// <summary>"upsert" (default) or "delete". When "delete", the worker
    /// removes all destination documents matching source_id + source_ref.</summary>
    [JsonPropertyName("message_type")]
    public string MessageType { get; set; } = "upsert";
}
