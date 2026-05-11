using System.Collections.Concurrent;
using Azure.Identity;
using Microsoft.Azure.Cosmos;

namespace OmniVec.Worker.Destinations;

public class CosmosDbDestinationWriter : IDestinationWriter
{
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
                        PatchOperation.Set("/embedding_dims", doc.Embedding.Length),
                        PatchOperation.Set("/pipeline_id", doc.PipelineId),
                        PatchOperation.Set("/pipeline_name", doc.PipelineName),
                        PatchOperation.Set("/content_hash", doc.ContentHash),
                    };
                    if (!string.IsNullOrEmpty(doc.PipelineGeneration))
                        ops.Add(PatchOperation.Set("/pipeline_generation", doc.PipelineGeneration));
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
                        ["source_ref"] = doc.SourceRef,
                        [vectorField] = doc.Embedding.ToList(),
                        ["embedded_at"] = now,
                        ["embedding_dims"] = doc.Embedding.Length,
                        ["pipeline_id"] = doc.PipelineId,
                        ["pipeline_name"] = doc.PipelineName,
                        ["content_hash"] = doc.ContentHash,
                    };
                    // T-VEC-1: persist source_id so purge-by-source can target rows.
                    if (!string.IsNullOrEmpty(doc.SourceId))
                        item["source_id"] = doc.SourceId;
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
                ConnectionMode = ConnectionMode.Direct,
                MaxRetryAttemptsOnRateLimitedRequests = int.MaxValue,
                MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(300),
            }));
    }
}
