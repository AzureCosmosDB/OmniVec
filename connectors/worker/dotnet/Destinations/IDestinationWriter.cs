namespace OmniVec.Worker.Destinations;

public record EmbeddingResult(
    string DocId,
    string SourceRef,
    float[] Embedding,
    string ContentHash,
    string PartitionKeyValue,
    string PipelineId,
    string PipelineName,
    string PipelineGeneration,
    string Content,
    Dictionary<string, string>? SourceContentFields = null,
    string SourceId = "");

public interface IDestinationWriter
{
    string DestinationType { get; }

    Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct);
}
