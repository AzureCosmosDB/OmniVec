using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Resolves the preprovisioned shared CFP lease container.
/// Processor names isolate source and generation checkpoints within the container.
/// </summary>
public class LeaseContainerManager
{
    private readonly CosmosClient _cosmosClient;
    private readonly ChangeFeedOptions _options;
    private readonly ILogger<LeaseContainerManager> _logger;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Container? _container;

    public LeaseContainerManager(
        [FromKeyedServices("lease")] CosmosClient cosmosClient,
        IOptions<ChangeFeedOptions> options,
        ILogger<LeaseContainerManager> logger)
    {
        _cosmosClient = cosmosClient;
        _options = options.Value;
        _logger = logger;
    }

    private string LeaseDatabase =>
        string.IsNullOrWhiteSpace(_options.LeaseCosmosDatabase)
            ? _options.OmniVecDatabase
            : _options.LeaseCosmosDatabase;

    /// <summary>Return the shared lease container after verifying that deployment provisioning created it.</summary>
    public async Task<Container> EnsureLeaseContainerAsync(string sourceId, CancellationToken ct)
    {
        if (_container is not null) return _container;
        await _gate.WaitAsync(ct);
        try
        {
            if (_container is not null) return _container;
            var db = _cosmosClient.GetDatabase(LeaseDatabase);
            var container = db.GetContainer(_options.LeaseContainerName);
            try
            {
                var response = await container.ReadContainerAsync(cancellationToken: ct);
                if (response.Resource.PartitionKeyPath != "/id")
                    throw new InvalidOperationException(
                        $"Lease container {_options.LeaseContainerName} must use partition key /id");
            }
            catch (CosmosException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                throw new InvalidOperationException(
                    $"Required lease container '{_options.LeaseContainerName}' is missing in database '{LeaseDatabase}'. " +
                    "Provision it through Bicep/Terraform before starting ingestion.", ex);
            }
            _container = container;
            _logger.LogInformation(
                "Using shared lease container {Container} for source {SourceId}",
                _options.LeaseContainerName, sourceId);
            return container;
        }
        finally
        {
            _gate.Release();
        }
    }
}
