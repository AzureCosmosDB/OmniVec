using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure;
using Azure.Identity;
using Azure.Storage.Files.DataLake;
using Microsoft.Azure.StackExchangeRedis;
using StackExchange.Redis;

namespace OmniVec.Worker.Destinations;

/// <summary>
/// Stages embedding batches in OneLake and starts a Fabric Spark Job Definition.
/// The job owns Iceberg MERGE operations; this writer never writes Iceberg
/// metadata or data files directly.
/// </summary>
public sealed class OneLakeIcebergDestinationWriter : IDestinationWriter
{
    internal const string WriterMarker = "omnivec-onelake-iceberg-v1";
    private static readonly ConcurrentDictionary<string, ConnectionMultiplexer> RedisConnections = new();
    private readonly CosmosDbDestinationWriter _cosmosWriter;
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<OneLakeIcebergDestinationWriter> _logger;

    public string DestinationType => "onelake-iceberg";

    public OneLakeIcebergDestinationWriter(
        IHttpClientFactory httpClientFactory,
        CosmosDbDestinationWriter cosmosWriter,
        ILogger<OneLakeIcebergDestinationWriter> logger)
    {
        _httpClientFactory = httpClientFactory;
        _cosmosWriter = cosmosWriter;
        _logger = logger;
    }

