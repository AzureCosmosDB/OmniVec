using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// Watches a Databricks Delta Lake table for changes via the Delta Change Data Feed (CDF)
/// using the Databricks SQL Statement Execution REST API. Each poll runs:
///   SELECT * FROM table_changes('catalog.schema.table', &lt;sinceVersion + 1&gt;)
/// and emits insert + update_postimage rows to Service Bus as embedding jobs.
///
/// Auth supports either:
///   * a Personal Access Token (PAT) referenced by Key Vault URI in `pat_secret_ref`, OR
///   * the pod's workload-identity MI (AAD token, resource = Databricks first-party app
///     id 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d).
///
/// Cursor is the Delta `_commit_version` (long) of the last row processed. Stored in
/// memory only for v1 (re-scans from v0 on pod restart — fine for the demo).
/// </summary>
public class DatabricksCdcWatcher : ISourceWatcher
{
    // Databricks first-party application id used as the AAD resource for MI tokens.
    private const string DatabricksAadResource = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d";

    private readonly Source _source;
    private readonly ChangeFeedOptions _options;
    private readonly OmniVecApiClient _apiClient;
    private readonly ContentHasher _hasher;
    private readonly ILogger<DatabricksCdcWatcher> _logger;
    private readonly ServiceBusPublisher? _sbPublisher;
    private readonly HttpClient _http;
    private readonly TokenCredential _credential = new DefaultAzureCredential();

    private CancellationTokenSource? _cts;
    private Task? _pollTask;

    private List<Pipeline> _activePipelines = new();
    private readonly object _pipelineLock = new();
    private List<Destination> _destinations = new();

    // -1 means "no rows seen yet" → first poll reads from version 0
    private long _lastVersion = -1;

    public string SourceId => _source.Id;
    public string Generation { get; }
    public bool SkipContentHash { get; set; }

    public DatabricksCdcWatcher(
        Source source,
        ChangeFeedOptions options,
        OmniVecApiClient apiClient,
        ContentHasher hasher,
        ILogger<DatabricksCdcWatcher> logger,
        string? generation = null,
        ServiceBusPublisher? sbPublisher = null)
    {
        _source = source;
        _options = options;
        _apiClient = apiClient;
        _hasher = hasher;
        _logger = logger;
        _sbPublisher = sbPublisher;
        Generation = generation ?? "0";

        var workspace = _source.DatabricksWorkspaceUrl
            ?? throw new InvalidOperationException(
                $"Databricks source {source.Id} missing required config 'workspace_url'");
        _http = new HttpClient { BaseAddress = new Uri(workspace.TrimEnd('/')), Timeout = TimeSpan.FromMinutes(2) };
    }

    public void UpdatePipelines(List<Pipeline> pipelines)
    {
        lock (_pipelineLock) { _activePipelines = new List<Pipeline>(pipelines); }
    }

    public void UpdateDestinations(List<Destination> destinations) => _destinations = destinations;

    public Task StartAsync(CancellationToken ct)
    {
        // Validate required config up-front so misconfigured sources fail loudly.
        _ = _source.DatabricksHttpPath ?? throw new InvalidOperationException(
            $"Databricks source {_source.Id} missing required config 'http_path'");
        _ = _source.DatabricksCatalog ?? throw new InvalidOperationException(
            $"Databricks source {_source.Id} missing required config 'catalog'");
        _ = _source.SchemaName ?? throw new InvalidOperationException(
            $"Databricks source {_source.Id} missing required config 'schema'");
        _ = _source.Table ?? throw new InvalidOperationException(
            $"Databricks source {_source.Id} missing required config 'table'");

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _pollTask = PollLoopAsync(_cts.Token);

        _logger.LogInformation(
            "Started Databricks CDF watcher for {Source} ({Catalog}.{Schema}.{Table}) auth={Auth} gen={Gen}",
            _source.Name,
            _source.DatabricksCatalog, _source.SchemaName, _source.Table,
            _source.DatabricksAuthType, Generation);
        return Task.CompletedTask;
    }

