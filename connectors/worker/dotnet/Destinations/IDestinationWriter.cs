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
    string SourceId = "",
    bool? StoreContent = null,
    List<string>? MetadataFields = null)
{
    /// <summary>
    /// Returns true when the named optional metadata field should be written.
    /// Convention: null MetadataFields → write everything (back-compat default).
    /// Empty list → write nothing optional.
    /// </summary>
    public bool ShouldIncludeMetadata(string field)
        => MetadataFields is null || MetadataFields.Contains(field);
}

public interface IDestinationWriter
{
    string DestinationType { get; }

    Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct);
}
