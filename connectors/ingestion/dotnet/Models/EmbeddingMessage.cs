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

    /// <summary>"text" (default) or "blob_ref" — when blob_ref, worker sends blob location to DocGrok for download + processing.</summary>
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
}
