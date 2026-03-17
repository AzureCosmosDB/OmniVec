using System.Collections.Concurrent;
using System.Net;
using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Manages lease containers in omnivec-cosmos. Creates one per source: leases-{source_id}.
/// Lease containers store CFP checkpoint state so progress survives restarts.
/// </summary>
public class LeaseContainerManager
{
    private readonly CosmosClient _cosmosClient;
    private readonly ChangeFeedOptions _options;
    private readonly ILogger<LeaseContainerManager> _logger;
    private readonly ConcurrentDictionary<string, bool> _ensured = new();

    public LeaseContainerManager(
        CosmosClient cosmosClient,
        IOptions<ChangeFeedOptions> options,
        ILogger<LeaseContainerManager> logger)
    {
        _cosmosClient = cosmosClient;
        _options = options.Value;
        _logger = logger;
    }

    /// <summary>Ensure the lease container exists for a source, return a reference to it.</summary>
    public async Task<Container> EnsureLeaseContainerAsync(string sourceId, CancellationToken ct)
    {
        var db = _cosmosClient.GetDatabase(_options.OmniVecDatabase);
        var containerName = $"leases-{sourceId}";

        if (!_ensured.ContainsKey(sourceId))
        {
            try
            {
                await db.CreateContainerIfNotExistsAsync(
                    new ContainerProperties(containerName, "/id"),
                    cancellationToken: ct);
                _ensured.TryAdd(sourceId, true);
                _logger.LogInformation("Ensured lease container {Container}", containerName);
            }
            catch (CosmosException ex)
            {
                _logger.LogError(ex, "Failed to create lease container {Container}", containerName);
                throw;
            }
        }

        return db.GetContainer(containerName);
    }

    /// <summary>Delete the lease container for a source (used during pipeline reset).</summary>
    public async Task DeleteLeaseContainerAsync(string sourceId, CancellationToken ct)
    {
        var db = _cosmosClient.GetDatabase(_options.OmniVecDatabase);
        var containerName = $"leases-{sourceId}";

        try
        {
            await db.GetContainer(containerName).DeleteContainerAsync(cancellationToken: ct);
            _ensured.TryRemove(sourceId, out _);
            _logger.LogInformation("Deleted lease container {Container} for reset", containerName);
        }
        catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            _logger.LogDebug("Lease container {Container} not found, nothing to delete", containerName);
            _ensured.TryRemove(sourceId, out _);
        }
        catch (CosmosException ex)
        {
            _logger.LogError(ex, "Failed to delete lease container {Container}", containerName);
            throw;
        }
    }
}
