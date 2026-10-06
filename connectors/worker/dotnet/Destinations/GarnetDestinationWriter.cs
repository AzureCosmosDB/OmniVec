using System.Collections.Concurrent;
using System.Text.Json;
using StackExchange.Redis;

namespace OmniVec.Worker.Destinations;

public sealed class GarnetDestinationWriter : IDestinationWriter
{
    private const string WriterMarker = "omnivec-garnet-v1";
    private static readonly ConcurrentDictionary<string, Lazy<Task<ConnectionMultiplexer>>> Connections = new();

    public string DestinationType => "garnet";

    public async Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        if (results.Count == 0) return;

        var vectorSet = Get(config, "vector_set", "omnivec-vectors");
        var database = await GetDatabaseAsync(config, ct);
        var dimensions = results[0].Embedding.Length;
        if (dimensions == 0 || results.Any(result => result.Embedding.Length != dimensions))
            throw new InvalidOperationException(
                "Garnet destination requires non-empty vectors with consistent dimensions");

        var metric = Get(config, "distance_metric", "COSINE").ToUpperInvariant();
        if (metric is not ("L2" or "COSINE" or "IP" or "XCOSINE_NORMALIZED"))
            throw new ArgumentException($"Unsupported Garnet distance_metric '{metric}'");
        var quantization = Get(config, "quantization", "NOQUANT").ToUpperInvariant();
        if (quantization is not ("NOQUANT" or "Q8" or "BIN"))
            throw new ArgumentException($"Unsupported Garnet quantization '{quantization}'");
        var graphM = GetPositiveInt(config, "m", 16);
        var buildEf = GetPositiveInt(config, "ef", 200);

        foreach (var result in results)
        {
            ct.ThrowIfCancellationRequested();
            var elementId = OneLakeIcebergDestinationWriter.BuildGarnetElementId(
                result.PipelineId, result.SourceId, result.SourceRef);
            var attributes = JsonSerializer.Serialize(new
            {
                id = elementId,
                source_id = result.SourceId,
                source_ref = result.SourceRef,
                content_hash = result.ContentHash,
                pipeline_id = result.PipelineId,
                pipeline_name = result.ShouldIncludeMetadata("pipeline_name")
                    ? result.PipelineName
                    : null,
                pipeline_generation = result.PipelineGeneration,
                source_version = result.SourceVersion,
                pipeline_revision = result.PipelineRevision,
                model = result.ModelName,
                content = result.StoreContent == false ? null : result.Content,
                source_content_fields = result.SourceContentFields,
                omnivec_writer_marker = WriterMarker,
            });
            await OneLakeIcebergDestinationWriter.ApplyGarnetVersionedOperationAsync(
                database,
                vectorSet,
                elementId,
                OneLakeIcebergDestinationWriter.BuildOperationVersion(
                    result.SourceVersion, result.PipelineRevision, result.ContentHash),
                transaction => transaction.ExecuteAsync(
                    "VADD",
                    vectorSet,
                    "FP32",
                    OneLakeIcebergDestinationWriter.ToFloat32Bytes(result.Embedding),
                    elementId,
                    quantization,
                    "SETATTR",
                    attributes,
                    "EF",
                    buildEf,
                    "M",
                    graphM,
                    "XDISTANCE_METRIC",
                    metric),
                ct);
        }
    }

    public async Task DeleteByRefAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
        CancellationToken ct)
    {
        if (requests.Count == 0) return;

        var vectorSet = Get(config, "vector_set", "omnivec-vectors");
        var database = await GetDatabaseAsync(config, ct);
        foreach (var request in requests)
        {
            ct.ThrowIfCancellationRequested();
            var elementId = OneLakeIcebergDestinationWriter.BuildGarnetElementId(
                request.PipelineId, request.SourceId, request.SourceRef);
            await OneLakeIcebergDestinationWriter.ApplyGarnetVersionedOperationAsync(
                database,
                vectorSet,
                elementId,
                OneLakeIcebergDestinationWriter.BuildOperationVersion(
                    request.SourceVersion, request.PipelineRevision, "delete"),
                transaction => transaction.ExecuteAsync("VREM", vectorSet, elementId),
                ct);
        }
    }

    public static async Task<IDatabase> GetDatabaseAsync(
        Dictionary<string, object> config,
        CancellationToken ct)
    {
        var endpoint = Required(config, "endpoint");
        var options = await OneLakeIcebergDestinationWriter.CreateGarnetOptionsAsync(config, ct);
        var connectionKey = string.Join(
            "|",
            endpoint,
            options.Ssl,
            options.User,
            Get(config, "password_secret_ref", ""),
            GetBool(config, "use_entra_auth", false));
        var connection = await Connections.GetOrAdd(
            connectionKey,
            _ => new Lazy<Task<ConnectionMultiplexer>>(
                () => ConnectionMultiplexer.ConnectAsync(options))).Value;
        return connection.GetDatabase();
    }

    private static string Required(Dictionary<string, object> config, string key)
    {
        var value = Get(config, key, "");
        return !string.IsNullOrWhiteSpace(value)
            ? value
            : throw new ArgumentException($"Garnet destination requires config.{key}");
    }

    private static string Get(Dictionary<string, object> config, string key, string fallback)
        => config.TryGetValue(key, out var value)
            && value is not null
            && !string.IsNullOrWhiteSpace(value.ToString())
                ? value.ToString()!
                : fallback;

    private static bool GetBool(Dictionary<string, object> config, string key, bool fallback)
        => bool.TryParse(Get(config, key, fallback.ToString()), out var value) ? value : fallback;

    private static int GetPositiveInt(Dictionary<string, object> config, string key, int fallback)
        => config.TryGetValue(key, out var value)
            && int.TryParse(value?.ToString(), out var parsed)
            && parsed > 0
                ? parsed
                : fallback;
}
