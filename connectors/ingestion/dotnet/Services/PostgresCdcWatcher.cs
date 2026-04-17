using System.Diagnostics;
using System.Net.Http.Json;
using Npgsql;
using NpgsqlTypes;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Watches a PostgreSQL table for changes using logical replication (pgoutput plugin).
/// Falls back to poll-based CDC using a tracked timestamp column if replication isn't available.
/// Requires wal_level=logical on Azure PostgreSQL Flexible Server.
/// </summary>
public class PostgresCdcWatcher : ISourceWatcher
{
    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ContentHasher _hasher;
    private readonly ILogger<PostgresCdcWatcher> _logger;
    private readonly HttpClient _docGrokClient;
    private readonly ServiceBusPublisher? _sbPublisher;

    private CancellationTokenSource? _cts;
    private Task? _pollTask;

    private List<Pipeline> _activePipelines = new();
    private readonly object _pipelineLock = new();
    private List<Destination> _destinations = new();

    private DateTime _lastCheckpoint = DateTime.SpecifyKind(DateTime.MinValue, DateTimeKind.Utc);

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public PostgresCdcWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        ContentHasher hasher,
        ILogger<PostgresCdcWatcher> logger,
        string? generation = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _apiClient = apiClient;
        _hasher = hasher;
        _logger = logger;
        _sbPublisher = sbPublisher;
        _docGrokClient = new HttpClient { BaseAddress = new Uri(options.DocGrokBaseUrl), Timeout = TimeSpan.FromSeconds(120) };
        Generation = generation ?? "0";
    }

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock) { _activePipelines = new List<Pipeline>(pipelines); }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public async Task StartAsync(CancellationToken ct)
    {
        // Verify connection works
        await using var conn = new NpgsqlConnection(_source.ConnectionString);
        await conn.OpenAsync(ct);

        var table = _source.Table!;
        var schema = _source.SchemaName ?? "public";

        // Check if table has a modified_at/updated_at column for poll-based CDC
        // (Logical replication is complex — poll-based is robust for embedding pipelines)
        var trackCol = await DetectTrackingColumnAsync(conn, schema, table, ct);
        _logger.LogInformation("PostgreSQL CDC for {Schema}.{Table}: tracking column = {Col}",
            schema, table, trackCol ?? "NONE (full scan each poll)");

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = PollLoopAsync(trackCol, _cts.Token);

        _logger.LogInformation("Started PostgreSQL CDC watcher for {Source} ({Schema}.{Table}) gen={Gen}",
            _source.Name, schema, table, Generation);
    }

    private async Task<string?> DetectTrackingColumnAsync(
        NpgsqlConnection conn, string schema, string table, CancellationToken ct)
    {
        // Look for common tracking columns
        var candidates = new[] { "updated_at", "modified_at", "last_modified", "_ts" };
        await using var cmd = new NpgsqlCommand(@"
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = @schema AND table_name = @table
            AND column_name = ANY(@candidates)
            LIMIT 1", conn);
        cmd.Parameters.AddWithValue("@schema", schema);
        cmd.Parameters.AddWithValue("@table", table);
        cmd.Parameters.AddWithValue("@candidates", candidates);
        var result = await cmd.ExecuteScalarAsync(ct);
        return result?.ToString();
    }

    private string? _lastPkValue;

    private async Task PollLoopAsync(string? trackingColumn, CancellationToken ct)
    {
        var schema = _source.SchemaName ?? "public";
        var table = _source.Table!;
        var pk = _source.PrimaryKey ?? "id";

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await using var conn = new NpgsqlConnection(_source.ConnectionString);
                await conn.OpenAsync(ct);

                List<Dictionary<string, object?>> changes;

                if (trackingColumn is not null && _lastCheckpoint > DateTime.SpecifyKind(DateTime.MinValue, DateTimeKind.Utc))
                {
                    // Incremental: rows changed since last checkpoint
                    // Use >= with PK tiebreaker to handle rows with same timestamp
                    string query;
                    if (_lastPkValue is not null)
                    {
                        query = $@"SELECT * FROM ""{schema}"".""{table}""
                            WHERE (""{trackingColumn}"" > @checkpoint)
                               OR (""{trackingColumn}"" = @checkpoint AND ""{pk}""::text > @lastPk)
                            ORDER BY ""{trackingColumn}"", ""{pk}""
                            LIMIT @limit";
                    }
                    else
                    {
                        query = $@"SELECT * FROM ""{schema}"".""{table}""
                            WHERE ""{trackingColumn}"" > @checkpoint
                            ORDER BY ""{trackingColumn}"", ""{pk}""
                            LIMIT @limit";
                    }

                    await using var cmd = new NpgsqlCommand(query, conn);
                    cmd.Parameters.AddWithValue("@checkpoint", NpgsqlDbType.TimestampTz, _lastCheckpoint);
                    cmd.Parameters.AddWithValue("@limit", _options.MaxItemsPerBatch);
                    if (_lastPkValue is not null)
                        cmd.Parameters.AddWithValue("@lastPk", _lastPkValue);

                    changes = await ReadRowsAsync(cmd, ct);

                    // Update checkpoint + PK cursor
                    if (changes.Count > 0)
                    {
                        var last = changes[^1];
                        if (last.TryGetValue(trackingColumn, out var tv) && tv is DateTime dt && dt >= _lastCheckpoint)
                            _lastCheckpoint = DateTime.SpecifyKind(dt, DateTimeKind.Utc);
                        _lastPkValue = last.TryGetValue(pk, out var pv) ? pv?.ToString() : null;
                    }
                }
                else
                {
                    // Full scan (first run or no tracking column)
                    string query;
                    if (trackingColumn is not null)
                    {
                        query = $@"SELECT * FROM ""{schema}"".""{table}"" ORDER BY ""{trackingColumn}"", ""{pk}"" LIMIT @limit";
                    }
                    else
                    {
                        query = $@"SELECT * FROM ""{schema}"".""{table}"" ORDER BY ""{pk}"" LIMIT @limit";
                    }
                    await using var cmd = new NpgsqlCommand(query, conn);
                    cmd.Parameters.AddWithValue("@limit", _options.MaxItemsPerBatch);
                    changes = await ReadRowsAsync(cmd, ct);

                    // Set checkpoint from results so next poll picks up remaining rows
                    if (trackingColumn is not null && changes.Count > 0)
                    {
                        var last = changes[^1];
                        if (last.TryGetValue(trackingColumn, out var tv) && tv is DateTime dt)
                            _lastCheckpoint = DateTime.SpecifyKind(dt, DateTimeKind.Utc);
                        else
                            _lastCheckpoint = DateTime.UtcNow;
                        _lastPkValue = last.TryGetValue(pk, out var pv) ? pv?.ToString() : null;
                    }
                    else if (trackingColumn is not null)
                    {
                        _lastCheckpoint = DateTime.UtcNow;
                    }
                }

                if (changes.Count > 0)
                {
                    await HandleChangesAsync(changes, pk, ct);
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "PostgreSQL CDC poll error for {Source}, backing off", _source.Name);
                try { await Task.Delay(_options.ErrorBackoffSeconds * 1000, ct); } catch { break; }
            }

            await Task.Delay(_options.FeedPollIntervalSeconds * 1000, ct);
        }
    }

    private static async Task<List<Dictionary<string, object?>>> ReadRowsAsync(
        NpgsqlCommand cmd, CancellationToken ct)
    {
        var rows = new List<Dictionary<string, object?>>();
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            var row = new Dictionary<string, object?>();
            for (int i = 0; i < reader.FieldCount; i++)
            {
                if (reader.IsDBNull(i))
                {
                    row[reader.GetName(i)] = null;
                    continue;
                }
                try
                {
                    row[reader.GetName(i)] = reader.GetValue(i);
                }
                catch
                {
                    // Unsupported types (e.g. pgvector vector) — read as string
                    try { row[reader.GetName(i)] = reader.GetFieldValue<string>(i); }
                    catch { row[reader.GetName(i)] = null; }
                }
            }
            rows.Add(row);
        }
        return rows;
    }

    private async Task HandleChangesAsync(
        List<Dictionary<string, object?>> changes, string pk, CancellationToken ct)
    {
        List<Pipeline> pipelines;
        lock (_pipelineLock) { pipelines = new List<Pipeline>(_activePipelines); }

        var relevantPipelines = pipelines
            .Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id))
            .ToList();
        if (relevantPipelines.Count == 0) return;

        var inlinePipelines = relevantPipelines.Where(p => p.ProcessingMode == "inline").ToList();
        var queuePipelines = relevantPipelines.Where(p => p.ProcessingMode != "inline").ToList();

        // Get content_fields from the first pipeline's source config
        var pipelineSource = relevantPipelines[0].Sources.FirstOrDefault(ps => ps.SourceId == _source.Id);
        var cfFields = pipelineSource?.ContentFields ?? new List<string> { "content" };

        // Filter eligible rows
        var eligible = new List<(string docId, string content, string contentHash, Dictionary<string, object?> row)>();
        int skippedNoContent = 0, skippedUnchanged = 0;

        foreach (var row in changes)
        {
            if (!Source.RowHasContent(row, cfFields)) { skippedNoContent++; continue; }
            var content = Source.ExtractContentFromRow(row, cfFields);
            if (string.IsNullOrEmpty(content)) { skippedNoContent++; continue; }

            var contentHash = _hasher.ComputeHash(content);

            if (!SkipContentHash)
            {
                var existingHash = row.TryGetValue("content_hash", out var h) ? h?.ToString() : null;
                if (contentHash == existingHash)
                {
                    var embeddedAt = row.TryGetValue("embedded_at", out var ea) ? ea?.ToString() : null;
                    bool needsReprocess = string.IsNullOrEmpty(embeddedAt);
                    if (!needsReprocess && DateTime.TryParse(embeddedAt, out var embDt))
                    {
                        foreach (var p in relevantPipelines)
                        {
                            if (!string.IsNullOrEmpty(p.ResetAt) &&
                                DateTime.TryParse(p.ResetAt, out var resetDt) &&
                                resetDt > embDt)
                            {
                                needsReprocess = true; break;
                            }
                        }
                    }
                    if (!needsReprocess) { skippedUnchanged++; continue; }
                }
            }

            var docId = row.TryGetValue(pk, out var pkVal) ? pkVal?.ToString() ?? "" : "";
            eligible.Add((docId, content, contentHash, row));
        }

        _logger.LogInformation(
            "CDC source={SourceId} ({Name}): {Total} changes → {Eligible} eligible, skipped: {NoContent} no-content, {Unchanged} unchanged",
            _source.Id, _source.Name, changes.Count, eligible.Count, skippedNoContent, skippedUnchanged);

        if (eligible.Count == 0) return;

        // Inline: embed + UPDATE source rows with pgvector
        if (inlinePipelines.Count > 0)
            await ProcessInlineAsync(eligible, inlinePipelines, pk, ct);

        // Queue: publish to Service Bus
        if (queuePipelines.Count > 0 && _sbPublisher?.IsEnabled == true)
            await PublishToServiceBusAsync(eligible, queuePipelines, ct);
    }

    private const int EmbedBatchSize = 50;

    private async Task<List<float[]>?> EmbedTextsAsync(string modelId, List<string> texts, CancellationToken ct)
    {
        var allEmbeddings = new List<float[]>();
        for (int offset = 0; offset < texts.Count; offset += EmbedBatchSize)
        {
            var chunk = texts.Skip(offset).Take(EmbedBatchSize).ToList();
            List<float[]>? chunkEmbeddings = null;
            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    var payload = new { model_id = modelId, texts = chunk };
                    var resp = await _docGrokClient.PostAsJsonAsync("/embed/batch", payload, ct);
                    if ((int)resp.StatusCode == 429 || (int)resp.StatusCode >= 500)
                    {
                        var delay = Math.Min(1000 * Math.Pow(2, attempt), 60_000);
                        _logger.LogWarning("DocGrok {Status}, attempt {Attempt}, retrying", resp.StatusCode, attempt);
                        await Task.Delay((int)delay, ct);
                        continue;
                    }
                    resp.EnsureSuccessStatusCode();

                    // Parse response manually — outputs is an array of embeddings
                    // Each element can be either [floats] or [[floats]] depending on model
                    var json = await resp.Content.ReadAsStringAsync(ct);
                    using var doc = System.Text.Json.JsonDocument.Parse(json);
                    var outputs = doc.RootElement.GetProperty("outputs");
                    chunkEmbeddings = new List<float[]>();
                    foreach (var item in outputs.EnumerateArray())
                    {
                        // Handle nested array: outputs[i] might be [[f,f,...]] or [f,f,...]
                        var target = item;
                        if (target.GetArrayLength() > 0 && target[0].ValueKind == System.Text.Json.JsonValueKind.Array)
                            target = target[0];
                        var vec = new float[target.GetArrayLength()];
                        int idx = 0;
                        foreach (var f in target.EnumerateArray())
                            vec[idx++] = f.GetSingle();
                        chunkEmbeddings.Add(vec);
                    }
                    break;
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception ex)
                {
                    var delay = Math.Min(1000 * Math.Pow(2, attempt), 60_000);
                    _logger.LogWarning(ex, "DocGrok error, attempt {Attempt}, retrying", attempt);
                    await Task.Delay((int)delay, ct);
                }
            }
            if (chunkEmbeddings is null || chunkEmbeddings.Count != chunk.Count)
                return null;
            allEmbeddings.AddRange(chunkEmbeddings);
        }
        return allEmbeddings;
    }

    private async Task ProcessInlineAsync(
        List<(string docId, string content, string contentHash, Dictionary<string, object?> row)> docs,
        List<Pipeline> pipelines, string pk, CancellationToken ct)
    {
        foreach (var pipeline in pipelines)
        {
            var sw = Stopwatch.StartNew();
            var texts = docs.Select(d => d.content).ToList();

            var embeddings = await EmbedTextsAsync(pipeline.DocgrokPipeline, texts, ct);

            if (embeddings is null || embeddings.Count != docs.Count)
            {
                _logger.LogError("Embed count mismatch for {Pipeline}: sent {Sent}, got {Got}",
                    pipeline.Name, docs.Count, embeddings?.Count ?? 0);
                continue;
            }

            // UPDATE rows with embedding (pgvector format) + metadata
            await using var conn = new NpgsqlConnection(_source.ConnectionString);
            await conn.OpenAsync(ct);
            var schema = _source.SchemaName ?? "public";
            var table = _source.Table!;
            var now = DateTime.UtcNow.ToString("O");

            // Ensure metadata columns exist
            var ddl = $@"
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS cfp_generation TEXT DEFAULT '';
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS pipeline_id TEXT DEFAULT '';
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS pipeline_name TEXT DEFAULT '';
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT '';
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ DEFAULT NOW();
                ALTER TABLE ""{schema}"".""{table}"" ADD COLUMN IF NOT EXISTS embedding_dims INTEGER DEFAULT 0;";
            await using (var alterCmd = new NpgsqlCommand(ddl, conn))
                try { await alterCmd.ExecuteNonQueryAsync(ct); } catch { /* ignore if exists */ }

            for (int i = 0; i < docs.Count; i++)
            {
                var (docId, _, contentHash, _) = docs[i];
                // pgvector stores as float array: '[0.1, 0.2, ...]'
                var vecStr = "[" + string.Join(",", embeddings[i].Select(f => f.ToString("G"))) + "]";

                for (int attempt = 1; ; attempt++)
                {
                    try
                    {
                        await using var cmd = new NpgsqlCommand($@"
                            UPDATE ""{schema}"".""{table}"" SET
                                embedding = @embedding::vector,
                                embedded_at = @embedded_at,
                                embedding_dims = @dims,
                                pipeline_id = @pipeline_id,
                                pipeline_name = @pipeline_name,
                                content_hash = @content_hash,
                                cfp_generation = @gen
                            WHERE ""{pk}"" = @id", conn);
                        cmd.Parameters.AddWithValue("@embedding", vecStr);
                        cmd.Parameters.AddWithValue("@embedded_at", now);
                        cmd.Parameters.AddWithValue("@dims", embeddings[i].Length);
                        cmd.Parameters.AddWithValue("@pipeline_id", pipeline.Id);
                        cmd.Parameters.AddWithValue("@pipeline_name", pipeline.Name);
                        cmd.Parameters.AddWithValue("@content_hash", contentHash);
                        cmd.Parameters.AddWithValue("@gen", Generation);
                        if (long.TryParse(docId, out var longId))
                            cmd.Parameters.AddWithValue("@id", longId);
                        else
                            cmd.Parameters.AddWithValue("@id", docId);
                        await cmd.ExecuteNonQueryAsync(ct);
                        break;
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (NpgsqlException ex) when (ex.IsTransient)
                    {
                        var delay = Math.Min(500 * Math.Pow(2, attempt), 30_000);
                        _logger.LogWarning("PostgreSQL transient error patching {DocId}, attempt {Attempt}", docId, attempt);
                        await Task.Delay((int)delay, ct);
                    }
                }
            }

            sw.Stop();
            _logger.LogInformation("Inline: {Count} docs embedded for pipeline={Pipeline} in {Elapsed}ms",
                docs.Count, pipeline.Name, sw.ElapsedMilliseconds);

            var batchKey = $"pg:{_source.Id}:{docs[0].docId}:{docs.Count}";
            _ = _apiClient.ReportInlineMetricsAsync(
                pipeline.Id, docs.Count, 0, sw.ElapsedMilliseconds, batchKey, ct);
        }
    }

    private async Task PublishToServiceBusAsync(
        List<(string docId, string content, string contentHash, Dictionary<string, object?> row)> docs,
        List<Pipeline> pipelines, CancellationToken ct)
    {
        foreach (var pipeline in pipelines)
        {
            var dest = _destinations.FirstOrDefault(d => d.Id == pipeline.DestinationId);
            if (dest is null) continue;

            var messages = docs.Select(d => new EmbeddingMessage
            {
                PipelineId = pipeline.Id,
                PipelineName = pipeline.Name,
                DocgrokPipeline = pipeline.DocgrokPipeline,
                SourceId = _source.Id,
                SourceRef = d.docId,
                DestinationId = dest.Id,
                DestinationType = dest.Type,
                DestinationConfig = dest.Config,
                Content = d.content,
                ContentHash = d.contentHash,
                PartitionKeyValue = d.docId,
                PipelineGeneration = Generation,
            }).ToList();

            await _sbPublisher!.PublishBatchAsync(messages, ct);
            _logger.LogInformation("Published {Count} messages to SB for pipeline={Pipeline}",
                messages.Count, pipeline.Name);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _cts?.Cancel();
        if (_pollTask is not null)
            try { await _pollTask; } catch { }
        _cts?.Dispose();
        _docGrokClient.Dispose();
    }

    private class DocGrokResponse
    {
        [System.Text.Json.Serialization.JsonPropertyName("outputs")]
        public List<float[]> Outputs { get; set; } = new();
    }
}
