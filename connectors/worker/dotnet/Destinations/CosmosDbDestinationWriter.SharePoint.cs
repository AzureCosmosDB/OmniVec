using System.Net;
using System.Text;
using Microsoft.Azure.Cosmos;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OmniVec.Worker.Destinations;

public partial class CosmosDbDestinationWriter
{
    private const string SyncField = "_omnivec_sync";
    private static readonly HashSet<string> SyncReservedFields = new(StringComparer.Ordinal)
    {
        "id", SyncField, "source_id", "pipeline_id", "source_ref", "embedded_at",
        "pipeline_generation", "pipeline_name", "content_hash", "embedding_dims", "ttl",
    };

    public async Task<bool> ReplaceSharePointAsync(
        Dictionary<string, object> config, SharePointReplacement replacement, CancellationToken ct)
    {
        var container = GetOrCreateClient(config["endpoint"].ToString()!)
            .GetContainer(config["database"].ToString()!, config["container"].ToString()!);
        var properties = await container.ReadContainerAsync(cancellationToken: ct);
        if (properties.Resource.PartitionKeyPaths is { Count: > 1 })
            throw new InvalidOperationException("SharePoint does not support hierarchical partition keys");
        return await ReplaceSharePointInStoreAsync(new CosmosSharePointSyncStore(container, replacement.Identity),
            properties.Resource.PartitionKeyPath,
            config.GetValueOrDefault("vector_field")?.ToString() ?? "embedding", replacement, ct);
    }

    internal async Task<bool> ReplaceSharePointInStoreAsync(
        ISharePointSyncStore store, string pkPath, string vectorField,
        SharePointReplacement replacement, CancellationToken ct)
    {
        ValidateSharePointFields(pkPath, vectorField, replacement);
        var stateId = replacement.Identity + "-state";
        var manifest = CreateSharePointItem(stateId, pkPath, replacement, "state");
        manifest[SyncField]!["completed"] = false;
        manifest["ttl"] = -1;
        var documents = replacement.Chunks.Select(chunk =>
            CreateSharePointChunk(pkPath, vectorField, replacement, chunk)).ToList();
        // Validate all payloads before claiming a revision or writing any vectors.
        var batches = SharePointBatches(documents).ToList();
        string etag;
        while (true)
        {
            var current = await store.ReadStateAsync(stateId, ct);
            if (current is not null)
            {
                var revision = current.Document[SyncField]!["revision"]!.Value<long>();
                if (revision > replacement.Revision) return false;
                if (revision == replacement.Revision
                    && current.Document[SyncField]!["completed"]!.Value<bool>())
                    return true;
            }
            var claimed = await store.TryClaimAsync(manifest, current?.ETag, ct);
            if (claimed is not null)
            {
                etag = claimed;
                break;
            }
        }

        foreach (var batch in batches)
            etag = await store.ExecuteAsync(manifest, etag, batch, [], ct);

        // Old vectors remain until EVERY new vector is safely written. Each
        // mutation also compares the manifest ETag, fencing superseded workers
        // across replicas and even after their Service Bus locks have expired.
        var keep = documents.Select(doc => doc["id"]!.Value<string>()!).ToHashSet(StringComparer.Ordinal);
        var obsolete = new List<string>();
        await foreach (var id in store.ReadChunkIdsAsync(replacement, ct))
            if (!keep.Contains(id)) obsolete.Add(id);
        foreach (var batch in obsolete.Chunk(99))
            etag = await store.ExecuteAsync(manifest, etag, [], batch, ct);

        // Retain this high-water mark even after delete/empty results, otherwise
        // a delayed older upsert could resurrect the deleted item.
        manifest[SyncField]!["completed"] = true;
        await store.ExecuteAsync(manifest, etag, [], [], ct);
        return true;
    }

    private sealed class CosmosSharePointSyncStore(Container container, string identity) : ISharePointSyncStore
    {
        private readonly PartitionKey _pk = new(identity);

