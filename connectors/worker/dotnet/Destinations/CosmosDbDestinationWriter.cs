using System.Collections.Concurrent;
using Azure.Identity;
using Microsoft.Azure.Cosmos;

namespace OmniVec.Worker.Destinations;

public class CosmosDbDestinationWriter : IDestinationWriter
{
    private const string CosmosDataUserAgent = "OmniVec-DataCosmos/1.0";

    private readonly ILogger<CosmosDbDestinationWriter> _logger;
    private static readonly ConcurrentDictionary<string, CosmosClient> _clients = new();
    private static readonly ConcurrentDictionary<string, string> _pkPathCache = new();

    public string DestinationType => "cosmosdb-vector";

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
        if (!_pkPathCache.TryGetValue(cacheKey, out var pkPath))
        {
            var props = await container.ReadContainerAsync(cancellationToken: ct);
            pkPath = props.Resource.PartitionKeyPath;
            _pkPathCache[cacheKey] = pkPath;
        }
        var pkField = pkPath.TrimStart('/');

        // Group by partition key value
        var groups = results.GroupBy(r => r.PartitionKeyValue);
        var tasks = new List<Task>();

        foreach (var group in groups)
        {
            var items = group.ToList();
            // TransactionalBatch max 100 ops
            for (int i = 0; i < items.Count; i += 100)
            {
                var chunk = items.Skip(i).Take(100).ToList();
                tasks.Add(WriteBatchWithRetryAsync(container, group.Key, chunk, pkField, vectorField, ct));
            }
        }

