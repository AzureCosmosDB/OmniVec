using System.Collections.Concurrent;
using Azure.Identity;
using Microsoft.Azure.Cosmos;

namespace OmniVec.Worker.Destinations;

public partial class CosmosDbDestinationWriter : IDestinationWriter
{
    private const string CosmosDataUserAgent = "OmniVec-DataCosmos/1.0";

    private readonly ILogger<CosmosDbDestinationWriter> _logger;
    private static readonly ConcurrentDictionary<string, CosmosClient> _clients = new();
    private static readonly ConcurrentDictionary<string, string> _pkPathCache = new();

    public string DestinationType => "cosmosdb-vector";

    public async Task ReplaceTextChunksAsync(Dictionary<string, object> config, DeleteRequest source,
        List<EmbeddingResult> chunks, CancellationToken ct)
    {
        var container = GetOrCreateClient(config["endpoint"].ToString()!)
            .GetContainer(config["database"].ToString()!, config["container"].ToString()!);
        var props = await container.ReadContainerAsync(cancellationToken: ct);
        var pkField = props.Resource.PartitionKeyPath.TrimStart('/');
        var vectorField = config.GetValueOrDefault("vector_field")?.ToString() ?? "embedding";
        if (props.Resource.PartitionKeyPaths is { Count: > 1 } || pkField.Contains('/')
            || (pkField != "id" && Services.CosmosTextChunker.Reserved.Contains(pkField))
            || vectorField.Contains('/') || Services.CosmosTextChunker.Reserved.Contains(vectorField)
            || vectorField == pkField
            || chunks.Any(c => c.ContentField == pkField || c.ContentField == vectorField))
            throw new NotSupportedException("Cosmos chunk fields conflict with partition/vector/metadata fields");
        await ReplaceTextChunksInStoreAsync(chunks,
            items => WriteBatchAsync(config, items, ct),
            keep => DeleteByRefAsync(config, [source with { KeepIds = keep }], ct));
    }

    internal static async Task ReplaceTextChunksInStoreAsync(List<EmbeddingResult> chunks,
        Func<List<EmbeddingResult>, Task> write, Func<HashSet<string>, Task> cleanup)
    {
        // Never erase the last good set until every new vector has been persisted.
        await write(chunks);
        await cleanup(chunks.Select(c => c.DocId).ToHashSet(StringComparer.Ordinal));
    }

    public CosmosDbDestinationWriter(ILogger<CosmosDbDestinationWriter> logger)
    {
        _logger = logger;
    }

    public async Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var endpoint = config["endpoint"]?.ToString() ?? "";
        var database = config["database"]?.ToString() ?? "";
        var containerName = config["container"]?.ToString() ?? "";

        var client = GetOrCreateClient(endpoint);
        var container = client.GetDatabase(database).GetContainer(containerName);

        // Read vector path from destination config (set by API probe of container's vector policy)
        var vectorField = config.ContainsKey("vector_field") ? config["vector_field"]?.ToString() ?? "embedding" : "embedding";

        // Resolve partition key path
        var cacheKey = $"{endpoint}/{database}/{containerName}";
        var pkPath = await GetPartitionKeyPathAsync(container, cacheKey, ct);
        var pkField = pkPath.TrimStart('/');

        // Group by partition key value
        var groups = results.GroupBy(r => DestinationPartitionKey(r, pkField));
        var tasks = new List<Task>();

        foreach (var group in groups)
        {
            var now = DateTime.UtcNow.ToString("O");
            foreach (var chunk in BuildPatchBatches(group, pkField, vectorField, now))
                tasks.Add(WriteBatchWithRetryAsync(container, group.Key, chunk, pkField, vectorField, now, ct));
        }