    public async Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        if (results.Count == 0) return;
        // A source row can only hold one configured write-back column set.
        // Keep distinct pipeline runs out of the same Spark MERGE source so a
        // table never receives multiple source records matching one target row.
        foreach (var pipelineResults in results.GroupBy(result => result.PipelineId))
            await WritePipelineBatchAsync(config, pipelineResults.ToList(), ct);
    }

    private async Task WritePipelineBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var workspaceId = Required(config, "workspace_id");
        var lakehouseItemId = Required(config, "lakehouse_item_id");
        var jobDefinitionId = Required(config, "spark_job_definition_item_id");
        var targetTable = Required(config, "target_table");
        var accountUrl = Get(config, "staging_account_url", "https://onelake.dfs.fabric.microsoft.com");
        var fileSystem = Required(config, "staging_file_system");
        var stagingRoot = Get(config, "staging_path", "Files/omnivec/staging").Trim('/');
        var writebackColumnsJson = GetJson(config, "writeback_columns", "{}");
        var runId = BuildRunId(targetTable, results);
        var stagingPath = $"{stagingRoot}/pipeline={SafePath(results[0].PipelineId)}/generation={SafePath(results[0].PipelineGeneration)}/batch={runId}.jsonl";
        var stageUri = BuildAbfsUri(accountUrl, fileSystem, stagingPath);

        await StageAsync(accountUrl, fileSystem, stagingPath, targetTable, runId, results, ct);
        await StartFabricJobAsync(
            config, workspaceId, lakehouseItemId, jobDefinitionId, stageUri, targetTable, runId,
            writebackColumnsJson, ct);
        await MirrorToServingEngineAsync(config, results, ct);
    }

    private async Task StageAsync(
        string accountUrl,
        string fileSystem,
        string stagingPath,
        string targetTable,
        string runId,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var records = results.Select(r => new
        {
            id = r.DocId,
            source_id = r.SourceId,
            source_ref = r.SourceRef,
            content_hash = r.ContentHash,
            pipeline_id = r.PipelineId,
            pipeline_generation = r.PipelineGeneration,
            model = r.ModelName,
            embedding = r.Embedding,
            content = r.StoreContent == false ? null : r.Content,
            source_content_fields = r.SourceContentFields,
            omnivec_writer_marker = WriterMarker,
            omnivec_run_id = runId,
            target_table = targetTable,
        });
        var payload = Encoding.UTF8.GetBytes(string.Join(
            "\n", records.Select(record => JsonSerializer.Serialize(record))) + "\n");

        var service = new DataLakeServiceClient(new Uri(accountUrl), new DefaultAzureCredential());
        var fileSystemClient = service.GetFileSystemClient(fileSystem);
        var directoryPath = stagingPath[..stagingPath.LastIndexOf('/')];
        await fileSystemClient.GetDirectoryClient(directoryPath).CreateIfNotExistsAsync(cancellationToken: ct);
        var file = fileSystemClient.GetFileClient(stagingPath);
        try
        {
            await using var stream = new MemoryStream(payload, writable: false);
            await file.UploadAsync(stream, overwrite: false, cancellationToken: ct);
            _logger.LogInformation("Staged {Count} embeddings at {StagingPath}", results.Count, stagingPath);
        }
        catch (RequestFailedException ex) when (ex.Status == 409)
        {
            // The deterministic run id makes an existing path a retry of the
            // same immutable batch. Reading properties proves it is durable.
            await file.GetPropertiesAsync(cancellationToken: ct);
            _logger.LogInformation("Reusing durable OneLake staging file {StagingPath}", stagingPath);
        }
    }

    private async Task StartFabricJobAsync(
        Dictionary<string, object> config,
        string workspaceId,
        string lakehouseItemId,
        string jobDefinitionId,
        string stagingUri,
        string targetTable,
        string runId,
        string writebackColumnsJson,
        CancellationToken ct)
    {
        var baseUrl = Get(config, "fabric_api_base_url", "https://api.fabric.microsoft.com/v1").TrimEnd('/');
        var endpoint = $"{baseUrl}/workspaces/{Uri.EscapeDataString(workspaceId)}/sparkJobDefinitions/{Uri.EscapeDataString(jobDefinitionId)}/jobs/sparkjob/instances";
        var token = await new DefaultAzureCredential().GetTokenAsync(
            new Azure.Core.TokenRequestContext(new[] { "https://api.fabric.microsoft.com/.default" }), ct);
        var arguments = string.Join(" ",
            "--staging-path", stagingUri,
            "--target-table", targetTable,
            "--run-id", runId,
            "--writer-marker", WriterMarker,
            "--writeback-columns-base64",
            Convert.ToBase64String(Encoding.UTF8.GetBytes(writebackColumnsJson)));
        var executionData = new Dictionary<string, object>
        {
            ["commandLineArguments"] = arguments,
            ["defaultLakehouseId"] = new
            {
                referenceType = "ById",
                workspaceId,
                itemId = lakehouseItemId,
            },
        };
        var executableFile = Get(config, "spark_executable_file", "");
        if (!string.IsNullOrWhiteSpace(executableFile))
            executionData["executableFile"] = executableFile;
        var payload = new Dictionary<string, object> { ["executionData"] = executionData };

        using var request = new HttpRequestMessage(HttpMethod.Post, endpoint)
        {
            Content = JsonContent.Create(payload),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        var client = _httpClientFactory.CreateClient("FabricJobs");
        using var response = await client.SendAsync(request, ct);
        if (response.StatusCode != HttpStatusCode.Accepted)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            throw new InvalidOperationException($"Fabric Spark job was not accepted ({(int)response.StatusCode}): {body}");
        }
        _logger.LogInformation(
            "Fabric Spark job accepted for run {RunId}; staged batch remains durable at {StagingUri}",
            runId, stagingUri);
    }

    private async Task MirrorToServingEngineAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var mirror = GetMirror(config);
        if (mirror is null) return;

        try
        {
            if (string.Equals(mirror.Type, "cosmosdb-vector", StringComparison.OrdinalIgnoreCase))
            {
                await _cosmosWriter.WriteBatchAsync(mirror.Config, results, ct);
                return;
            }
            if (!string.Equals(mirror.Type, "redis", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException($"Unsupported OneLake Iceberg mirror type '{mirror.Type}'");

            await MirrorToRedisAsync(mirror.Config, results, ct);
        }
        catch (Exception ex) when (mirror.BestEffort)
        {
            _logger.LogWarning(ex, "Best-effort {MirrorType} mirror failed; OneLake batch remains committed to staging",
                mirror.Type);
        }
    }

    private async Task MirrorToRedisAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var endpoint = Required(config, "endpoint");
        var options = ConfigurationOptions.Parse(endpoint);
        options.Ssl = GetBool(config, "tls", true);
        options.AbortOnConnectFail = false;
        if (GetBool(config, "use_entra_auth", true))
            await AzureCacheForRedis.ConfigureForAzureWithTokenCredentialAsync(
                options, new DefaultAzureCredential());

        var connection = RedisConnections.GetOrAdd(
            $"{endpoint}|{options.Ssl}|{GetBool(config, "use_entra_auth", true)}",
            _ => ConnectionMultiplexer.Connect(options));
        var database = connection.GetDatabase();
        var prefix = Get(config, "key_prefix", "omnivec").Trim(':');
        var ttl = GetNullableInt(config, "ttl_seconds");
        var writes = results.Select(async result =>
        {
            var key = $"{prefix}:{result.PipelineId}:{result.SourceId}:{result.SourceRef}";
            var value = JsonSerializer.Serialize(new
            {
                embedding = result.Embedding,
                source_id = result.SourceId,
                source_ref = result.SourceRef,
                content_hash = result.ContentHash,
                pipeline_id = result.PipelineId,
                pipeline_generation = result.PipelineGeneration,
                model = result.ModelName,
                omnivec_writer_marker = WriterMarker,
            });
            await database.StringSetAsync(key, value, ttl is null ? null : TimeSpan.FromSeconds(ttl.Value));
        });
        await Task.WhenAll(writes);
    }

    internal static string BuildRunId(string targetTable, IEnumerable<EmbeddingResult> results)
    {
        var material = targetTable + "\n" + string.Join(
            "\n",
            results.OrderBy(r => r.PipelineId).ThenBy(r => r.SourceId).ThenBy(r => r.SourceRef)
                .Select(r => $"{r.PipelineId}\u001f{r.PipelineGeneration}\u001f{r.ModelName}\u001f{r.SourceId}\u001f{r.SourceRef}\u001f{r.ContentHash}"));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material))).ToLowerInvariant();
    }

    internal static string BuildAbfsUri(string accountUrl, string fileSystem, string path)
    {
        var host = new Uri(accountUrl).Host;
        return $"abfss://{fileSystem}@{host}/{path.TrimStart('/')}";
    }

    private static string Required(Dictionary<string, object> config, string key)
    {
        var value = Get(config, key, "");
        return !string.IsNullOrWhiteSpace(value)
            ? value
            : throw new ArgumentException($"OneLake Iceberg destination requires config.{key}");
    }

    private static string Get(Dictionary<string, object> config, string key, string fallback)
        => config.TryGetValue(key, out var value) && value is not null && !string.IsNullOrWhiteSpace(value.ToString())
            ? value.ToString()!
            : fallback;

    private static string GetJson(Dictionary<string, object> config, string key, string fallback)
    {
        if (!config.TryGetValue(key, out var value) || value is null) return fallback;
        return value is JsonElement json ? json.GetRawText() : JsonSerializer.Serialize(value);
    }

    private static MirrorConfig? GetMirror(Dictionary<string, object> config)
    {
        if (!config.TryGetValue("mirror", out var value) || value is null) return null;
        var raw = value is JsonElement json ? json.GetRawText() : JsonSerializer.Serialize(value);
        return JsonSerializer.Deserialize<MirrorConfig>(raw, new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        });
    }

    private static bool GetBool(Dictionary<string, object> config, string key, bool fallback)
        => bool.TryParse(Get(config, key, fallback.ToString()), out var value) ? value : fallback;

    private static int? GetNullableInt(Dictionary<string, object> config, string key)
        => config.TryGetValue(key, out var value) && int.TryParse(value?.ToString(), out var parsed) && parsed > 0
            ? parsed
            : null;

    private static string SafePath(string value)
        => string.Concat(value.Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_'));

    private sealed class MirrorConfig
    {
        public string Type { get; init; } = "";
        public string? DestinationId { get; init; }
        public Dictionary<string, object> Config { get; init; } = new();
        public bool BestEffort { get; init; }
    }
}
