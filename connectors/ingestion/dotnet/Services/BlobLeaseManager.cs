using System.Net;
using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Distributed lease manager for Blob sources. Prevents duplicate enumeration across
/// multiple change-feed pod replicas (default replicaCount=15).
///
/// Each pod calls TryAcquireAsync(sourceId) on every reconcile cycle. At most one pod
/// holds a valid lease at any time. Expired leases (owner missed renewals for >
/// LeaseTtlSeconds) can be stolen by any other pod using a conditional ETag update.
///
/// Lease doc schema (container: blob-leases in omnivec-cosmos):
///   { "id": "<sourceId>", "ownerId": "<hostname>", "expiresAt": "<iso-utc>" }
/// Partition key: /id
/// </summary>
public class BlobLeaseManager
{
    private readonly CosmosClient _cosmos;
    private readonly ChangeFeedOptions _options;
    private readonly ILogger<BlobLeaseManager> _logger;
    private readonly string _ownerId;
    private Container? _container;
    private readonly SemaphoreSlim _ensureGate = new(1, 1);

    // Tunables
    public int LeaseTtlSeconds { get; set; } = 90;  // stale after 90s without renewal

    public BlobLeaseManager(
        CosmosClient cosmos,
        IOptions<ChangeFeedOptions> options,
        ILogger<BlobLeaseManager> logger)
    {
        _cosmos = cosmos;
        _options = options.Value;
        _logger = logger;
        _ownerId = Environment.GetEnvironmentVariable("HOSTNAME")
                   ?? Environment.GetEnvironmentVariable("POD_NAME")
                   ?? Guid.NewGuid().ToString("N");
    }

    public string OwnerId => _ownerId;

    private async Task<Container> EnsureContainerAsync(CancellationToken ct)
    {
        if (_container is not null) return _container;
        await _ensureGate.WaitAsync(ct);
        try
        {
            if (_container is not null) return _container;
            var db = _cosmos.GetDatabase(_options.OmniVecDatabase);
            await db.CreateContainerIfNotExistsAsync(
                new ContainerProperties("blob-leases", "/id"),
                cancellationToken: ct);
            _container = db.GetContainer("blob-leases");
            return _container;
        }
        finally
        {
            _ensureGate.Release();
        }
    }

    /// <summary>
    /// Try to acquire or renew the lease for <paramref name="sourceId"/>.
    /// Returns true iff this pod currently owns the lease (and renewal was written).
    /// Safe to call on every reconcile pass.
    /// </summary>
    public async Task<bool> TryAcquireAsync(string sourceId, CancellationToken ct)
    {
        try
        {
            var container = await EnsureContainerAsync(ct);
            var pk = new PartitionKey(sourceId);
            var nowUtc = DateTime.UtcNow;
            var newExpiresAt = nowUtc.AddSeconds(LeaseTtlSeconds).ToString("o");

            // Read existing lease (if any)
            ItemResponse<LeaseDoc>? existing = null;
            try
            {
                existing = await container.ReadItemAsync<LeaseDoc>(sourceId, pk, cancellationToken: ct);
            }
            catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
            {
                // No lease yet — try to create with IfNoneMatch on ETag
                try
                {
                    await container.CreateItemAsync(
                        new LeaseDoc { Id = sourceId, OwnerId = _ownerId, ExpiresAt = newExpiresAt },
                        pk,
                        cancellationToken: ct);
                    return true;
                }
                catch (CosmosException ex2) when (ex2.StatusCode == HttpStatusCode.Conflict)
                {
                    // Someone else created it between read and create — retry as a read below.
                    existing = await container.ReadItemAsync<LeaseDoc>(sourceId, pk, cancellationToken: ct);
                }
            }

            if (existing is null) return false;

            var doc = existing.Resource;
            var etag = existing.ETag;

            bool isOwner = doc.OwnerId == _ownerId;
            bool isExpired = !DateTime.TryParse(doc.ExpiresAt, null,
                System.Globalization.DateTimeStyles.AssumeUniversal | System.Globalization.DateTimeStyles.AdjustToUniversal,
                out var exp) || exp < nowUtc;

            if (!isOwner && !isExpired)
            {
                return false; // Someone else holds a valid lease
            }

            // Either we own it (renewal) or it is expired (steal). Either way, write with IfMatch.
            doc.OwnerId = _ownerId;
            doc.ExpiresAt = newExpiresAt;
            try
            {
                await container.ReplaceItemAsync(
                    doc, sourceId, pk,
                    new ItemRequestOptions { IfMatchEtag = etag },
                    cancellationToken: ct);
                return true;
            }
            catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.PreconditionFailed)
            {
                // Another pod won the race. That's fine.
                return false;
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "BlobLeaseManager.TryAcquireAsync failed for source {SourceId}", sourceId);
            return false;
        }
    }

    /// <summary>Best-effort release of a lease (on watcher dispose).</summary>
    public async Task ReleaseAsync(string sourceId, CancellationToken ct)
    {
        try
        {
            if (_container is null) return;
            var pk = new PartitionKey(sourceId);
            var existing = await _container.ReadItemAsync<LeaseDoc>(sourceId, pk, cancellationToken: ct);
            if (existing.Resource.OwnerId != _ownerId) return;
            // Expire it immediately.
            existing.Resource.ExpiresAt = DateTime.UtcNow.AddSeconds(-1).ToString("o");
            await _container.ReplaceItemAsync(
                existing.Resource, sourceId, pk,
                new ItemRequestOptions { IfMatchEtag = existing.ETag },
                cancellationToken: ct);
        }
        catch { /* best-effort */ }
    }

    private class LeaseDoc
    {
        [System.Text.Json.Serialization.JsonPropertyName("id")]
        public string Id { get; set; } = "";
        [System.Text.Json.Serialization.JsonPropertyName("ownerId")]
        public string OwnerId { get; set; } = "";
        [System.Text.Json.Serialization.JsonPropertyName("expiresAt")]
        public string ExpiresAt { get; set; } = "";
    }
}