        public async Task<SharePointSyncState?> ReadStateAsync(string id, CancellationToken ct)
        {
            try
            {
                var response = await container.ReadItemAsync<JObject>(id, _pk, cancellationToken: ct);
                return new(response.Resource, response.ETag);
            }
            catch (CosmosException ex) when (ex.StatusCode == HttpStatusCode.NotFound) { return null; }
        }

        public async Task<string?> TryClaimAsync(JObject manifest, string? etag, CancellationToken ct)
        {
            try
            {
                var response = etag is null
                    ? await container.CreateItemAsync(manifest, _pk, cancellationToken: ct)
                    : await container.ReplaceItemAsync(manifest, manifest["id"]!.Value<string>(), _pk,
                        new ItemRequestOptions { IfMatchEtag = etag }, ct);
                return response.ETag;
            }
            catch (CosmosException ex) when (ex.StatusCode is HttpStatusCode.Conflict or HttpStatusCode.PreconditionFailed)
            { return null; }
        }

        public async Task<string> ExecuteAsync(
            JObject manifest, string etag, IReadOnlyList<JObject> writes,
            IReadOnlyList<string> deletes, CancellationToken ct)
        {
            var batch = container.CreateTransactionalBatch(_pk).ReplaceItem(
                manifest["id"]!.Value<string>(), manifest,
                new TransactionalBatchItemRequestOptions { IfMatchEtag = etag });
            foreach (var doc in writes) batch.UpsertItem(doc);
            foreach (var id in deletes) batch.DeleteItem(id);
            using var response = await batch.ExecuteAsync(ct);
            if (!response.IsSuccessStatusCode)
                throw new InvalidOperationException(
                    $"SharePoint guarded transaction failed ({response.StatusCode}); replay the message");
            return response[0].ETag;
        }