        await Task.WhenAll(tasks);
    }

    private static async Task<string> GetPartitionKeyPathAsync(Container container, string cacheKey, CancellationToken ct)
    {
        if (_pkPathCache.TryGetValue(cacheKey, out var path)) return path;
        var properties = await container.ReadContainerAsync(cancellationToken: ct);
        path = properties.Resource.PartitionKeyPath;
        _pkPathCache[cacheKey] = path;
        return path;
    }

    internal static string DestinationPartitionKey(EmbeddingResult document, string pkField)
        => pkField == "id" ? document.DocId : document.PartitionKeyValue;

    internal static IEnumerable<IGrouping<string, string>> GroupDeletionIds(
        IEnumerable<string> ids, string pkPath, string sourcePartition)
        => ids.GroupBy(id => pkPath == "/id" ? id : sourcePartition);

    private Task WriteBatchWithRetryAsync(
        Container container,
        string pkValue,
        List<DocumentPatch> docs,
        string pkField,
        string vectorField,
        string now,
        CancellationToken ct)
    {
        var pk = new PartitionKey(pkValue);
        return WritePatchBatchWithRetryAsync(docs, pkValue, async (items, token) =>
        {
            var batch = container.CreateTransactionalBatch(pk);
            foreach (var doc in items)
                foreach (var ops in doc.Operations)
                    batch.PatchItem(doc.Document.DocId, ops);
            using var response = await batch.ExecuteAsync(token);
            return response.StatusCode;
        }, (doc, token) => UpsertBatchWithRetryAsync(container, pk, [doc], pkField, vectorField, now, token), ct);
    }

    internal async Task WritePatchBatchWithRetryAsync(
        List<DocumentPatch> docs,
        string pkValue,
        Func<List<DocumentPatch>, CancellationToken, Task<System.Net.HttpStatusCode>> patch,
        Func<EmbeddingResult, CancellationToken, Task> upsertMissing,
        CancellationToken ct)
    {
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                // Always try patch first — preserves existing document fields.
                // Only fall back to upsert if patch fails with NotFound.
                var status = await patch(docs, ct);
                var statusCode = (int)status;
                if (statusCode is >= 200 and <= 299)
                    return;

                // A missing item rolls back the whole transaction. Retry items
                // separately so upserting a missing item cannot replace its neighbors.
                if (status == System.Net.HttpStatusCode.NotFound)
                {
                    if (docs.Count > 1)
                    {
                        await Task.WhenAll(docs.Select(doc => WritePatchBatchWithRetryAsync(
                            [doc], pkValue, patch, upsertMissing, ct)));
                        return;
                    }
                    _logger.LogInformation("Patch NotFound pk={PK}, falling back to upsert", pkValue);
                    await upsertMissing(docs[0].Document, ct);
                    return;
                }

                if (statusCode == 429 || statusCode >= 500)
                {
                    if (attempt >= 5) throw new InvalidOperationException($"Cosmos patch retries exhausted: {status}");
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Batch {Status} pk={PK}, attempt {Attempt}, retrying in {Delay}ms",
                        status, pkValue, attempt, delay.TotalMilliseconds);
                    await Task.Delay(delay, ct);
                    continue;
                }

                _logger.LogError("Batch failed (non-retryable): pk={PK}, status={Status}", pkValue, status);
                throw new Exception($"Batch patch failed: {status}");
            }
            catch (CosmosException ex) when (
                attempt < 5 && (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                ex.StatusCode == System.Net.HttpStatusCode.ServiceUnavailable ||
                (int)ex.StatusCode >= 500))
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Batch exception {Status} pk={PK}, attempt {Attempt}, retrying",
                    ex.StatusCode, pkValue, attempt);
                await Task.Delay(delay, ct);
            }
            catch (OperationCanceledException) { throw; }
        }
    }

    internal sealed record DocumentPatch(EmbeddingResult Document, List<PatchOperation[]> Operations);

    internal static List<List<DocumentPatch>> BuildPatchBatches(
        IEnumerable<EmbeddingResult> docs, string pkField, string vectorField, string now)
    {
        var batches = new List<List<DocumentPatch>>();
        var batch = new List<DocumentPatch>();
        var operationCount = 0;
        foreach (var doc in docs)
        {
            var operations = BuildDocumentFields(doc, pkField, vectorField, now, false)
                .Select(field => PatchOperation.Set(
                    "/" + field.Key.Replace("~", "~0").Replace("/", "~1"), field.Value))
                .Chunk(10).ToList();
            // All patches for an item must commit together; never split an item
            // across transactions, even at the 100-operation batch boundary.
            if (operations.Count > 100)
                throw new InvalidOperationException($"Document '{doc.DocId}' exceeds the 1000-field atomic patch limit");
            if (operationCount + operations.Count > 100)
            {
                batches.Add(batch);
                batch = new();
                operationCount = 0;
            }
            batch.Add(new(doc, operations));
            operationCount += operations.Count;
        }
        if (batch.Count > 0)
            batches.Add(batch);
        return batches;
    }

    internal static Dictionary<string, object> BuildDocumentFields(
        EmbeddingResult doc, string pkField, string vectorField, string now, bool forCreate)
    {
        var item = new Dictionary<string, object>
        {
            [vectorField] = doc.Embedding.ToList(),
            ["embedded_at"] = now,
            ["pipeline_id"] = doc.PipelineId,
            ["content_hash"] = doc.ContentHash,
        };
        if (forCreate && doc.ShouldIncludeMetadata("source_ref"))
            item["source_ref"] = doc.SourceRef;
        if (doc.ShouldIncludeMetadata("embedding_dims"))
            item["embedding_dims"] = doc.Embedding.Length;
        if (doc.ShouldIncludeMetadata("pipeline_name"))
            item["pipeline_name"] = doc.PipelineName;
        if (forCreate && !string.IsNullOrEmpty(doc.SourceId))
            item["source_id"] = doc.SourceId;
        if (doc.StoreContent == true && !string.IsNullOrEmpty(doc.Content))
            item[doc.ContentField ?? "content"] = doc.Content;
        // Source fields are independent of processed-content opt-in. Preserve
        // create's precedence when a source field also names the content field.
        if (doc.SourceContentFields != null)
            foreach (var (field, value) in doc.SourceContentFields)
                item[field] = value;
        if (!string.IsNullOrEmpty(doc.PipelineGeneration))
            item["pipeline_generation"] = doc.PipelineGeneration;
        if (doc.ChunkIndex is not null)
        {
            item["source_ref"] = doc.SourceRef;
            item["source_id"] = doc.SourceId;
            item["chunk_index"] = doc.ChunkIndex.Value;
            item["chunk_count"] = doc.ChunkCount!.Value;
            item["chunk_source_partition"] = doc.PartitionKeyValue;
        }
        item.Remove("id");
        if (!string.IsNullOrEmpty(pkField))
            item.Remove(pkField);
        if (forCreate)
        {
            item["id"] = doc.DocId;
            if (!string.IsNullOrEmpty(pkField) && pkField != "id")
                item[pkField] = doc.PartitionKeyValue;
        }
        return item;
    }

    /// <summary>
    /// Fallback: upsert documents that don't exist yet (separate destination container).
    /// </summary>
    private async Task UpsertBatchWithRetryAsync(
        Container container,
        PartitionKey pk,
        List<EmbeddingResult> docs,
        string pkField,
        string vectorField,
        string now,
        CancellationToken ct)
    {
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                var batch = container.CreateTransactionalBatch(pk);
                foreach (var doc in docs)
                    batch.UpsertItem(BuildDocumentFields(doc, pkField, vectorField, now, true));

                using var response = await batch.ExecuteAsync(ct);
                if (response.IsSuccessStatusCode)
                    return;

                var statusCode = (int)response.StatusCode;
                if (statusCode == 429 || statusCode >= 500)
                {
                    if (attempt >= 5) throw new InvalidOperationException($"Cosmos upsert retries exhausted: {response.StatusCode}");
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Upsert {Status} pk={PK}, attempt {Attempt}, retrying",
                        response.StatusCode, pk, attempt);
                    await Task.Delay(delay, ct);
                    continue;
                }

                throw new Exception($"Batch upsert failed: {response.StatusCode}");
            }
            catch (CosmosException ex) when (
                attempt < 5 && (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                (int)ex.StatusCode >= 500))
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                await Task.Delay(delay, ct);
            }
            catch (OperationCanceledException) { throw; }
        }
    }

    private static CosmosClient GetOrCreateClient(string endpoint)
    {
        return _clients.GetOrAdd(endpoint, ep =>
            new CosmosClient(ep, new DefaultAzureCredential(), new CosmosClientOptions
            {
                ApplicationName = CosmosDataUserAgent,
                ConnectionMode = ConnectionMode.Direct,
                MaxRetryAttemptsOnRateLimitedRequests = 5,
                MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(30),
            }));
    }

    /// <summary>
    /// Delete every destination document matching (source_id, source_ref).
    /// Used by BlobEventConsumer to propagate BlobDeleted events.
    /// Queries by source_id+source_ref so it removes all chunks
    /// (doc_id is "{source_ref}" or "{source_ref}-chunk-N").
    /// </summary>
    public async Task DeleteByRefAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
        CancellationToken ct)
    {
        if (requests.Count == 0) return;
        var endpoint = config["endpoint"]?.ToString() ?? "";
        var database = config["database"]?.ToString() ?? "";
        var containerName = config["container"]?.ToString() ?? "";
        var client = GetOrCreateClient(endpoint);
        var container = client.GetDatabase(database).GetContainer(containerName);
        var pkPath = await GetPartitionKeyPathAsync(container, $"{endpoint}/{database}/{containerName}", ct);

        foreach (var req in requests)
        {
            try
            {
                var query = new QueryDefinition(
                    "SELECT c.id FROM c WHERE c.source_id = @sid AND c.source_ref = @ref AND c.pipeline_id = @pid"
                    + (req.KeepIds is null ? "" : " AND c.chunk_source_partition = @partition"))
                    .WithParameter("@sid", req.SourceId)
                    .WithParameter("@ref", req.SourceRef)
                    .WithParameter("@pid", req.PipelineId)
                    .WithParameter("@partition", req.PartitionKeyValue);
                // With /id, each chunk occupies its own logical partition.
                // Keep source and pipeline filters even when the query fans out.
                using var iter = container.GetItemQueryIterator<DeletedIdDoc>(
                    query,
                    requestOptions: new QueryRequestOptions
                    {
                        PartitionKey = pkPath == "/id" ? null : new PartitionKey(req.PartitionKeyValue),
                    });

                var ids = new List<string>();
                while (iter.HasMoreResults)
                {
                    var page = await iter.ReadNextAsync(ct);
                    foreach (var d in page)
                        if (!string.IsNullOrEmpty(d.Id) && req.KeepIds?.Contains(d.Id) != true)
                            ids.Add(d.Id);
                }

                if (ids.Count == 0)
                {
                    _logger.LogInformation(
                        "Delete: no Cosmos docs for source_id={SrcId} source_ref={Ref}",
                        req.SourceId, req.SourceRef);
                    continue;
                }

                foreach (var partition in GroupDeletionIds(ids, pkPath, req.PartitionKeyValue))
                {
                    var partitionIds = partition.ToList();
                    for (int i = 0; i < partitionIds.Count; i += 100)
                    {
                        var batch = container.CreateTransactionalBatch(new PartitionKey(partition.Key));
                        foreach (var id in partitionIds.Skip(i).Take(100)) batch.DeleteItem(id);
                        using var resp = await batch.ExecuteAsync(ct);
                        if (!resp.IsSuccessStatusCode)
                        {
                            _logger.LogWarning(
                                "Cosmos delete batch status={Status} for src={SrcId} ref={Ref}",
                                resp.StatusCode, req.SourceId, req.SourceRef);
                            throw new InvalidOperationException($"Cosmos delete batch failed: {resp.StatusCode}");
                        }
                    }
                }
                _logger.LogInformation(
                    "Deleted {Count} Cosmos doc(s) for source_id={SrcId} source_ref={Ref}",
                    ids.Count, req.SourceId, req.SourceRef);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Cosmos delete failed for src={SrcId} ref={Ref}",
                    req.SourceId, req.SourceRef);
                throw;
            }
        }
    }

    private sealed class DeletedIdDoc
    {
        [Newtonsoft.Json.JsonProperty("id")]
        public string Id { get; set; } = "";
    }
}
