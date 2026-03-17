using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Common interface for all source watchers (CosmosDB, MS SQL, PostgreSQL).
/// </summary>
public interface ISourceWatcher : IAsyncDisposable
{
    string SourceId { get; }
    string Generation { get; }
    bool SkipContentHash { get; set; }
    void UpdatePipelines(List<Pipeline> pipelines);
    void UpdateDestinations(List<Destination> destinations);
    Task StartAsync(CancellationToken ct);
}
