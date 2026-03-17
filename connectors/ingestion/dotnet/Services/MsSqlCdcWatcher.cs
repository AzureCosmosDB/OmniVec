using System.Data;
using System.Diagnostics;
using System.Net.Http.Json;
using Microsoft.Data.SqlClient;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Watches an MS SQL table for changes using SQL Server CDC (Change Data Capture).
/// Requires CDC enabled on the source database and table.
/// Tracks LSN (Log Sequence Number) as checkpoint.
/// </summary>
public class MsSqlCdcWatcher : ISourceWatcher
{
    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ContentHasher _hasher;
    private readonly ILogger<MsSqlCdcWatcher> _logger;
    private readonly HttpClient _docGrokClient;
    private readonly ServiceBusPublisher? _sbPublisher;

    private CancellationTokenSource? _cts;
    private Task? _pollTask;

    private List<Pipeline> _activePipelines = new();
    private readonly object _pipelineLock = new();
    private List<Destination> _destinations = new();

    private byte[]? _lastLsn; // CDC checkpoint
    private bool _fullScanDone;
    private long _lastFullScanPk; // PK tiebreaker for pagination

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public MsSqlCdcWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        ContentHasher hasher,
        ILogger<MsSqlCdcWatcher> logger,
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
        lock (_pipelineLock) { _activePipelines = pipelines; }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public async Task StartAsync(CancellationToken ct)
    {
        // Verify CDC is enabled on the table
        await using var conn = new SqlConnection(_source.ConnectionString);
        await conn.OpenAsync(ct);

        var schema = _source.SchemaName ?? "dbo";
        var table = _source.Table!;
        var captureInstance = $"{schema}_{table}";

        await using var checkCmd = new SqlCommand(
            "SELECT COUNT(*) FROM cdc.change_tables WHERE capture_instance = @ci", conn);
        checkCmd.Parameters.AddWithValue("@ci", captureInstance);
        var cdcEnabled = (int)(await checkCmd.ExecuteScalarAsync(ct))! > 0;

        // Ensure cfp_generation column exists
        try
        {
            await using var alterCmd = new SqlCommand(
                $"IF COL_LENGTH('[{schema}].[{table}]', 'cfp_generation') IS NULL ALTER TABLE [{schema}].[{table}] ADD cfp_generation NVARCHAR(50) DEFAULT ''", conn);
            await alterCmd.ExecuteNonQueryAsync(ct);
        }
        catch { /* ignore if already exists or permission error */ }

        if (!cdcEnabled)
        {
            _logger.LogWarning("CDC not enabled for {Schema}.{Table} (capture_instance={CI}). Enabling now...",
                schema, table, captureInstance);
            await using var enableCmd = new SqlCommand($@"
                EXEC sys.sp_cdc_enable_table
                    @source_schema = N'{schema}',
                    @source_name = N'{table}',
                    @role_name = NULL,
                    @supports_net_changes = 1", conn);
            await enableCmd.ExecuteNonQueryAsync(ct);
            _logger.LogInformation("CDC enabled for {Schema}.{Table}", schema, table);
        }

        // Get initial LSN (start from now for first run, or from beginning if generation reset)
        _lastLsn = null; // Start from beginning

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = PollLoopAsync(_cts.Token);

        _logger.LogInformation("Started MS SQL CDC watcher for {Source} ({Schema}.{Table}) gen={Gen}",
            _source.Name, schema, table, Generation);
    }

    private async Task PollLoopAsync(CancellationToken ct)
    {
        var schema = _source.SchemaName ?? "dbo";
        var table = _source.Table!;
        var captureInstance = $"{schema}_{table}";
        var pk = _source.PrimaryKey ?? "id";

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await using var conn = new SqlConnection(_source.ConnectionString);
                await conn.OpenAsync(ct);

                // Phase 1: Full table scan for existing data (paginated by PK)
                if (!_fullScanDone)
                {
                    var scanQuery = $@"
                        SELECT TOP 500 *
                        FROM [{schema}].[{table}]
                        WHERE [{pk}] > @lastPk
                        ORDER BY [{pk}]";
                    await using var scanCmd = new SqlCommand(scanQuery, conn);
                    scanCmd.CommandTimeout = 60;
                    if (_lastFullScanPk > 0)
                        scanCmd.Parameters.AddWithValue("@lastPk", _lastFullScanPk);
                    else
                        scanCmd.Parameters.AddWithValue("@lastPk", 0L);

                    var rows = new List<Dictionary<string, object?>>();
                    await using var reader = await scanCmd.ExecuteReaderAsync(ct);
                    while (await reader.ReadAsync(ct))
                    {
                        var row = new Dictionary<string, object?>();
                        for (int i = 0; i < reader.FieldCount; i++)
                        {
                            var name = reader.GetName(i);
                            row[name] = reader.IsDBNull(i) ? null : reader.GetValue(i);
                        }
                        rows.Add(row);
                    }

                    if (rows.Count > 0)
                    {
                        // Update PK bookmark
                        var lastRow = rows[^1];
                        if (lastRow.TryGetValue(pk, out var pkVal) && pkVal is not null)
                        {
                            if (pkVal is long l) _lastFullScanPk = l;
                            else if (pkVal is int n) _lastFullScanPk = n;
                            else if (long.TryParse(pkVal.ToString(), out var parsed)) _lastFullScanPk = parsed;
                        }

                        _logger.LogInformation(
                            "CDC source={SourceId} ({Name}): full scan batch {Count} rows (pk>{LastPk})",
                            _source.Id, _source.Name, rows.Count, _lastFullScanPk);

                        await HandleChangesAsync(rows, pk, ct);
                    }

                    if (rows.Count < 500)
                    {
                        _fullScanDone = true;
                        _logger.LogInformation("Full scan complete for {Source}, switching to CDC mode", _source.Name);
                    }

                    // Don't delay between full scan batches
                    continue;
                }

                // Phase 2: CDC-based incremental tracking
                // Get current max LSN
                await using var maxLsnCmd = new SqlCommand("SELECT sys.fn_cdc_get_max_lsn()", conn);
                var maxLsn = (byte[]?)await maxLsnCmd.ExecuteScalarAsync(ct);
                if (maxLsn is null || maxLsn.Length == 0)
                {
                    await Task.Delay(_options.FeedPollIntervalSeconds * 1000, ct);
                    continue;
                }

                // Get from LSN
                byte[] fromLsn;
                if (_lastLsn is null)
                {
                    // Start from the minimum available LSN
                    await using var minCmd = new SqlCommand(
                        $"SELECT sys.fn_cdc_get_min_lsn('{captureInstance}')", conn);
                    fromLsn = (byte[]?)await minCmd.ExecuteScalarAsync(ct) ?? maxLsn;
                }
                else
                {
                    // Increment past the last processed LSN
                    await using var incCmd = new SqlCommand(
                        "SELECT sys.fn_cdc_increment_lsn(@lsn)", conn);
                    incCmd.Parameters.Add("@lsn", SqlDbType.Binary, 10).Value = _lastLsn;
                    fromLsn = (byte[])(await incCmd.ExecuteScalarAsync(ct))!;
                }

                // Compare LSNs
                if (CompareBytes(fromLsn, maxLsn) > 0)
                {
                    await Task.Delay(_options.FeedPollIntervalSeconds * 1000, ct);
                    continue;
                }

                // Query CDC changes
                var query = $@"
                    SELECT ct.*, t.*
                    FROM cdc.fn_cdc_get_all_changes_{captureInstance}(@from_lsn, @to_lsn, N'all') ct
                    INNER JOIN [{schema}].[{table}] t ON ct.[{pk}] = t.[{pk}]
                    WHERE ct.__$operation IN (2, 4)  -- Insert or Update (after)
                    ORDER BY ct.__$start_lsn";

                await using var cmd = new SqlCommand(query, conn);
                cmd.Parameters.Add("@from_lsn", SqlDbType.Binary, 10).Value = fromLsn;
                cmd.Parameters.Add("@to_lsn", SqlDbType.Binary, 10).Value = maxLsn;
                cmd.CommandTimeout = 60;

                var changes = new List<Dictionary<string, object?>>();
                await using var cdcReader = await cmd.ExecuteReaderAsync(ct);
                while (await cdcReader.ReadAsync(ct))
                {
                    var row = new Dictionary<string, object?>();
                    for (int i = 0; i < cdcReader.FieldCount; i++)
                    {
                        var name = cdcReader.GetName(i);
                        row[name] = cdcReader.IsDBNull(i) ? null : cdcReader.GetValue(i);
                    }
                    changes.Add(row);
                }

                if (changes.Count > 0)
                {
                    await HandleChangesAsync(changes, pk, ct);
                }

                _lastLsn = maxLsn;
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogError(ex, "MS SQL CDC poll error for {Source}, backing off", _source.Name);
                try { await Task.Delay(_options.ErrorBackoffSeconds * 1000, ct); } catch { break; }
            }

            await Task.Delay(_options.FeedPollIntervalSeconds * 1000, ct);
        }
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

