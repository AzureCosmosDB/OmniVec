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
    List<string>? MetadataFields = null,
    string? ContentField = null)
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

    /// <summary>
    /// Remove all destination documents matching (source_id, source_ref) for the
    /// given pipeline. Used by Event-Grid-driven blob delete propagation.
    /// Default implementation is a no-op so existing writers compile without
    /// change; destinations that can support deletes (e.g. Cosmos) override it.
    /// </summary>
    Task DeleteByRefAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
        CancellationToken ct) => Task.CompletedTask;
}

public record DeleteRequest(
    string SourceId,
    string SourceRef,
    string PartitionKeyValue,
    string PipelineId);