        public async IAsyncEnumerable<string> ReadChunkIdsAsync(
            SharePointReplacement replacement,
            [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
        {
            var query = new QueryDefinition(
                "SELECT c.id FROM c WHERE c._omnivec_sync.identity = @identity " +
                "AND c._omnivec_sync.kind = 'chunk' AND c.source_id = @source AND c.pipeline_id = @pipeline")
                .WithParameter("@identity", replacement.Identity)
                .WithParameter("@source", replacement.SourceId)
                .WithParameter("@pipeline", replacement.PipelineId);
            using var iterator = container.GetItemQueryIterator<DeletedIdDoc>(
                query, requestOptions: new QueryRequestOptions { PartitionKey = _pk });
            while (iterator.HasMoreResults)
                foreach (var doc in await iterator.ReadNextAsync(ct))
                    yield return doc.Id;
        }
    }

    internal record SharePointSyncState(JObject Document, string ETag);

    internal interface ISharePointSyncStore
    {
        Task<SharePointSyncState?> ReadStateAsync(string id, CancellationToken ct);
        Task<string?> TryClaimAsync(JObject manifest, string? etag, CancellationToken ct);
        Task<string> ExecuteAsync(JObject manifest, string etag, IReadOnlyList<JObject> writes,
            IReadOnlyList<string> deletes, CancellationToken ct);
        IAsyncEnumerable<string> ReadChunkIdsAsync(SharePointReplacement replacement, CancellationToken ct);
    }

    private static IEnumerable<List<JObject>> SharePointBatches(List<JObject> documents)
    {
        var batch = new List<JObject>();
        var bytes = 0;
        foreach (var doc in documents)
        {
            var size = Encoding.UTF8.GetByteCount(doc.ToString(Formatting.None));
            // Leave room below Cosmos's 2 MiB transaction limit for the manifest
            // and the SDK's operation envelope; do not rely on count alone.
            if (size > 1_500_000)
                throw new InvalidOperationException("SharePoint chunk is too large for a Cosmos transaction");
            if (batch.Count == 99 || bytes + size > 1_500_000)
            {
                yield return batch;
                batch = new();
                bytes = 0;
            }
            batch.Add(doc);
            bytes += size;
        }
        if (batch.Count > 0) yield return batch;
    }

    private static void ValidateSharePointFields(
        string pkPath, string vectorField, SharePointReplacement replacement)
    {
        var pkFields = pkPath.TrimStart('/').Split('/');
        if (!pkPath.StartsWith('/') || pkFields.Any(string.IsNullOrWhiteSpace)
            || SyncReservedFields.Contains(pkFields[0]) || pkFields[0] == vectorField)
            throw new InvalidOperationException(
                "SharePoint requires a dedicated document partition key (for example /document_id), not /id or a metadata/vector field");
        if (string.IsNullOrWhiteSpace(vectorField) || vectorField.Contains('/')
            || SyncReservedFields.Contains(vectorField))
            throw new InvalidOperationException("SharePoint vector field conflicts with synchronization fields");
        if (replacement.Revision <= 0 || string.IsNullOrWhiteSpace(replacement.Identity)
            || string.IsNullOrWhiteSpace(replacement.SourceId) || string.IsNullOrWhiteSpace(replacement.PipelineId))
            throw new InvalidOperationException("Invalid SharePoint synchronization identity/revision");
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var chunk in replacement.Chunks)
        {
            if (!ids.Add(chunk.DocId) || chunk.PartitionKeyValue != replacement.Identity
                || chunk.SourceId != replacement.SourceId || chunk.PipelineId != replacement.PipelineId
                || chunk.DocId.IndexOfAny(['/', '\\', '?', '#']) >= 0
                || !chunk.DocId.StartsWith($"{replacement.Identity}-{replacement.Revision}-chunk-", StringComparison.Ordinal)
                || chunk.Embedding.Length == 0 || chunk.Embedding.Any(value => !float.IsFinite(value)))
                throw new InvalidOperationException("Invalid SharePoint chunk identity or embedding");
            var contentField = chunk.ContentField ?? "content";
            if (chunk.StoreContent == true && (SyncReservedFields.Contains(contentField)
                || contentField == vectorField || contentField == pkFields[0] || contentField.Contains('/')))
                throw new InvalidOperationException("SharePoint content field conflicts with synchronization fields");
        }
    }

    private static JObject CreateSharePointItem(
        string id, string pkPath, SharePointReplacement replacement, string kind)
    {
        var item = new JObject
        {
            ["id"] = id,
            ["source_id"] = replacement.SourceId,
            ["pipeline_id"] = replacement.PipelineId,
            ["source_ref"] = replacement.SourceRef,
            [SyncField] = new JObject
            {
                ["identity"] = replacement.Identity,
                ["revision"] = replacement.Revision,
                ["kind"] = kind,
            },
        };
        var parts = pkPath.TrimStart('/').Split('/');
        var parent = item;
        foreach (var part in parts.SkipLast(1))
        {
            var nested = new JObject();
            parent[part] = nested;
            parent = nested;
        }
        parent[parts[^1]] = replacement.Identity;
        return item;
    }

    private static JObject CreateSharePointChunk(
        string pkPath, string vectorField, SharePointReplacement replacement, EmbeddingResult chunk)
    {
        var item = CreateSharePointItem(chunk.DocId, pkPath, replacement, "chunk");
        item[vectorField] = JArray.FromObject(chunk.Embedding);
        item["embedded_at"] = DateTime.UtcNow.ToString("O");
        item["content_hash"] = chunk.ContentHash;
        item["pipeline_generation"] = chunk.PipelineGeneration;
        if (chunk.ShouldIncludeMetadata("embedding_dims")) item["embedding_dims"] = chunk.Embedding.Length;
        if (chunk.ShouldIncludeMetadata("pipeline_name")) item["pipeline_name"] = chunk.PipelineName;
        if (chunk.StoreContent == true) item[chunk.ContentField ?? "content"] = chunk.Content;
        return item;
    }
}
