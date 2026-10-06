using System.Collections.Concurrent;
using System.Net;
using System.Text;
using Azure.Identity;
using Microsoft.Azure.Cosmos;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OmniVec.Worker.Destinations;

public partial class CosmosDbDestinationWriter : IDestinationWriter
{
    private const string CosmosDataUserAgent = "OmniVec-DataCosmos/1.0";
    private const int MaxUpsertRetryAttempts = 5;
    private const int TransactionPayloadBudgetBytes = 1_500_000;
    private const int MaxDocumentPayloadBytes = 2 * 1024 * 1024;

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
        _ = BuildUpsertBatches(chunks, pkField, vectorField, DateTime.UtcNow.ToString("O"));
        await ReplaceTextChunksInStoreAsync(chunks,
            items => WriteBatchAsync(config, items, ct),
            keep => DeleteByRefAsync(config, [source with { KeepIds = keep }], ct),
            source.ChunkCleanupOrder);
    }

    internal static async Task ReplaceTextChunksInStoreAsync(List<EmbeddingResult> chunks,
        Func<List<EmbeddingResult>, Task> write, Func<HashSet<string>, Task> cleanup,
        string cleanupOrder = "insert-first")
    {
        if (cleanupOrder == "delete-first")
        {
            await cleanup(new(StringComparer.Ordinal));
            await write(chunks);
        }
        else if (cleanupOrder == "insert-first")
        {
            await write(chunks);
            await cleanup(chunks.Select(c => c.DocId).ToHashSet(StringComparer.Ordinal));
        }
        else
            throw new ArgumentException("Unknown Cosmos chunk cleanup_order");
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

        var now = DateTime.UtcNow.ToString("O");
        var batches = results.GroupBy(r => DestinationPartitionKey(r, pkField))
            .SelectMany(group => BuildUpsertBatches(group, pkField, vectorField, now)
                .Select(documents => (Partition: group.Key, Documents: documents)))
            .ToList();
        await Task.WhenAll(batches.Select(batch => UpsertBatchWithRetryAsync(
            container, batch.Partition, batch.Documents, ct)));
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

    internal static string DeletionPartitionKey(
        string id, string? storedPartitionKey, string sourcePartition, string pkPath)
        => pkPath == "/id" ? id : storedPartitionKey ?? sourcePartition;

    internal static List<List<JObject>> BuildUpsertBatches(
        IEnumerable<EmbeddingResult> docs, string pkField, string vectorField, string now)
    {
        var batches = new List<List<JObject>>();
        var batch = new List<JObject>();
        var bytes = 0;
        foreach (var doc in docs)
        {
            var item = JObject.FromObject(BuildDocumentFields(doc, pkField, vectorField, now, true));
            var size = Encoding.UTF8.GetByteCount(item.ToString(Formatting.None));
            // Reserve room for Cosmos's transaction envelope below its 2 MiB limit.
            if (size > MaxDocumentPayloadBytes)
                throw new InvalidOperationException($"Document '{doc.DocId}' exceeds the Cosmos 2 MiB item limit");
            if (batch.Count > 0 && (batch.Count == 100 || bytes + size > TransactionPayloadBudgetBytes))
            {
                batches.Add(batch);
                batch = new();
                bytes = 0;
            }
            batch.Add(item);
            bytes += size;
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

    private Task UpsertBatchWithRetryAsync(
        Container container, string pkValue, List<JObject> docs, CancellationToken ct)
        => WriteUpsertBatchWithRetryAsync(docs, pkValue, async (items, token) =>
        {
            if (items.Count == 1
                && Encoding.UTF8.GetByteCount(items[0].ToString(Formatting.None)) > TransactionPayloadBudgetBytes)
            {
                var itemResponse = await container.UpsertItemAsync(items[0], new PartitionKey(pkValue),
                    cancellationToken: token);
                return (itemResponse.StatusCode, null);
            }
            var batch = container.CreateTransactionalBatch(new PartitionKey(pkValue));
            foreach (var item in items) batch.UpsertItem(item);
            using var response = await batch.ExecuteAsync(token);
            return (response.StatusCode, response.RetryAfter);
        }, ct);

    internal async Task WriteUpsertBatchWithRetryAsync(
        List<JObject> docs, string pkValue,
        Func<List<JObject>, CancellationToken, Task<(HttpStatusCode Status, TimeSpan? RetryAfter)>> execute,
        CancellationToken ct)
    {
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                var (status, retryAfter) = await execute(docs, ct);
                var statusCode = (int)status;
                if (statusCode is >= 200 and <= 299)
                    return;

                if (statusCode is 429 or 408 || statusCode >= 500)
                {
                    if (attempt >= MaxUpsertRetryAttempts)
                        throw new InvalidOperationException($"Cosmos upsert retries exhausted: {status}");
                    var delay = retryAfter is { } retry && retry > TimeSpan.Zero
                        ? retry : TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Upsert {Status} pk={PK}, attempt {Attempt}, retrying",
                        status, pkValue, attempt);
                    await Task.Delay(delay, ct);
                    continue;
                }

                _logger.LogError("Cosmos upsert failed: pk={PK}, status={Status}", pkValue, status);
                throw new InvalidOperationException($"Batch upsert failed: {status}");
            }
            catch (CosmosException ex) when (
                attempt < MaxUpsertRetryAttempts && (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                (int)ex.StatusCode >= 500))
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning(ex, "Cosmos upsert exception: pk={PK}, attempt {Attempt}", pkValue, attempt);
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
                var pkField = pkPath.TrimStart('/');
                var query = new QueryDefinition(
                    $"SELECT c.id, c[\"{pkField}\"] AS partition_key FROM c WHERE c.source_id = @sid"
                    + " AND (c.source_ref = @ref OR c.id = @document_id) AND c.pipeline_id = @pid"
                    + (req.KeepIds is null ? "" : " AND c.chunk_source_partition = @partition"))
                    .WithParameter("@sid", req.SourceId)
                    .WithParameter("@ref", req.SourceRef)
                    .WithParameter("@document_id", req.DocumentId ?? "")
                    .WithParameter("@pid", req.PipelineId)
                    .WithParameter("@partition", req.PartitionKeyValue);
                using var iter = container.GetItemQueryIterator<DeletedIdDoc>(
                    query,
                    requestOptions: new QueryRequestOptions
                    {
                        PartitionKey = null,
                    });

                var docs = new List<DeletedIdDoc>();
                while (iter.HasMoreResults)
                {
                    var page = await iter.ReadNextAsync(ct);
                    foreach (var d in page)
                        if (!string.IsNullOrEmpty(d.Id) && req.KeepIds?.Contains(d.Id) != true)
                            docs.Add(d);
                }

                if (docs.Count == 0)
                {
                    _logger.LogInformation(
                        "Delete: no Cosmos docs for source_id={SrcId} source_ref={Ref}",
                        req.SourceId, req.SourceRef);
                    continue;
                }

                foreach (var partition in docs.GroupBy(d =>
                    DeletionPartitionKey(d.Id, d.PartitionKey, req.PartitionKeyValue, pkPath)))
                {
                    var partitionIds = partition.Select(d => d.Id).ToList();
                    for (int i = 0; i < partitionIds.Count; i += 100)
                    {
                        var ids = partitionIds.Skip(i).Take(100).ToList();
                        for (var attempt = 0; ; attempt++)
                        {
                            var batch = container.CreateTransactionalBatch(new PartitionKey(partition.Key));
                            foreach (var id in ids) batch.DeleteItem(id);
                            using var resp = await batch.ExecuteAsync(ct);
                            if (resp.IsSuccessStatusCode) break;

                            var retryable = resp.StatusCode == System.Net.HttpStatusCode.TooManyRequests
                                || resp.StatusCode == System.Net.HttpStatusCode.RequestTimeout
                                || (int)resp.StatusCode >= 500;
                            if (!retryable || attempt >= 7)
                            {
                                _logger.LogWarning(
                                    "Cosmos delete batch status={Status} for src={SrcId} ref={Ref}",
                                    resp.StatusCode, req.SourceId, req.SourceRef);
                                throw new InvalidOperationException($"Cosmos delete batch failed: {resp.StatusCode}");
                            }

                            var retryAfter = resp.RetryAfter;
                            var delay = retryAfter.HasValue && retryAfter.Value > TimeSpan.Zero
                                ? retryAfter.Value
                                : TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                            _logger.LogInformation(
                                "Cosmos delete batch throttled for src={SrcId} ref={Ref}; retry {Attempt} in {DelayMs}ms",
                                req.SourceId, req.SourceRef, attempt + 1, delay.TotalMilliseconds);
                            await Task.Delay(delay, ct);
                        }
                    }
                }
                _logger.LogInformation(
                    "Deleted {Count} Cosmos doc(s) for source_id={SrcId} source_ref={Ref}",
                    docs.Count, req.SourceId, req.SourceRef);
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

        [Newtonsoft.Json.JsonProperty("partition_key")]
        public string? PartitionKey { get; set; }
    }
}
