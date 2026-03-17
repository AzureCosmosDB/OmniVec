using Microsoft.Data.SqlClient;

namespace OmniVec.Worker.Destinations;

public class MsSqlDestinationWriter : IDestinationWriter
{
    private readonly ILogger<MsSqlDestinationWriter> _logger;

    public string DestinationType => "mssql";

    public MsSqlDestinationWriter(ILogger<MsSqlDestinationWriter> logger)
    {
        _logger = logger;
    }

    private static string BuildConnectionString(Dictionary<string, object> config)
    {
        if (config.TryGetValue("connection_string", out var cs) && cs is not null)
            return cs.ToString()!;

        var server = (config.GetValueOrDefault("server") ?? config.GetValueOrDefault("host", ""))?.ToString() ?? "";
        var database = config.GetValueOrDefault("database", "")?.ToString() ?? "";
        var user = config.GetValueOrDefault("user", "")?.ToString() ?? "";
        var password = config.GetValueOrDefault("password", "")?.ToString() ?? "";

        if (!string.IsNullOrEmpty(user))
            return $"Server={server};Database={database};User Id={user};Password={password};Encrypt=True;TrustServerCertificate=False;";

        // Managed identity
        return $"Server={server};Database={database};Encrypt=True;TrustServerCertificate=False;Authentication=Active Directory Default;";
    }

    public async Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        var connStr = BuildConnectionString(config);
        var table = config.GetValueOrDefault("table", "vectors")?.ToString() ?? "vectors";
        var idCol = config.GetValueOrDefault("id_column", config.GetValueOrDefault("id_col", "id"))?.ToString() ?? "id";
        var vectorCol = config.GetValueOrDefault("vector_column", config.GetValueOrDefault("vector_col", "embedding"))?.ToString() ?? "embedding";
        var contentCol = config.GetValueOrDefault("content_column", config.GetValueOrDefault("content_col", "content"))?.ToString() ?? "content";

        await using var conn = new SqlConnection(connStr);
        await conn.OpenAsync(ct);

        // Ensure cfp_generation column exists
        try
        {
            var schema = config.GetValueOrDefault("schema_name", config.GetValueOrDefault("schema", "dbo"))?.ToString() ?? "dbo";
            await using var alterCmd = new SqlCommand(
                $"IF COL_LENGTH('[{schema}].[{table}]', 'cfp_generation') IS NULL ALTER TABLE [{schema}].[{table}] ADD cfp_generation NVARCHAR(50) DEFAULT ''", conn);
            await alterCmd.ExecuteNonQueryAsync(ct);
        }
        catch { /* ignore if already exists */ }

        // Try UPDATE first (works when source=destination table with NOT NULL columns).
        // Fall back to MERGE for dedicated vector tables.
        var updateSql = $@"
            UPDATE [{table}] SET
                {vectorCol} = @embedding,
                pipeline_id = @pipeline_id,
                pipeline_name = @pipeline_name,
                content_hash = @content_hash,
                embedded_at = @embedded_at,
                cfp_generation = @gen
            WHERE {idCol} = @id";

        var insertSql = $@"
            MERGE [{table}] AS target
            USING (SELECT @id AS [{idCol}]) AS source
            ON target.[{idCol}] = source.[{idCol}]
            WHEN MATCHED THEN
                UPDATE SET {vectorCol} = @embedding, {contentCol} = @content,
                    pipeline_id = @pipeline_id, pipeline_name = @pipeline_name,
                    content_hash = @content_hash, embedded_at = @embedded_at,
                    cfp_generation = @gen
            WHEN NOT MATCHED THEN
                INSERT ([{idCol}], {vectorCol}, {contentCol}, pipeline_id, pipeline_name, content_hash, embedded_at, cfp_generation)
                VALUES (@id, @embedding, @content, @pipeline_id, @pipeline_name, @content_hash, @embedded_at, @gen);";

        foreach (var result in results)
        {
            var embeddingJson = "[" + string.Join(",", result.Embedding) + "]";

            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    // Try UPDATE first
                    await using var cmd = new SqlCommand(updateSql, conn);
                    if (long.TryParse(result.DocId, out var longId))
                        cmd.Parameters.AddWithValue("id", longId);
                    else
                        cmd.Parameters.AddWithValue("id", result.DocId);
                    cmd.Parameters.AddWithValue("embedding", embeddingJson);
                    cmd.Parameters.AddWithValue("pipeline_id", result.PipelineId);
                    cmd.Parameters.AddWithValue("pipeline_name", result.PipelineName);
                    cmd.Parameters.AddWithValue("content_hash", result.ContentHash);
                    cmd.Parameters.AddWithValue("embedded_at", DateTime.UtcNow);
                    cmd.Parameters.AddWithValue("gen", result.PipelineGeneration ?? "");
                    var rows = await cmd.ExecuteNonQueryAsync(ct);

                    if (rows == 0)
                    {
                        // Row doesn't exist — INSERT via MERGE
                        await using var mergeCmd = new SqlCommand(insertSql, conn);
                        if (long.TryParse(result.DocId, out var insertLongId))
                            mergeCmd.Parameters.AddWithValue("id", insertLongId);
                        else
                            mergeCmd.Parameters.AddWithValue("id", result.DocId);
                        mergeCmd.Parameters.AddWithValue("embedding", embeddingJson);
                        mergeCmd.Parameters.AddWithValue("content", result.Content);
                        mergeCmd.Parameters.AddWithValue("pipeline_id", result.PipelineId);
                        mergeCmd.Parameters.AddWithValue("pipeline_name", result.PipelineName);
                        mergeCmd.Parameters.AddWithValue("content_hash", result.ContentHash);
                        mergeCmd.Parameters.AddWithValue("embedded_at", DateTime.UtcNow);
                        mergeCmd.Parameters.AddWithValue("gen", result.PipelineGeneration ?? "");
                        await mergeCmd.ExecuteNonQueryAsync(ct);
                    }
                    break;
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception ex)
                {
                    if (attempt >= 5)
                    {
                        _logger.LogError("MSSQL write failed doc={DocId} after {Attempt} attempts: {Error}",
                            result.DocId, attempt, ex.Message);
                        break;
                    }
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("MSSQL upsert error doc={DocId}: {Error}, attempt {Attempt}, retrying",
                        result.DocId, ex.Message, attempt);
                    await Task.Delay(delay, ct);
                }
            }
        }
    }
}