        await Task.WhenAll(tasks);
    }

    private async Task WriteBatchWithRetryAsync(
        Container container,
        string pkValue,
        List<EmbeddingResult> docs,
        string pkField,
        string vectorField,
        CancellationToken ct)
    {
        var pk = new PartitionKey(pkValue);
        var now = DateTime.UtcNow.ToString("O");

        for (int attempt = 1; ; attempt++)
        {
            try
            {
                // Always try patch first — preserves existing document fields.
                // Only fall back to upsert if patch fails with NotFound.
                var batch = container.CreateTransactionalBatch(pk);
                foreach (var doc in docs)
                {
                    var ops = new List<PatchOperation>
                    {
                        PatchOperation.Set($"/{vectorField}", doc.Embedding.ToList()),
                        PatchOperation.Set("/embedded_at", now),
                        PatchOperation.Set("/pipeline_id", doc.PipelineId),
                        PatchOperation.Set("/content_hash", doc.ContentHash),
                    };
                    if (doc.ShouldIncludeMetadata("embedding_dims"))
                        ops.Add(PatchOperation.Set("/embedding_dims", doc.Embedding.Length));
                    if (doc.ShouldIncludeMetadata("pipeline_name"))
                        ops.Add(PatchOperation.Set("/pipeline_name", doc.PipelineName));
                    if (!string.IsNullOrEmpty(doc.PipelineGeneration))
                        ops.Add(PatchOperation.Set("/pipeline_generation", doc.PipelineGeneration));
                    // Persist the (already-truncated) embedded text when the
                    // pipeline opts in. Default for Cosmos is to NOT store it
                    // (preserves prior behavior + avoids the 2 MB doc limit).
                    if (doc.StoreContent == true && !string.IsNullOrEmpty(doc.Content))
                        ops.Add(PatchOperation.Set($"/{doc.ContentField ?? "content"}", doc.Content));
                    batch.PatchItem(doc.DocId, ops);
                }

                using var response = await batch.ExecuteAsync(ct);
                if (response.IsSuccessStatusCode)
                    return;

                var statusCode = (int)response.StatusCode;

                // NotFound = docs don't exist yet → fall back to upsert
                if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
                {
                    _logger.LogInformation("Patch NotFound pk={PK}, falling back to upsert", pkValue);
                    await UpsertBatchWithRetryAsync(container, pk, docs, pkField, vectorField, now, ct);
                    return;
                }

                if (statusCode == 429 || statusCode >= 500)
                {
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Batch {Status} pk={PK}, attempt {Attempt}, retrying in {Delay}ms",
                        response.StatusCode, pkValue, attempt, delay.TotalMilliseconds);
                    await Task.Delay(delay, ct);
                    continue;
                }

                _logger.LogError("Batch failed (non-retryable): pk={PK}, status={Status}", pkValue, response.StatusCode);
                throw new Exception($"Batch patch failed: {response.StatusCode}");
            }
            catch (CosmosException ex) when (
                ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                ex.StatusCode == System.Net.HttpStatusCode.ServiceUnavailable ||
                (int)ex.StatusCode >= 500)
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Batch exception {Status} pk={PK}, attempt {Attempt}, retrying",
                    ex.StatusCode, pkValue, attempt);
                await Task.Delay(delay, ct);
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex)
            {
                var delay = TimeSpan.FromMilliseconds(Math.Min(1000 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Batch error pk={PK}: {Error}, attempt {Attempt}, retrying",
                    pkValue, ex.Message, attempt);
                await Task.Delay(delay, ct);
            }
        }
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
                {
                    var item = new Dictionary<string, object>
                    {
                        ["id"] = doc.DocId,
                        [vectorField] = doc.Embedding.ToList(),
                        ["embedded_at"] = now,
                        ["pipeline_id"] = doc.PipelineId,
                        ["content_hash"] = doc.ContentHash,
                    };
                    if (doc.ShouldIncludeMetadata("source_ref"))
                        item["source_ref"] = doc.SourceRef;
                    if (doc.ShouldIncludeMetadata("embedding_dims"))
                        item["embedding_dims"] = doc.Embedding.Length;
                    if (doc.ShouldIncludeMetadata("pipeline_name"))
                        item["pipeline_name"] = doc.PipelineName;
                    // T-VEC-1: persist source_id so purge-by-source can target rows.
                    if (!string.IsNullOrEmpty(doc.SourceId))
                        item["source_id"] = doc.SourceId;
                    // Opt-in: persist the (already-truncated) embedded text.
                    if (doc.StoreContent == true && !string.IsNullOrEmpty(doc.Content))
                        item[doc.ContentField ?? "content"] = doc.Content;
                    // Copy source content fields with their original names (e.g. "summary", "title")
                    if (doc.SourceContentFields != null)
                    {
                        foreach (var (field, value) in doc.SourceContentFields)
                            item[field] = value;
                    }
                    if (!string.IsNullOrEmpty(pkField))
                        item[pkField] = doc.PartitionKeyValue;
                    if (!string.IsNullOrEmpty(doc.PipelineGeneration))
                        item["pipeline_generation"] = doc.PipelineGeneration;
                    batch.UpsertItem(item);
                }

                using var response = await batch.ExecuteAsync(ct);
                if (response.IsSuccessStatusCode)
                    return;

                var statusCode = (int)response.StatusCode;
                if (statusCode == 429 || statusCode >= 500)
                {
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Upsert {Status} pk={PK}, attempt {Attempt}, retrying",
                        response.StatusCode, pk, attempt);
                    await Task.Delay(delay, ct);
                    continue;
                }

                throw new Exception($"Batch upsert failed: {response.StatusCode}");
            }
            catch (CosmosException ex) when (
                ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests ||
                ex.StatusCode == System.Net.HttpStatusCode.RequestTimeout ||
                (int)ex.StatusCode >= 500)
            {
                var delay = ex.RetryAfter ?? TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                await Task.Delay(delay, ct);
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex)
            {
                var delay = TimeSpan.FromMilliseconds(Math.Min(1000 * Math.Pow(2, attempt), 30_000));
                _logger.LogWarning("Upsert error pk: {Error}, attempt {Attempt}, retrying",
                    ex.Message, attempt);
                await Task.Delay(delay, ct);
            }
        }
    }

    private static CosmosClient GetOrCreateClient(string endpoint)
    {
        return _clients.GetOrAdd(endpoint, ep =>
            new CosmosClient(ep, new DefaultAzureCredential(), new CosmosClientOptions
            {
                ApplicationName = CosmosDataUserAgent,
                ConnectionMode = ConnectionMode.Direct,
                MaxRetryAttemptsOnRateLimitedRequests = int.MaxValue,
                MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(300),
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

        foreach (var req in requests)
        {
            try
            {
                var query = new QueryDefinition(
                    "SELECT c.id FROM c WHERE c.source_id = @sid AND c.source_ref = @ref")
                    .WithParameter("@sid", req.SourceId)
                    .WithParameter("@ref", req.SourceRef);
                var pk = new PartitionKey(req.PartitionKeyValue);
                using var iter = container.GetItemQueryIterator<DeletedIdDoc>(
                    query,
                    requestOptions: new QueryRequestOptions { PartitionKey = pk });

                var ids = new List<string>();
                while (iter.HasMoreResults)
                {
                    var page = await iter.ReadNextAsync(ct);
                    foreach (var d in page) if (!string.IsNullOrEmpty(d.Id)) ids.Add(d.Id);
                }

                if (ids.Count == 0)
                {
                    _logger.LogInformation(
                        "Delete: no Cosmos docs for source_id={SrcId} source_ref={Ref}",
                        req.SourceId, req.SourceRef);
                    continue;
                }

                for (int i = 0; i < ids.Count; i += 100)
                {
                    var chunk = ids.Skip(i).Take(100).ToList();
                    var batch = container.CreateTransactionalBatch(pk);
                    foreach (var id in chunk) batch.DeleteItem(id);
                    using var resp = await batch.ExecuteAsync(ct);
                    if (!resp.IsSuccessStatusCode)
                    {
                        _logger.LogWarning(
                            "Cosmos delete batch status={Status} for src={SrcId} ref={Ref}",
                            resp.StatusCode, req.SourceId, req.SourceRef);
                    }
                }
                _logger.LogInformation(
                    "Deleted {Count} Cosmos doc(s) for source_id={SrcId} source_ref={Ref}",
                    ids.Count, req.SourceId, req.SourceRef);
            }
            catch (CosmosException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                // Already deleted — fine
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
