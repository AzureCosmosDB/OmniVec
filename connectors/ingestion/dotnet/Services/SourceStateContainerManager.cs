using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;

namespace OmniVec.ChangeFeed.Services;

/// <summary>Resolves the preprovisioned shared polling-source state container.</summary>
public sealed class SourceStateContainerManager
{
    private readonly CosmosClient _cosmosClient;
    private readonly ChangeFeedOptions _options;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Container? _container;

    public SourceStateContainerManager(
        [FromKeyedServices("lease")] CosmosClient cosmosClient,
        IOptions<ChangeFeedOptions> options)
    {
        _cosmosClient = cosmosClient;
        _options = options.Value;
    }

    public async Task<Container> GetContainerAsync(CancellationToken ct)
    {
        if (_container is not null) return _container;
        await _gate.WaitAsync(ct);
        try
        {
            if (_container is not null) return _container;
            var database = string.IsNullOrWhiteSpace(_options.LeaseCosmosDatabase)
                ? _options.OmniVecDatabase
                : _options.LeaseCosmosDatabase;
            var container = _cosmosClient
                .GetDatabase(database)
                .GetContainer(_options.StateContainerName);
            try
            {
                var response = await container.ReadContainerAsync(cancellationToken: ct);
                if (response.Resource.PartitionKeyPath != "/scopeId")
                    throw new InvalidOperationException(
                        $"State container {_options.StateContainerName} must use partition key /scopeId");
            }
            catch (CosmosException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                throw new InvalidOperationException(
                    $"Required state container '{_options.StateContainerName}' is missing in database '{database}'. " +
                    "Provision it through Bicep/Terraform before starting ingestion.", ex);
            }
            _container = container;
            return container;
        }
        finally
        {
            _gate.Release();
        }
    }
}
