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
}

public class PipelineSource
{
    [JsonPropertyName("source_id")]
    public string SourceId { get; set; } = "";

    [JsonPropertyName("content_fields")]
    public List<string> ContentFields { get; set; } = new() { "content" };
}

public class PipelinesResponse
{
    [JsonPropertyName("pipelines")]
    public List<Pipeline> Pipelines { get; set; } = new();
}
