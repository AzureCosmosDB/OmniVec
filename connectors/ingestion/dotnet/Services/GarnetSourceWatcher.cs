using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;
using OmniVec.Worker.Destinations;
using StackExchange.Redis;

namespace OmniVec.ChangeFeed.Services;

public sealed record GarnetSourceRecord(
    string Id,
    long Version,
    string Content,
    bool Deleted,
    Dictionary<string, string> RawFields,
    string ContentHash);

public static class GarnetSourceRecordParser
{
    public static GarnetSourceRecord Parse(string sourceRef, string json)
    {
        if (string.IsNullOrWhiteSpace(sourceRef))
            throw new ArgumentException("Garnet HASH fields must be non-empty source references", nameof(sourceRef));

        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("Garnet HASH values must be JSON objects");
        if (!root.TryGetProperty("id", out var idValue)
            || idValue.ValueKind is not (JsonValueKind.String or JsonValueKind.Number)
            || string.IsNullOrWhiteSpace(idValue.ToString()))
            throw new InvalidDataException("Garnet HASH records require a non-empty id");
        if (!root.TryGetProperty("version", out var versionValue)
            || versionValue.ValueKind != JsonValueKind.Number
            || !versionValue.TryGetInt64(out var version)
            || version <= 0)
            throw new InvalidDataException("Garnet HASH records require a positive Int64 version");
        if (!root.TryGetProperty("content", out var contentValue)
            || contentValue.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("Garnet HASH records require string content");

        var deleted = false;
        if (root.TryGetProperty("deleted", out var deletedValue))
        {
            if (deletedValue.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new InvalidDataException("Garnet HASH deleted must be a boolean");
            deleted = deletedValue.GetBoolean();
        }

        var fields = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var property in root.EnumerateObject())
            fields[property.Name] = property.Value.ValueKind == JsonValueKind.String
                ? property.Value.GetString() ?? ""
                : property.Value.GetRawText();

        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(root.GetRawText())))
            .ToLowerInvariant();
        return new GarnetSourceRecord(idValue.ToString(), version, contentValue.GetString()!, deleted, fields, hash);
    }
}