    private async Task PollLoopAsync(CancellationToken ct)
    {
        var fqTable = $"{_source.DatabricksCatalog}.{_source.SchemaName}.{_source.Table}";
        var pollInterval = TimeSpan.FromSeconds(Math.Max(10, _options.SourcePollIntervalSeconds));

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var since = _lastVersion + 1;
                var sql = $"SELECT * FROM table_changes('{fqTable}', {since}) " +
                          "WHERE _change_type IN ('insert', 'update_postimage') " +
                          "ORDER BY _commit_version ASC LIMIT 1000";

                var rows = await ExecuteStatementAsync(sql, ct);
                if (rows.Count > 0)
                {
                    var maxV = rows.Max(r => GetLong(r, "_commit_version") ?? _lastVersion);
                    await HandleChangesAsync(rows, ct);
                    _lastVersion = maxV;
                    _logger.LogInformation(
                        "Databricks CDF source={SourceId}: processed {Count} rows, cursor → v{Version}",
                        _source.Id, rows.Count, _lastVersion);
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                // Common cause for an "expected" failure is the warehouse being cold-started
                // (Statement Execution API will start it but the first call can time out).
                // Just log and retry; backoff is the next poll interval.
                _logger.LogWarning(ex,
                    "Databricks CDF poll failed for source {SourceId} ({Name}) — retrying in {Sec}s",
                    _source.Id, _source.Name, pollInterval.TotalSeconds);
            }

            try { await Task.Delay(pollInterval, ct); } catch { break; }
        }
    }

    // -------- Databricks REST helpers --------

    private async Task<string> GetAuthHeaderAsync(CancellationToken ct)
    {
        if (string.Equals(_source.DatabricksAuthType, "pat", StringComparison.OrdinalIgnoreCase))
        {
            var secretRef = _source.DatabricksPatSecretRef
                ?? throw new InvalidOperationException(
                    $"Databricks source {_source.Id} auth_type=pat requires 'pat_secret_ref' (Key Vault URI)");
            // Fetch PAT via MI from Key Vault.
            var kvUri = new Uri(secretRef);
            var kvScope = "https://vault.azure.net/.default";
            var kvToken = await _credential.GetTokenAsync(
                new TokenRequestContext(new[] { kvScope }), ct);
            using var req = new HttpRequestMessage(HttpMethod.Get, secretRef + "?api-version=7.4");
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", kvToken.Token);
            using var resp = await new HttpClient().SendAsync(req, ct);
            resp.EnsureSuccessStatusCode();
            var json = await resp.Content.ReadAsStringAsync(ct);
            using var doc = JsonDocument.Parse(json);
            var pat = doc.RootElement.GetProperty("value").GetString()
                ?? throw new InvalidOperationException("Key Vault secret value was empty");
            return $"Bearer {pat}";
        }

        // Default: MI / workload identity → AAD token for Databricks resource.
        var token = await _credential.GetTokenAsync(
            new TokenRequestContext(new[] { $"{DatabricksAadResource}/.default" }), ct);
        return $"Bearer {token.Token}";
    }

    /// <summary>
    /// Submits a SQL statement to the warehouse via the Statement Execution API
    /// and returns the result rows as a list of column-name → value dictionaries.
    /// Synchronous wait mode (max 50s); polls for SUCCEEDED if it returns PENDING/RUNNING.
    /// </summary>
    private async Task<List<Dictionary<string, object?>>> ExecuteStatementAsync(string sql, CancellationToken ct)
    {
        var auth = await GetAuthHeaderAsync(ct);

        var body = new
        {
            statement = sql,
            warehouse_id = ExtractWarehouseId(_source.DatabricksHttpPath!),
            wait_timeout = "50s",
            disposition = "INLINE",
            format = "JSON_ARRAY",
        };
        var bodyJson = JsonSerializer.Serialize(body);

        using var req = new HttpRequestMessage(HttpMethod.Post, "/api/2.0/sql/statements")
        {
            Content = new StringContent(bodyJson, Encoding.UTF8, "application/json"),
        };
        req.Headers.TryAddWithoutValidation("Authorization", auth);

        using var resp = await _http.SendAsync(req, ct);
        var respText = await resp.Content.ReadAsStringAsync(ct);
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException(
                $"Databricks SQL POST failed {(int)resp.StatusCode}: {Truncate(respText, 500)}");

        using var initial = JsonDocument.Parse(respText);
        var root = initial.RootElement;
        var statementId = root.GetProperty("statement_id").GetString()!;
        var state = root.GetProperty("status").GetProperty("state").GetString();

        // Poll until SUCCEEDED / terminal.
        JsonElement final = root.Clone();
        int polls = 0;
        while (state is "PENDING" or "RUNNING")
        {
            if (++polls > 60) throw new TimeoutException(
                $"Databricks statement {statementId} still {state} after 60 polls");
            await Task.Delay(TimeSpan.FromSeconds(2), ct);
            using var pollReq = new HttpRequestMessage(HttpMethod.Get, $"/api/2.0/sql/statements/{statementId}");
            pollReq.Headers.TryAddWithoutValidation("Authorization", auth);
            using var pollResp = await _http.SendAsync(pollReq, ct);
            var pollText = await pollResp.Content.ReadAsStringAsync(ct);
            pollResp.EnsureSuccessStatusCode();
            using var pollDoc = JsonDocument.Parse(pollText);
            final = pollDoc.RootElement.Clone();
            state = final.GetProperty("status").GetProperty("state").GetString();
        }

        if (state != "SUCCEEDED")
        {
            string err = "";
            if (final.GetProperty("status").TryGetProperty("error", out var e))
                err = e.GetProperty("message").GetString() ?? "";
            throw new InvalidOperationException(
                $"Databricks statement {statementId} terminal state {state}: {Truncate(err, 500)}");
        }

        return ParseResult(final);
    }

    /// <summary>Extract warehouse id from an http_path like "/sql/1.0/warehouses/2ec9fdcc70a48b2f".</summary>
    private static string ExtractWarehouseId(string httpPath)
    {
        var parts = httpPath.TrimEnd('/').Split('/');
        return parts[^1];
    }

    private static List<Dictionary<string, object?>> ParseResult(JsonElement final)
    {
        var rows = new List<Dictionary<string, object?>>();
        if (!final.TryGetProperty("manifest", out var manifest)) return rows;
        if (!manifest.TryGetProperty("schema", out var schema)) return rows;
        if (!schema.TryGetProperty("columns", out var cols)) return rows;

        var colNames = new List<string>();
        var colTypes = new List<string>();
        foreach (var c in cols.EnumerateArray())
        {
            colNames.Add(c.GetProperty("name").GetString() ?? "");
            colTypes.Add(c.TryGetProperty("type_name", out var tn) ? tn.GetString() ?? "STRING" : "STRING");
        }

        if (!final.TryGetProperty("result", out var result)) return rows;
        if (!result.TryGetProperty("data_array", out var dataArr)) return rows;

        foreach (var rowEl in dataArr.EnumerateArray())
        {
            var dict = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
            int i = 0;
            foreach (var cell in rowEl.EnumerateArray())
            {
                var name = i < colNames.Count ? colNames[i] : $"col{i}";
                var type = i < colTypes.Count ? colTypes[i] : "STRING";
                object? value = cell.ValueKind == JsonValueKind.Null
                    ? null
                    : CoerceCell(cell.GetString(), type);
                dict[name] = value;
                i++;
            }
            rows.Add(dict);
        }
        return rows;
    }

    private static object? CoerceCell(string? raw, string typeName)
    {
        if (raw is null) return null;
        switch (typeName.ToUpperInvariant())
        {
            case "INT":
            case "SHORT":
                return int.TryParse(raw, out var i) ? i : raw;
            case "LONG":
            case "BIGINT":
                return long.TryParse(raw, out var l) ? l : raw;
            case "DOUBLE":
            case "FLOAT":
                return double.TryParse(raw, out var d) ? d : raw;
            case "BOOLEAN":
                return bool.TryParse(raw, out var b) ? b : raw;
            default:
                return raw;
        }
    }

    private static long? GetLong(Dictionary<string, object?> row, string key)
    {
        if (!row.TryGetValue(key, out var v) || v is null) return null;
        return v switch
        {
            long ll => ll,
            int ii => ii,
            string s when long.TryParse(s, out var ls) => ls,
            _ => null,
        };
    }

    private static string Truncate(string s, int max) => s.Length <= max ? s : s.Substring(0, max) + "…";

    // -------- Change handling (mirrors PostgresCdcWatcher.HandleChangesAsync) --------

    private async Task HandleChangesAsync(
        List<Dictionary<string, object?>> changes, CancellationToken ct)
    {
        List<Pipeline> pipelines;
        lock (_pipelineLock) { pipelines = new List<Pipeline>(_activePipelines); }

        var relevantPipelines = pipelines
            .Where(p => p.Sources.Any(ps => ps.SourceId == _source.Id))
            .ToList();
        if (relevantPipelines.Count == 0) return;

        // Databricks v1 supports queue mode only (inline would require UPDATE-back via SQL warehouse).
        var queuePipelines = relevantPipelines.Where(p => p.ProcessingMode != "inline").ToList();
        if (queuePipelines.Count == 0)
        {
            _logger.LogWarning(
                "Databricks source {SourceId}: inline pipelines are not supported in v1; skipping {Count} changes",
                _source.Id, changes.Count);
            return;
        }
        if (_sbPublisher?.IsEnabled != true)
        {
            _logger.LogWarning(
                "Databricks source {SourceId}: queue pipeline configured but ServiceBus publisher disabled; skipping",
                _source.Id);
            return;
        }

        var pipelineSource = relevantPipelines[0].Sources.FirstOrDefault(ps => ps.SourceId == _source.Id);
        var cfFields = pipelineSource?.ContentFields ?? new List<string> { _source.DatabricksContentColumn };
        var idCol = _source.DatabricksIdColumn;

        var eligible = new List<(string docId, string content, string contentHash, Dictionary<string, object?> row)>();
        int skippedNoContent = 0;

        foreach (var row in changes)
        {
            if (!Source.RowHasContent(row, cfFields)) { skippedNoContent++; continue; }
            var content = Source.ExtractContentFromRow(row, cfFields);
            if (string.IsNullOrEmpty(content)) { skippedNoContent++; continue; }

            var contentHash = _hasher.ComputeHash(content);
            var docId = row.TryGetValue(idCol, out var pkVal) ? pkVal?.ToString() ?? "" : "";
            if (string.IsNullOrEmpty(docId))
            {
                // Fall back to commit version + row hash for synthetic id so we never lose a row.
                var v = GetLong(row, "_commit_version") ?? 0;
                docId = $"v{v}-{contentHash.Substring(0, 12)}";
            }
            eligible.Add((docId, content, contentHash, row));
        }

        _logger.LogInformation(
            "Databricks CDF source={SourceId} ({Name}): {Total} changes → {Eligible} eligible, skipped: {NoContent} no-content",
            _source.Id, _source.Name, changes.Count, eligible.Count, skippedNoContent);

        if (eligible.Count == 0) return;

        await PublishToServiceBusAsync(eligible, queuePipelines, ct);
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
                StoreContent = pipeline.StoreContent,
                ContentField = pipeline.ContentField,
                MetadataFields = pipeline.MetadataFields,
            }).ToList();

            await _sbPublisher!.PublishBatchAsync(messages, ct);
            _logger.LogInformation(
                "Published {Count} Databricks messages to SB for pipeline={Pipeline}",
                messages.Count, pipeline.Name);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _cts?.Cancel();
        if (_pollTask is not null)
            try { await _pollTask; } catch { }
        _cts?.Dispose();
        _http.Dispose();
    }
}