        // Filter eligible rows
        var eligible = new List<(string docId, string content, string contentHash, Dictionary<string, object?> row)>();
        int skippedNoContent = 0, skippedUnchanged = 0;

        foreach (var row in changes)
        {
            if (!_source.RowHasContent(row)) { skippedNoContent++; continue; }

            var content = _source.ExtractContentFromRow(row);
            if (string.IsNullOrEmpty(content)) { skippedNoContent++; continue; }

            var contentHash = _hasher.ComputeHash(content);

            if (!SkipContentHash)
            {
                var existingHash = row.TryGetValue("content_hash", out var h) ? h?.ToString() : null;
                if (contentHash == existingHash)
                {
                    // Check reset_at
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
                                needsReprocess = true;
                                break;
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

        // Inline mode: embed + UPDATE source rows directly
        if (inlinePipelines.Count > 0)
        {
            await ProcessInlineAsync(eligible, inlinePipelines, pk, ct);
        }

        // Queue mode: publish to Service Bus
        if (queuePipelines.Count > 0 && _sbPublisher?.IsEnabled == true)
        {
            await PublishToServiceBusAsync(eligible, queuePipelines, ct);
        }
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
                        _logger.LogWarning("DocGrok {Status}, attempt {Attempt}, retrying in {Delay}ms",
                            resp.StatusCode, attempt, delay);
                        await Task.Delay((int)delay, ct);
                        continue;
                    }
                    resp.EnsureSuccessStatusCode();

                    var json = await resp.Content.ReadAsStringAsync(ct);
                    using var doc = System.Text.Json.JsonDocument.Parse(json);
                    var outputs = doc.RootElement.GetProperty("outputs");
                    chunkEmbeddings = new List<float[]>();
                    foreach (var item in outputs.EnumerateArray())
                    {
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
                catch (HttpRequestException ex)
                {
                    var delay = Math.Min(1000 * Math.Pow(2, attempt), 60_000);
                    _logger.LogWarning(ex, "DocGrok error, attempt {Attempt}, retrying in {Delay}ms", attempt, delay);
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
        List<Pipeline> pipelines,
        string pk,
        CancellationToken ct)
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

            // UPDATE source rows with embedding, embedded_at, pipeline_id, content_hash
            await using var conn = new SqlConnection(_source.ConnectionString);
            await conn.OpenAsync(ct);
            var schema = _source.SchemaName ?? "dbo";
            var table = _source.Table!;
            var now = DateTime.UtcNow.ToString("O");

            for (int i = 0; i < docs.Count; i++)
            {
                var (docId, _, contentHash, _) = docs[i];
                var embeddingJson = System.Text.Json.JsonSerializer.Serialize(embeddings[i].ToList());

                for (int attempt = 1; ; attempt++)
                {
                    try
                    {
                        await using var cmd = new SqlCommand($@"
                            UPDATE [{schema}].[{table}] SET
                                embedding = @embedding,
                                embedded_at = @embedded_at,
                                embedding_dims = @dims,
                                pipeline_id = @pipeline_id,
                                pipeline_name = @pipeline_name,
                                content_hash = @content_hash,
                                cfp_generation = @gen
                            WHERE [{pk}] = @id", conn);
                        cmd.Parameters.AddWithValue("@embedding", embeddingJson);
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
                    catch (SqlException ex) when (ex.Number == -2 || ex.Number == 1205) // timeout or deadlock
                    {
                        var delay = Math.Min(500 * Math.Pow(2, attempt), 30_000);
                        _logger.LogWarning("SQL error {Number} patching {DocId}, attempt {Attempt}, retrying",
                            ex.Number, docId, attempt);
                        await Task.Delay((int)delay, ct);
                    }
                }
            }

            sw.Stop();
            _logger.LogInformation("Inline: {Count} docs embedded for pipeline={Pipeline} in {Elapsed}ms",
                docs.Count, pipeline.Name, sw.ElapsedMilliseconds);

            var batchKey = $"mssql:{_source.Id}:{docs[0].docId}:{docs.Count}";
            _ = _apiClient.ReportInlineMetricsAsync(
                pipeline.Id, docs.Count, 0, sw.ElapsedMilliseconds, batchKey, ct);
        }
    }

    private async Task PublishToServiceBusAsync(
        List<(string docId, string content, string contentHash, Dictionary<string, object?> row)> docs,
        List<Pipeline> pipelines,
        CancellationToken ct)
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

    private static int CompareBytes(byte[] a, byte[] b)
    {
        for (int i = 0; i < Math.Min(a.Length, b.Length); i++)
        {
            if (a[i] < b[i]) return -1;
            if (a[i] > b[i]) return 1;
        }
        return a.Length.CompareTo(b.Length);
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
        public List<float[]> Embeddings { get; set; } = new();
    }
}