/// <summary>Polls a Garnet HASH and writes inline embeddings to a separate Garnet vector set.</summary>
public sealed class GarnetSourceWatcher : ISourceWatcher
{
    public const string ReservedCheckpointPrefix = "__omnivec:source-checkpoints:";

    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ContentHasher _hasher;
    private readonly BlobLeaseManager _leaseManager;
    private readonly ILogger<GarnetSourceWatcher> _logger;
    private readonly HttpClient _docGrokClient;
    private readonly IDestinationWriter _writer;
    private readonly object _pipelineLock = new();
    private readonly object _destinationLock = new();
    private List<Pipeline> _pipelines = new();
    private List<Destination> _destinations = new();
    private CancellationTokenSource? _cts;
    private Task? _pollTask;
    private IDatabase? _database;

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public GarnetSourceWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        ContentHasher hasher,
        BlobLeaseManager leaseManager,
        ILogger<GarnetSourceWatcher> logger,
        string? generation = null,
        IDestinationWriter? writer = null,
        HttpClient? embeddingClient = null)
    {
        _source = source;
        _options = options;
        _apiClient = apiClient;
        _hasher = hasher;
        _leaseManager = leaseManager;
        _logger = logger;
        Generation = generation ?? "0";
        _writer = writer ?? new GarnetDestinationWriter();
        _ownsEmbeddingClient = embeddingClient is null;
        _docGrokClient = embeddingClient ?? new HttpClient
        {
            BaseAddress = new Uri(options.DocGrokBaseUrl),
            Timeout = TimeSpan.FromMinutes(5),
        };
    }

    private readonly bool _ownsEmbeddingClient;

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock)
            _pipelines = pipelines
                .Where(pipeline => pipeline.Sources.Any(binding => binding.SourceId == _source.Id))
                .ToList();
    }

    public void UpdateDestinations(List<Destination> destinations)
    {
        lock (_destinationLock)
            _destinations = destinations.ToList();
    }

    public async Task StartAsync(CancellationToken ct)
    {
        if (_pollTask is not null) return;

        var sourceConfig = ToConfig(_source.Config);
        _database = await GarnetDestinationWriter.GetDatabaseAsync(sourceConfig, ct);
        await _database.PingAsync();
        ct.ThrowIfCancellationRequested();
        var keyType = await _database.KeyTypeAsync(_source.GarnetHashKey);
        if (keyType is not (RedisType.None or RedisType.Hash))
            throw new InvalidOperationException("Configured Garnet source key is not a HASH");

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = PollAsync(_cts.Token);
    }

    private async Task PollAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await PollOnceAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception,
                    "Garnet source poll failed for source {SourceId}; uncheckpointed records remain eligible for retry",
                    _source.Id);
            }

            await Task.Delay(TimeSpan.FromSeconds(_source.GarnetPollIntervalSeconds), ct);
        }
    }

    internal async Task PollOnceAsync(CancellationToken ct, IDatabase? sourceDatabase = null)
    {
        var database = sourceDatabase ?? _database ?? throw new InvalidOperationException("Garnet watcher has not connected");
        List<Pipeline> pipelines;
        List<Destination> destinations;
        lock (_pipelineLock) pipelines = _pipelines.ToList();
        lock (_destinationLock) destinations = _destinations.ToList();
        var validPipelines = pipelines.Select(pipeline =>
            (Pipeline: pipeline, Destination: ValidatePipeline(pipeline, destinations))).ToList();
        var pageSize = _source.GarnetScanPageSize;
        var cursor = "0";

        do
        {
            ct.ThrowIfCancellationRequested();
            var readClock = Stopwatch.StartNew();
            var response = (RedisResult[]?)await database.ExecuteAsync(
                "HSCAN", _source.GarnetHashKey!, cursor, "COUNT", pageSize)
                ?? throw new InvalidDataException("Garnet HSCAN returned no response");
            if (response.Length != 2)
                throw new InvalidDataException("Garnet HSCAN returned an invalid response");
            cursor = response[0].ToString() ?? "0";
            var values = (RedisResult[]?)response[1] ?? Array.Empty<RedisResult>();
            var records = ParsePage(values);
            readClock.Stop();
            var pageReadMs = readClock.ElapsedMilliseconds;

            foreach (var (pipeline, destination) in validPipelines)
            {
                await Parallel.ForEachAsync(
                    records.Chunk(_source.GarnetBatchSize).Select((batch, index) => (Batch: batch, Index: index)),
                    new ParallelOptions
                    {
                        MaxDegreeOfParallelism = Math.Clamp(pipeline.ResourcePolicy.MaxConcurrencyPerWorker, 1, 64),
                        CancellationToken = ct,
                    },
                    async (item, token) =>
                {
                    var batch = item.Batch;
                    token.ThrowIfCancellationRequested();
                    var stage = new StageTimes();
                    if (item.Index == 0)
                        stage.ReadMs += pageReadMs;
                    try
                    {
                        var eligible = new List<(string SourceRef, GarnetSourceRecord Record)>();
                        foreach (var entry in batch)
                        {
                            var checkpointClock = Stopwatch.StartNew();
                            var checkpoint = await ReadCheckpointAsync(database, pipeline, entry.SourceRef);
                            checkpointClock.Stop();
                            stage.ReadMs += checkpointClock.ElapsedMilliseconds;
                            if (checkpoint is not null)
                            {
                                if (entry.Record.Version < checkpoint.SourceVersion)
                                    continue;
                                if (entry.Record.Version == checkpoint.SourceVersion
                                    && entry.Record.ContentHash != checkpoint.ContentHash)
                                    throw new InvalidDataException(
                                        $"Garnet source reference '{entry.SourceRef}' changed content without increasing version");
                                if (entry.Record.Version == checkpoint.SourceVersion
                                    && checkpoint.PipelineRevision >= GetPipelineRevision(pipeline))
                                    continue;
                            }
                            eligible.Add(entry);
                        }

                        if (eligible.Count == 0) return;
                        await ProcessBatchAsync(database, pipeline, destination, eligible, stage, token);
                    }
                    catch (Exception exception) when (exception is not OperationCanceledException || !ct.IsCancellationRequested)
                    {
                        stage.MetricsMs = await ReportMetricsAsync(
                            pipeline,
                            0,
                            1,
                            stage,
                            $"failed:{SourceId}:{pipeline.Id}:{GetPipelineRevision(pipeline)}:{Guid.NewGuid():N}",
                            ct);
                        _logger.LogError(exception,
                            "Garnet inline batch failed for pipeline {PipelineId}, source {SourceId}; source checkpoints were not advanced for failed writes",
                            pipeline.Id, SourceId);
                    }
                });
            }
        } while (cursor != "0");
    }

    private List<(string SourceRef, GarnetSourceRecord Record)> ParsePage(RedisResult[] values)
    {
        var page = new Dictionary<string, GarnetSourceRecord>(StringComparer.Ordinal);
        var conflictingRefs = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index + 1 < values.Length; index += 2)
        {
            var sourceRef = values[index].ToString();
            if (conflictingRefs.Contains(sourceRef)) continue;
            try
            {
                var record = GarnetSourceRecordParser.Parse(sourceRef, values[index + 1].ToString());
                if (page.TryGetValue(sourceRef, out var existing))
                {
                    if (record.Version == existing.Version && record.ContentHash != existing.ContentHash)
                    {
                        page.Remove(sourceRef);
                        conflictingRefs.Add(sourceRef);
                        throw new InvalidDataException(
                            $"Garnet source reference '{sourceRef}' has conflicting contents at version {record.Version}");
                    }
                    if (record.Version > existing.Version)
                        page[sourceRef] = record;
                }
                else
                {
                    page.Add(sourceRef, record);
                }
            }
            catch (Exception exception) when (exception is JsonException or InvalidDataException or ArgumentException)
            {
                _logger.LogError(exception,
                    "Ignoring invalid Garnet source HASH field {SourceRef}; its contents must be corrected and versioned",
                    sourceRef);
            }
        }

        return page.Select(entry => (entry.Key, entry.Value)).ToList();
    }

    private Destination ValidatePipeline(Pipeline pipeline, List<Destination> destinations)
    {
        if (!string.Equals(pipeline.ProcessingMode, "inline", StringComparison.OrdinalIgnoreCase)
            || !string.Equals(pipeline.ContentStrategy, "truncate", StringComparison.OrdinalIgnoreCase)
            || pipeline.ChunkConfig is not null)
            throw new InvalidOperationException(
                $"Garnet source pipeline '{pipeline.Id}' only supports inline, truncate processing without chunk configuration");

        var binding = pipeline.Sources.SingleOrDefault(item => item.SourceId == _source.Id)
            ?? throw new InvalidOperationException($"Pipeline '{pipeline.Id}' no longer references this source");
        if (!binding.ContentFields.SequenceEqual(["content"], StringComparer.Ordinal)
            || !string.Equals(binding.ContentMode, "field", StringComparison.OrdinalIgnoreCase)
            || binding.ContentTypeField is not null
            || binding.SharePointIdentity is not null)
            throw new InvalidOperationException(
                $"Garnet source pipeline '{pipeline.Id}' only supports the content field and field content mode");

        var destination = destinations.SingleOrDefault(item => item.Id == pipeline.DestinationId)
            ?? throw new InvalidOperationException(
                $"Garnet source pipeline '{pipeline.Id}' has no enabled destination '{pipeline.DestinationId}'");
        if (!string.Equals(destination.Type, "garnet", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Garnet sources can only write to Garnet vector sets");

        var sourceEndpoint = _source.Endpoint ?? "";
        var destinationEndpoint = destination.Config.TryGetValue("endpoint", out var endpoint)
            ? endpoint?.ToString() ?? ""
            : "";
        var sourceKey = _source.GarnetHashKey ?? "";
        var vectorSet = destination.Config.TryGetValue("vector_set", out var vector)
            ? vector?.ToString() ?? "omnivec-vectors"
            : "omnivec-vectors";
        var sourceTls = !_source.Config.TryGetValue("tls", out var sourceTlsValue) || sourceTlsValue.GetBoolean();
        var destinationTls = !destination.Config.TryGetValue("tls", out var destinationTlsValue)
            || bool.Parse(destinationTlsValue.ToString()!);
        if (sourceKey.StartsWith(ReservedCheckpointPrefix, StringComparison.OrdinalIgnoreCase)
            || GarnetEndpoint.Parse(sourceEndpoint, sourceTls) != GarnetEndpoint.Parse(destinationEndpoint, destinationTls))
            throw new InvalidOperationException("Garnet source and destination must use the same endpoint");
        if (string.Equals(sourceKey, vectorSet, StringComparison.OrdinalIgnoreCase)
            || vectorSet.StartsWith(ReservedCheckpointPrefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException(
                "Garnet source HASH, destination vector set, and internal checkpoint keys must be distinct");

        return destination;
    }

    private async Task ProcessBatchAsync(
        IDatabase sourceDatabase,
        Pipeline pipeline,
        Destination destination,
        List<(string SourceRef, GarnetSourceRecord Record)> records,
        StageTimes stage,
        CancellationToken ct)
    {
        var deletes = records
            .Where(entry => entry.Record.Deleted || string.IsNullOrWhiteSpace(entry.Record.Content))
            .ToList();
        var upserts = records.Except(deletes).ToList();
        var destinationConfig = ToConfig(destination.Config);
        var sourceRevision = GetPipelineRevision(pipeline);

        if (upserts.Count > 0)
        {
            var modelClock = Stopwatch.StartNew();
            var vectors = await InlineEmbeddingClient.EmbedAsync(
                _docGrokClient,
                pipeline.DocgrokPipeline,
                upserts.Select(entry => entry.Record.Content).ToList(),
                ct);
            modelClock.Stop();
            stage.ModelMs += modelClock.ElapsedMilliseconds;

            var embeddingResults = upserts.Select((entry, index) => new EmbeddingResult(
                entry.Record.Id,
                entry.SourceRef,
                vectors[index],
                _hasher.ComputeHash(entry.Record.Content),
                entry.SourceRef,
                pipeline.Id,
                pipeline.Name,
                pipeline.Generation,
                entry.Record.Content,
                entry.Record.RawFields,
                SourceId,
                pipeline.StoreContent,
                pipeline.MetadataFields,
                SourceVersion: entry.Record.Version,
                PipelineRevision: sourceRevision,
                ModelName: pipeline.DocgrokPipeline)).ToList();

            var writeClock = Stopwatch.StartNew();
            await _writer.WriteBatchAsync(destinationConfig, embeddingResults, ct);
            foreach (var (entry, embeddingResult) in upserts.Zip(embeddingResults))
                await WriteCheckpointAsync(sourceDatabase, pipeline, entry.SourceRef, entry.Record, sourceRevision);
            writeClock.Stop();
            stage.WriteMs += writeClock.ElapsedMilliseconds;
        }

        if (deletes.Count > 0)
        {
            var writeClock = Stopwatch.StartNew();
            await _writer.DeleteByRefAsync(
                destinationConfig,
                deletes.Select(entry => new DeleteRequest(
                    SourceId,
                    entry.SourceRef,
                    entry.SourceRef,
                    pipeline.Id,
                    SourceVersion: entry.Record.Version,
                    PipelineRevision: sourceRevision)).ToList(),
                ct);
            foreach (var entry in deletes)
                await WriteCheckpointAsync(sourceDatabase, pipeline, entry.SourceRef, entry.Record, sourceRevision);
            writeClock.Stop();
            stage.WriteMs += writeClock.ElapsedMilliseconds;
        }

        stage.MetricsMs = await ReportMetricsAsync(
            pipeline,
            records.Count,
            0,
            stage,
            BuildMetricsBatchKey(pipeline, records),
            ct);
        _logger.LogInformation(
            "Garnet inline stages pipeline={PipelineId} source={SourceId} records={Count} read_ms={ReadMs} model_ms={ModelMs} write_ms={WriteMs} metrics_ms={MetricsMs}",
            pipeline.Id, SourceId, records.Count, stage.ReadMs, stage.ModelMs, stage.WriteMs, stage.MetricsMs);
    }

    private async Task<GarnetCheckpoint?> ReadCheckpointAsync(
        IDatabase database,
        Pipeline pipeline,
        string sourceRef)
    {
        var key = CheckpointKey(pipeline.Id);
        var value = await database.HashGetAsync(key, sourceRef);
        if (!value.HasValue) return null;
        var checkpoint = JsonSerializer.Deserialize<GarnetCheckpoint>(value.ToString())
            ?? throw new InvalidDataException($"Garnet internal checkpoint for pipeline '{pipeline.Id}' is invalid");
        if (checkpoint.SourceVersion <= 0 || checkpoint.PipelineRevision <= 0
            || string.IsNullOrWhiteSpace(checkpoint.ContentHash))
            throw new InvalidDataException($"Garnet internal checkpoint for pipeline '{pipeline.Id}' is invalid");
        return checkpoint;
    }

    private async Task WriteCheckpointAsync(
        IDatabase database,
        Pipeline pipeline,
        string sourceRef,
        GarnetSourceRecord record,
        long pipelineRevision)
    {
        var checkpoint = new GarnetCheckpoint(record.Version, record.ContentHash, pipelineRevision);
        await database.HashSetAsync(
            CheckpointKey(pipeline.Id),
            sourceRef,
            JsonSerializer.Serialize(checkpoint));
    }

    private string CheckpointKey(string pipelineId)
    {
        var identity = Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes($"{SourceId}\u001f{pipelineId}"))).ToLowerInvariant();
        return ReservedCheckpointPrefix + identity;
    }

    private static string BuildMetricsBatchKey(Pipeline pipeline, List<(string SourceRef, GarnetSourceRecord Record)> records)
    {
        var identities = records.Select(entry => $"{entry.SourceRef}:{entry.Record.Version}:{entry.Record.ContentHash}")
            .OrderBy(identity => identity, StringComparer.Ordinal);
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("\n", identities))));
        return $"garnet:{pipeline.Id}:{GetPipelineRevision(pipeline)}:{digest}";
    }

    private async Task<long> ReportMetricsAsync(
        Pipeline pipeline,
        int processed,
        int failed,
        StageTimes stage,
        string batchKey,
        CancellationToken ct)
    {
        var processingMs = stage.ReadMs + stage.ModelMs + stage.WriteMs;
        var metricClock = Stopwatch.StartNew();
        await _apiClient.ReportInlineMetricsAsync(
            pipeline.Id, processed, failed, processingMs, batchKey, ct,
            pipeline.DocgrokPipeline, _source.Id, pipeline.DestinationId);
        metricClock.Stop();
        return metricClock.ElapsedMilliseconds;
    }

    internal static long GetPipelineRevision(Pipeline pipeline)
    {
        var revision = 1L;
        foreach (var value in new[] { pipeline.ResetAt, pipeline.UpdatedAt })
        {
            if (string.IsNullOrWhiteSpace(value)) continue;
            if (!DateTimeOffset.TryParse(
                    value,
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeUniversal,
                    out var timestamp))
                throw new InvalidDataException($"Pipeline '{pipeline.Id}' has an invalid revision timestamp");
            revision = Math.Max(revision, timestamp.UtcDateTime.Ticks);
        }
        return revision;
    }

    private static Dictionary<string, object> ToConfig(Dictionary<string, System.Text.Json.JsonElement> config)
        => config.ToDictionary(entry => entry.Key, entry => (object)entry.Value, StringComparer.Ordinal);

    private static Dictionary<string, object> ToConfig(Dictionary<string, object> config)
        => config.ToDictionary(entry => entry.Key, entry => entry.Value, StringComparer.Ordinal);

    public async ValueTask DisposeAsync()
    {
        if (_cts is not null)
        {
            await _cts.CancelAsync();
            if (_pollTask is not null)
            {
                try { await _pollTask; }
                catch (OperationCanceledException) { }
            }
            _cts.Dispose();
            _cts = null;
        }
        if (_ownsEmbeddingClient) _docGrokClient.Dispose();
        await _leaseManager.ReleaseAsync(SourceId, CancellationToken.None);
    }

    private sealed record GarnetCheckpoint(long SourceVersion, string ContentHash, long PipelineRevision);
    private sealed class StageTimes
    {
        public long ReadMs { get; set; }
        public long ModelMs { get; set; }
        public long WriteMs { get; set; }
        public long MetricsMs { get; set; }
    }
}
