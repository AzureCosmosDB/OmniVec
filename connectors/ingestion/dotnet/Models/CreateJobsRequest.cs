using System.Text.Json.Serialization;

namespace OmniVec.ChangeFeed.Models;

public class CreateJobsRequest
{
    [JsonPropertyName("jobs")]
    public List<CreateJobEntry> Jobs { get; set; } = new();
}

public class CreateJobEntry
{
    [JsonPropertyName("pipeline_id")]
    public string PipelineId { get; set; } = "";

    [JsonPropertyName("source_id")]
    public string SourceId { get; set; } = "";

    [JsonPropertyName("source_ref")]
    public string SourceRef { get; set; } = "";

    [JsonPropertyName("metadata")]
    public Dictionary<string, object> Metadata { get; set; } = new()
    {
        ["trigger"] = "change_feed_dotnet"
    };
}

public class CreateJobsResponse
{
    [JsonPropertyName("created")]
    public int Created { get; set; }

    [JsonPropertyName("skipped")]
    public int Skipped { get; set; }
}
