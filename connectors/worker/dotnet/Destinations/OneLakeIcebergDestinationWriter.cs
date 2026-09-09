using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure;
using Azure.Identity;
using Azure.Security.KeyVault.Secrets;
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
    private static readonly ConcurrentDictionary<string, Lazy<Task<ConnectionMultiplexer>>> GarnetConnections = new();
    private static readonly ConcurrentDictionary<string, ConnectionMultiplexer> RedisConnections = new();
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<OneLakeIcebergDestinationWriter> _logger;

    public string DestinationType => "onelake-iceberg";

    public OneLakeIcebergDestinationWriter(
        IHttpClientFactory httpClientFactory,
        ILogger<OneLakeIcebergDestinationWriter> logger)
    {
        _httpClientFactory = httpClientFactory;
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
        var stagingRoot = Get(config, "staging_path", $"{lakehouseItemId}/Files/omnivec/staging").Trim('/');
        var writebackColumnsJson = GetJson(config, "writeback_columns", "{}");
        var runId = BuildRunId(targetTable, results);
        var stagingPath = $"{stagingRoot}/pipeline={SafePath(results[0].PipelineId)}/generation={SafePath(results[0].PipelineGeneration)}/batch={runId}.jsonl";
        var stageUri = BuildAbfsUri(accountUrl, fileSystem, stagingPath);

        await StageAsync(accountUrl, fileSystem, stagingPath, targetTable, runId, results, ct);
        await StartFabricJobAsync(
            config, workspaceId, jobDefinitionId, stageUri, targetTable, runId,
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
            source_sequence_number = r.SourceVersion,
            pipeline_revision = r.PipelineRevision,
            operation_order = BuildOperationOrder(r.SourceVersion, r.PipelineRevision),
            operation_version = BuildOperationVersion(r.SourceVersion, r.PipelineRevision, runId),
            target_table = targetTable,
            is_deleted = false,
        });
        var payload = Encoding.UTF8.GetBytes(string.Join(
            "\n", records.Select(record => JsonSerializer.Serialize(record))) + "\n");
        await StagePayloadAsync(accountUrl, fileSystem, stagingPath, payload, ct);
        _logger.LogInformation("Staged or reused {Count} embeddings at {StagingPath}", results.Count, stagingPath);
    }

    private async Task StartFabricJobAsync(
        Dictionary<string, object> config,
        string workspaceId,
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
            if (string.Equals(mirror.Type, "garnet", StringComparison.OrdinalIgnoreCase))
            {
                await MirrorToGarnetAsync(mirror.Config, results, ct);
                return;
            }
            if (string.Equals(mirror.Type, "redis", StringComparison.OrdinalIgnoreCase))
            {
                await MirrorToRedisAsync(mirror.Config, results, ct);
                return;
            }
            throw new ArgumentException($"Unsupported OneLake Iceberg mirror type '{mirror.Type}'");
        }
        catch (Exception ex) when (mirror.BestEffort)
        {
            _logger.LogWarning(ex, "Best-effort {MirrorType} mirror failed; OneLake batch remains committed to staging",
                mirror.Type);
        }
    }

    private async Task MirrorToGarnetAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        var endpoint = Required(config, "endpoint");
        var vectorSet = Get(config, "vector_set", "omnivec-vectors");
        var options = await CreateGarnetOptionsAsync(config, ct);

        var connectionKey = $"{endpoint}|{options.Ssl}|{options.User}|{Get(config, "password_secret_ref", "")}|{GetBool(config, "use_entra_auth", false)}";
        var connection = await GarnetConnections.GetOrAdd(
            connectionKey,
            _ => new Lazy<Task<ConnectionMultiplexer>>(
                () => ConnectionMultiplexer.ConnectAsync(options))).Value;
        var database = connection.GetDatabase();
        var dimensions = results[0].Embedding.Length;
        if (dimensions == 0 || results.Any(result => result.Embedding.Length != dimensions))
            throw new InvalidOperationException("Garnet mirror requires non-empty vectors with consistent dimensions");

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
            var elementId = BuildGarnetElementId(result.PipelineId, result.SourceId, result.SourceRef);
            var operationVersion = BuildOperationVersion(
                result.SourceVersion, result.PipelineRevision, result.ContentHash);
            var attributes = JsonSerializer.Serialize(new
            {
                id = elementId,
                source_id = result.SourceId,
                source_ref = result.SourceRef,
                content_hash = result.ContentHash,
                pipeline_id = result.PipelineId,
                pipeline_generation = result.PipelineGeneration,
                source_version = result.SourceVersion,
                pipeline_revision = result.PipelineRevision,
                model = result.ModelName,
                content = result.StoreContent == false ? null : result.Content,
                source_content_fields = result.SourceContentFields,
                omnivec_writer_marker = WriterMarker,
            });
            await ApplyGarnetVersionedOperationAsync(
                database,
                vectorSet,
                elementId,
                operationVersion,
                transaction => transaction.ExecuteAsync(
                    "VADD",
                    vectorSet,
                    "FP32",
                    ToFloat32Bytes(result.Embedding),
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

    private async Task MirrorToRedisAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
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
            var key = BuildRedisKey(prefix, result.PipelineId, result.SourceId, result.SourceRef);
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
            await ApplyRedisVersionedOperationAsync(
                database,
                key,
                BuildOperationVersion(
                    result.SourceVersion, result.PipelineRevision, result.ContentHash),
                transaction => transaction.StringSetAsync(
                    key, value, ttl is null ? null : TimeSpan.FromSeconds(ttl.Value)),
                ct);
        });
        await Task.WhenAll(writes);
    }

    public async Task DeleteByRefAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
        CancellationToken ct)
    {
        if (requests.Count == 0) return;
        var workspaceId = Required(config, "workspace_id");
        var lakehouseItemId = Required(config, "lakehouse_item_id");
        var jobDefinitionId = Required(config, "spark_job_definition_item_id");
        var targetTable = Required(config, "target_table");
        var accountUrl = Get(config, "staging_account_url", "https://onelake.dfs.fabric.microsoft.com");
        var fileSystem = Required(config, "staging_file_system");
        var stagingRoot = Get(config, "staging_path", $"{lakehouseItemId}/Files/omnivec/staging").Trim('/');
        var writebackColumnsJson = GetJson(config, "writeback_columns", "{}");

        foreach (var pipelineRequests in requests.GroupBy(request => request.PipelineId))
        {
            var batch = pipelineRequests.ToList();
            var runId = BuildDeleteRunId(targetTable, batch);
            var stagingPath = $"{stagingRoot}/pipeline={SafePath(batch[0].PipelineId)}/deletes/batch={runId}.jsonl";
            var stageUri = BuildAbfsUri(accountUrl, fileSystem, stagingPath);
            var records = batch.Select(request => new
            {
                id = request.SourceRef,
                source_id = request.SourceId,
                source_ref = request.SourceRef,
                content_hash = "",
                pipeline_id = request.PipelineId,
                pipeline_generation = "",
                model = "",
                embedding = Array.Empty<float>(),
                content = (string?)null,
                source_content_fields = new Dictionary<string, string>(),
                omnivec_writer_marker = WriterMarker,
                omnivec_run_id = runId,
                source_sequence_number = request.SourceVersion,
                pipeline_revision = request.PipelineRevision,
                operation_order = BuildOperationOrder(request.SourceVersion, request.PipelineRevision),
                operation_version = BuildOperationVersion(
                    request.SourceVersion, request.PipelineRevision, runId),
                target_table = targetTable,
                is_deleted = true,
            });
            var payload = Encoding.UTF8.GetBytes(string.Join(
                "\n", records.Select(record => JsonSerializer.Serialize(record))) + "\n");
            await StagePayloadAsync(accountUrl, fileSystem, stagingPath, payload, ct);
            await StartFabricJobAsync(
                config, workspaceId, jobDefinitionId, stageUri, targetTable, runId,
                writebackColumnsJson, ct);
        }

        var mirror = GetMirror(config);
        if (mirror is null) return;
        try
        {
            ct.ThrowIfCancellationRequested();
            if (string.Equals(mirror.Type, "garnet", StringComparison.OrdinalIgnoreCase))
            {
                await DeleteFromGarnetAsync(mirror.Config, requests, ct);
            }
            else if (string.Equals(mirror.Type, "redis", StringComparison.OrdinalIgnoreCase))
            {
                await DeleteFromRedisAsync(mirror.Config, requests, ct);
            }
        }
        catch (Exception ex) when (mirror.BestEffort)
        {
            _logger.LogWarning(ex, "Best-effort {MirrorType} delete failed; OneLake delete remains submitted",
                mirror.Type);
        }
    }

    private static async Task DeleteFromGarnetAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
        CancellationToken ct)
    {
        var endpoint = Required(config, "endpoint");
        var vectorSet = Get(config, "vector_set", "omnivec-vectors");
        var options = await CreateGarnetOptionsAsync(config, ct);
        var connectionKey = $"{endpoint}|{options.Ssl}|{options.User}|{Get(config, "password_secret_ref", "")}|{GetBool(config, "use_entra_auth", false)}";
        var connection = await GarnetConnections.GetOrAdd(
            connectionKey,
            _ => new Lazy<Task<ConnectionMultiplexer>>(
                () => ConnectionMultiplexer.ConnectAsync(options))).Value;
        var database = connection.GetDatabase();
        foreach (var request in requests)
        {
            ct.ThrowIfCancellationRequested();
            var elementId = BuildGarnetElementId(request.PipelineId, request.SourceId, request.SourceRef);
            await ApplyGarnetVersionedOperationAsync(
                database,
                vectorSet,
                elementId,
                BuildOperationVersion(request.SourceVersion, request.PipelineRevision, "delete"),
                transaction => transaction.ExecuteAsync("VREM", vectorSet, elementId),
                ct);
        }
    }

    private static async Task ApplyGarnetVersionedOperationAsync(
        IDatabase database,
        string vectorSet,
        string elementId,
        string operationVersion,
        Func<ITransaction, Task<RedisResult>> queueOperation,
        CancellationToken ct)
    {
        var versionKey = $"{vectorSet}:omnivec-version:{elementId}";
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var current = await database.StringGetAsync(versionKey);
            if (current.HasValue
                && string.CompareOrdinal(current.ToString(), operationVersion) > 0)
                return;

            var transaction = database.CreateTransaction();
            transaction.AddCondition(current.HasValue
                ? Condition.StringEqual(versionKey, current)
                : Condition.KeyNotExists(versionKey));
            var operation = queueOperation(transaction);
            var versionWrite = transaction.StringSetAsync(versionKey, operationVersion);
            if (!await transaction.ExecuteAsync())
                continue;
            await operation;
            await versionWrite;
            return;
        }
    }

    private static async Task DeleteFromRedisAsync(
        Dictionary<string, object> config,
        List<DeleteRequest> requests,
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
        foreach (var request in requests)
        {
            ct.ThrowIfCancellationRequested();
            var key = BuildRedisKey(prefix, request.PipelineId, request.SourceId, request.SourceRef);
            await ApplyRedisVersionedOperationAsync(
                database,
                key,
                BuildOperationVersion(
                    request.SourceVersion, request.PipelineRevision, "delete"),
                transaction => transaction.KeyDeleteAsync(key),
                ct);
        }
    }

    private static async Task ApplyRedisVersionedOperationAsync(
        IDatabase database,
        string key,
        string operationVersion,
        Func<ITransaction, Task<bool>> queueOperation,
        CancellationToken ct)
    {
        var versionKey = $"{key}:omnivec-version";
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var current = await database.StringGetAsync(versionKey);
            if (current.HasValue
                && string.CompareOrdinal(current.ToString(), operationVersion) > 0)
                return;
            var transaction = database.CreateTransaction();
            transaction.AddCondition(current.HasValue
                ? Condition.StringEqual(versionKey, current)
                : Condition.KeyNotExists(versionKey));
            var operation = queueOperation(transaction);
            var versionWrite = transaction.StringSetAsync(versionKey, operationVersion);
            if (!await transaction.ExecuteAsync())
                continue;
            await operation;
            await versionWrite;
            return;
        }
    }

    internal static string BuildRunId(string targetTable, IEnumerable<EmbeddingResult> results)
    {
        var material = targetTable + "\n" + string.Join(
            "\n",
            results.OrderBy(r => r.PipelineId).ThenBy(r => r.SourceId).ThenBy(r => r.SourceRef)
                .Select(r => $"{r.PipelineId}\u001f{r.PipelineGeneration}\u001f{r.ModelName}\u001f{r.SourceId}\u001f{r.SourceRef}\u001f{r.ContentHash}\u001f{r.SourceVersion}\u001f{r.PipelineRevision}"));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material))).ToLowerInvariant();
    }

    internal static string BuildDeleteRunId(string targetTable, IEnumerable<DeleteRequest> requests)
    {
        var material = targetTable + "\ndelete\n" + string.Join(
            "\n",
            requests.OrderBy(r => r.PipelineId).ThenBy(r => r.SourceId).ThenBy(r => r.SourceRef)
                .Select(r => $"{r.PipelineId}\u001f{r.SourceId}\u001f{r.SourceRef}\u001f{r.SourceVersion}\u001f{r.PipelineRevision}"));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material))).ToLowerInvariant();
    }

    internal static string BuildAbfsUri(string accountUrl, string fileSystem, string path)
    {
        var host = new Uri(accountUrl).Host;
        return $"abfss://{fileSystem}@{host}/{path.TrimStart('/')}";
    }

    internal static string BuildGarnetElementId(string pipelineId, string sourceId, string sourceRef)
    {
        var material = $"{pipelineId}\u001f{sourceId}\u001f{sourceRef}";
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material))).ToLowerInvariant();
    }

    internal static byte[] ToFloat32Bytes(float[] vector)
    {
        var bytes = new byte[vector.Length * sizeof(float)];
        Buffer.BlockCopy(vector, 0, bytes, 0, bytes.Length);
        return bytes;
    }

    internal static string BuildOperationOrder(long sourceVersion, long pipelineRevision)
        => $"{sourceVersion:D20}:{pipelineRevision:D20}";

    internal static string BuildOperationVersion(
        long sourceVersion,
        long pipelineRevision,
        string operationId)
        => $"{BuildOperationOrder(sourceVersion, pipelineRevision)}:{operationId}";

    private static string BuildRedisKey(string prefix, string pipelineId, string sourceId, string sourceRef)
        => $"{prefix}:{pipelineId}:{sourceId}:{sourceRef}";

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

    private static int GetPositiveInt(Dictionary<string, object> config, string key, int fallback)
        => config.TryGetValue(key, out var value)
            && int.TryParse(value?.ToString(), out var parsed)
            && parsed > 0
                ? parsed
                : fallback;

    private static int? GetNullableInt(Dictionary<string, object> config, string key)
        => config.TryGetValue(key, out var value) && int.TryParse(value?.ToString(), out var parsed) && parsed > 0
            ? parsed
            : null;

    private static async Task<ConfigurationOptions> CreateGarnetOptionsAsync(
        Dictionary<string, object> config,
        CancellationToken ct)
    {
        var options = ConfigurationOptions.Parse(Required(config, "endpoint"));
        options.Ssl = GetBool(config, "tls", true);
        options.AbortOnConnectFail = false;
        options.Protocol = RedisProtocol.Resp2;
        if (GetBool(config, "use_entra_auth", false))
        {
            await AzureCacheForRedis.ConfigureForAzureWithTokenCredentialAsync(
                options, new DefaultAzureCredential());
            options.Protocol = RedisProtocol.Resp2;
            return options;
        }

        var username = Get(config, "username", "");
        if (!string.IsNullOrWhiteSpace(username))
            options.User = username;
        var secretRef = Get(config, "password_secret_ref", "");
        if (!string.IsNullOrWhiteSpace(secretRef))
            options.Password = await ResolveKeyVaultSecretAsync(secretRef, ct);
        return options;
    }

    private static async Task<string> ResolveKeyVaultSecretAsync(string reference, CancellationToken ct)
    {
        if (!reference.StartsWith("kv://", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Garnet password_secret_ref must use kv://<vault>/<secret>");
        var parts = reference[5..].Split('/', 2, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length != 2)
            throw new ArgumentException("Garnet password_secret_ref must use kv://<vault>/<secret>");
        var vaultHost = parts[0].Contains('.') ? parts[0] : $"{parts[0]}.vault.azure.net";
        var client = new SecretClient(new Uri($"https://{vaultHost}"), new DefaultAzureCredential());
        return (await client.GetSecretAsync(parts[1], cancellationToken: ct)).Value.Value;
    }

    private static string SafePath(string value)
        => string.Concat(value.Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_'));

    private static async Task StagePayloadAsync(
        string accountUrl,
        string fileSystem,
        string stagingPath,
        byte[] payload,
        CancellationToken ct)
    {
        var service = new DataLakeServiceClient(new Uri(accountUrl), new DefaultAzureCredential());
        var fileSystemClient = service.GetFileSystemClient(fileSystem);
        var directoryPath = stagingPath[..stagingPath.LastIndexOf('/')];
        await fileSystemClient.GetDirectoryClient(directoryPath).CreateIfNotExistsAsync(cancellationToken: ct);
        var file = fileSystemClient.GetFileClient(stagingPath);
        try
        {
            await using var stream = new MemoryStream(payload, writable: false);
            await file.UploadAsync(stream, overwrite: false, cancellationToken: ct);
        }
        catch (RequestFailedException ex) when (ex.Status == 409)
        {
            await file.GetPropertiesAsync(cancellationToken: ct);
        }
    }

    private sealed class MirrorConfig
    {
        public string Type { get; init; } = "";
        public string? DestinationId { get; init; }
        public Dictionary<string, object> Config { get; init; } = new();
        public bool BestEffort { get; init; }
    }
}
