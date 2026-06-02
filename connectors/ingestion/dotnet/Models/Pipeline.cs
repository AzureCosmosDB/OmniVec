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

    [JsonPropertyName("destination_id")]
    public string DestinationId { get; set; } = "";

    [JsonPropertyName("reset_at")]
    public string? ResetAt { get; set; }

    [JsonPropertyName("generation")]
    public string Generation { get; set; } = "1";

    [JsonPropertyName("vector_index_path")]
    public string VectorIndexPath { get; set; } = "embedding";

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
}

public class PipelinesResponse
{
    [JsonPropertyName("pipelines")]
    public List<Pipeline> Pipelines { get; set; } = new();
}
