using Npgsql;

namespace OmniVec.Worker.Destinations;

public class PostgresDestinationWriter : IDestinationWriter
{
    private readonly ILogger<PostgresDestinationWriter> _logger;

    public string DestinationType => "pgvector";

    public PostgresDestinationWriter(ILogger<PostgresDestinationWriter> logger)
    {
        _logger = logger;
    }

    private static string BuildConnectionString(Dictionary<string, object> config)
    {
        if (config.TryGetValue("connection_string", out var cs) && cs is not null)
            return cs.ToString()!;

        var host = config.GetValueOrDefault("host", "")?.ToString() ?? "";
        var port = config.GetValueOrDefault("port", "5432")?.ToString() ?? "5432";
        var database = config.GetValueOrDefault("database", "")?.ToString() ?? "";
        var user = config.GetValueOrDefault("user", "")?.ToString() ?? "";
        var password = config.GetValueOrDefault("password", "")?.ToString() ?? "";
        var sslMode = config.GetValueOrDefault("ssl_mode", "require")?.ToString() ?? "require";

        return $"Host={host};Port={port};Database={database};Username={user};Password={password};SSL Mode={sslMode}";
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

        await using var conn = new NpgsqlConnection(connStr);
        await conn.OpenAsync(ct);

        // Ensure pgvector extension and cfp_generation column exist
        try
        {
            await using (var cmd = new NpgsqlCommand("CREATE EXTENSION IF NOT EXISTS vector", conn))
                await cmd.ExecuteNonQueryAsync(ct);
            await using (var cmd2 = new NpgsqlCommand($"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS cfp_generation TEXT DEFAULT ''", conn))
                await cmd2.ExecuteNonQueryAsync(ct);
        }
        catch { /* ignore permission errors — columns may already exist */ }

        // Try UPDATE first (works when source=destination table with NOT NULL columns).
        // Fall back to INSERT ON CONFLICT for dedicated vector tables.
        var updateSql = $@"
            UPDATE {table} SET
                {vectorCol} = @embedding::vector,
                pipeline_id = @pipeline_id,
                pipeline_name = @pipeline_name,
                content_hash = @content_hash,
                embedded_at = @embedded_at,
                cfp_generation = @gen
            WHERE {idCol} = @id";

        var insertSql = $@"
            INSERT INTO {table} ({idCol}, {vectorCol}, {contentCol}, pipeline_id, pipeline_name, content_hash, embedded_at, cfp_generation)
            VALUES (@id, @embedding::vector, @content, @pipeline_id, @pipeline_name, @content_hash, @embedded_at, @gen)
            ON CONFLICT ({idCol}) DO UPDATE SET
                {vectorCol} = EXCLUDED.{vectorCol},
                {contentCol} = EXCLUDED.{contentCol},
                pipeline_id = EXCLUDED.pipeline_id,
                pipeline_name = EXCLUDED.pipeline_name,
                content_hash = EXCLUDED.content_hash,
                embedded_at = EXCLUDED.embedded_at,
                cfp_generation = EXCLUDED.cfp_generation";

        foreach (var result in results)
        {
            var embeddingStr = "[" + string.Join(",", result.Embedding) + "]";

            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    // Try UPDATE first
                    await using var cmd = new NpgsqlCommand(updateSql, conn);
                    if (long.TryParse(result.DocId, out var longId))
                        cmd.Parameters.AddWithValue("id", longId);
                    else
                        cmd.Parameters.AddWithValue("id", result.DocId);
                    cmd.Parameters.AddWithValue("embedding", embeddingStr);
                    cmd.Parameters.AddWithValue("pipeline_id", result.PipelineId);
                    cmd.Parameters.AddWithValue("pipeline_name", result.PipelineName);
                    cmd.Parameters.AddWithValue("content_hash", result.ContentHash);
                    cmd.Parameters.AddWithValue("embedded_at", DateTime.UtcNow);
                    cmd.Parameters.AddWithValue("gen", result.PipelineGeneration ?? "");
                    var rows = await cmd.ExecuteNonQueryAsync(ct);

                    if (rows == 0)
                    {
                        // Row doesn't exist — INSERT
                        await using var insertCmd = new NpgsqlCommand(insertSql, conn);
                        if (long.TryParse(result.DocId, out var insertLongId))
                            insertCmd.Parameters.AddWithValue("id", insertLongId);
                        else
                            insertCmd.Parameters.AddWithValue("id", result.DocId);
                        insertCmd.Parameters.AddWithValue("embedding", embeddingStr);
                        insertCmd.Parameters.AddWithValue("content", result.Content);
                        insertCmd.Parameters.AddWithValue("pipeline_id", result.PipelineId);
                        insertCmd.Parameters.AddWithValue("pipeline_name", result.PipelineName);
                        insertCmd.Parameters.AddWithValue("content_hash", result.ContentHash);
                        insertCmd.Parameters.AddWithValue("embedded_at", DateTime.UtcNow);
                        insertCmd.Parameters.AddWithValue("gen", result.PipelineGeneration ?? "");
                        await insertCmd.ExecuteNonQueryAsync(ct);
                    }
                    break;
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception ex)
                {
                    if (attempt >= 5)
                    {
                        _logger.LogError("Postgres write failed doc={DocId} after {Attempt} attempts: {Error}",
                            result.DocId, attempt, ex.Message);
                        break;
                    }
                    var delay = TimeSpan.FromMilliseconds(Math.Min(500 * Math.Pow(2, attempt), 30_000));
                    _logger.LogWarning("Postgres upsert error doc={DocId}: {Error}, attempt {Attempt}, retrying",
                        result.DocId, ex.Message, attempt);
                    await Task.Delay(delay, ct);
                }
            }
        }
    }
}
