using System.Net.Http.Json;
using System.Text.Json;

namespace OmniVec.Worker.Services;

/// <summary>
/// HTTP client for DocGrok router with infinite retry on transient errors.
/// Handles both text embedding (/embed/batch) and blob/PDF processing (/process/blob).
/// The router decides whether to call models directly or route through pipeline-worker.
/// </summary>
public class DocGrokClient
{
    private readonly HttpClient _http;
    private readonly ILogger<DocGrokClient> _logger;

    public DocGrokClient(HttpClient http, ILogger<DocGrokClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    /// <summary>
    /// Embed a batch of texts. Returns one float[] per input text.
    /// Retries infinitely on 429/5xx/network errors.
    /// </summary>
    public async Task<List<float[]>> EmbedBatchAsync(
        string modelOrPipeline, List<string> texts, CancellationToken ct)
    {
        object request;
        if (modelOrPipeline.StartsWith("mdl-"))
            request = new { model_id = modelOrPipeline, texts };
        else
            request = new { pipeline = modelOrPipeline, texts };

        HttpResponseMessage resp = null!;
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                resp = await _http.PostAsJsonAsync("/embed/batch", request, ct);

                if ((int)resp.StatusCode == 429)
                {
                    var retryAfter = resp.Headers.RetryAfter?.Delta
                        ?? TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("Embed 429, attempt {Attempt}, retry after {Delay}s",
                        attempt, retryAfter.TotalSeconds);
                    await Task.Delay(retryAfter, ct);
                    continue;
                }

                if ((int)resp.StatusCode >= 500)
                {
                    var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("Embed {Status}, attempt {Attempt}, retry after {Delay}s",
                        resp.StatusCode, attempt, delay.TotalSeconds);
                    await Task.Delay(delay, ct);
                    continue;
                }

                // Non-retryable 4xx (400 context length, 413 payload too large, etc.).
                // Surface as a typed exception so the worker can bisect/dead-letter
                // instead of abandoning the whole batch into a redelivery loop.
                if ((int)resp.StatusCode >= 400)
                {
                    var body = await resp.Content.ReadAsStringAsync(ct);
                    throw new EmbeddingClientException(
                        (int)resp.StatusCode,
                        body,
                        $"Embed returned {(int)resp.StatusCode} {resp.StatusCode}: {Truncate(body, 500)}");
                }

                break;
            }
            catch (EmbeddingClientException) { throw; }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                _logger.LogWarning("Embed call failed: {Error}, attempt {Attempt}, retry after {Delay}s",
                    ex.Message, attempt, delay.TotalSeconds);
                await Task.Delay(delay, ct);
            }
        }

        resp.EnsureSuccessStatusCode();

        var json = await resp.Content.ReadAsStringAsync(ct);
        using var doc = JsonDocument.Parse(json);
        var outputs = doc.RootElement.GetProperty("outputs");

        var results = new List<float[]>(outputs.GetArrayLength());
        foreach (var output in outputs.EnumerateArray())
        {
            // Handle nested arrays: [[0.1, 0.2, ...]] → [0.1, 0.2, ...]
            var arr = output;
            if (arr.ValueKind == JsonValueKind.Array && arr.GetArrayLength() > 0 &&
                arr[0].ValueKind == JsonValueKind.Array)
                arr = arr[0];

            var floats = new float[arr.GetArrayLength()];
            int i = 0;
            foreach (var val in arr.EnumerateArray())
                floats[i++] = val.GetSingle();
            results.Add(floats);
        }

        return results;
    }

    private static string Truncate(string s, int max)
        => string.IsNullOrEmpty(s) ? "" : (s.Length <= max ? s : s.Substring(0, max) + "…");

    /// <summary>
    /// Process a blob/PDF via DocGrok router.Passes blob reference URLs to the router,
    /// which routes to the pipeline-worker for OCR/chunking/embedding.
    /// Returns a list of (chunkText, embedding) pairs — one per chunk.
    /// </summary>
    public async Task<List<(string ChunkText, float[] Embedding)>> EmbedBlobAsync(
        string modelOrPipeline,
        string blobAccountUrl,
        string? blobConnectionString,
        string blobContainer,
        string blobName,
        CancellationToken ct)
    {
        var request = new Dictionary<string, object?>
        {
            ["blob_name"] = blobName,
            ["blob_container"] = blobContainer,
        };

        if (modelOrPipeline.StartsWith("mdl-"))
            request["model_id"] = modelOrPipeline;
        else
            request["pipeline"] = modelOrPipeline;

        if (!string.IsNullOrEmpty(blobConnectionString))
            request["blob_connection_string"] = blobConnectionString;
        else if (!string.IsNullOrEmpty(blobAccountUrl))
            request["blob_account_url"] = blobAccountUrl;

        HttpResponseMessage resp = null!;
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                resp = await _http.PostAsJsonAsync("/embed", request, ct);

                if ((int)resp.StatusCode == 429)
                {
                    var retryAfter = resp.Headers.RetryAfter?.Delta
                        ?? TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("EmbedBlob 429, attempt {Attempt}, retry after {Delay}s",
                        attempt, retryAfter.TotalSeconds);
                    await Task.Delay(retryAfter, ct);
                    continue;
                }

                if ((int)resp.StatusCode >= 500)
                {
                    var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("EmbedBlob {Status}, attempt {Attempt}, retry after {Delay}s",
                        resp.StatusCode, attempt, delay.TotalSeconds);
                    await Task.Delay(delay, ct);
                    continue;
                }

                break;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                _logger.LogWarning("EmbedBlob call failed: {Error}, attempt {Attempt}, retry after {Delay}s",
                    ex.Message, attempt, delay.TotalSeconds);
                await Task.Delay(delay, ct);
            }
        }

        resp.EnsureSuccessStatusCode();

        var json = await resp.Content.ReadAsStringAsync(ct);
        using var doc = JsonDocument.Parse(json);

        // Response format: { "chunks": [{ "text": "...", "embedding": [0.1, ...] }, ...] }
        var results = new List<(string, float[])>();

        if (doc.RootElement.TryGetProperty("chunks", out var chunks))
        {
            foreach (var chunk in chunks.EnumerateArray())
            {
                var text = chunk.GetProperty("text").GetString() ?? "";
                var embArr = chunk.GetProperty("embedding");
                var target = embArr;
                if (target.ValueKind == JsonValueKind.Array && target.GetArrayLength() > 0 &&
                    target[0].ValueKind == JsonValueKind.Array)
                    target = target[0];
                var floats = new float[target.GetArrayLength()];
                int i = 0;
                foreach (var val in target.EnumerateArray())
                    floats[i++] = val.GetSingle();
                results.Add((text, floats));
            }
        }
        else if (doc.RootElement.TryGetProperty("outputs", out var outputs))
        {
            // Fallback: same format as text embedding
            foreach (var output in outputs.EnumerateArray())
            {
                var arr = output;
                if (arr.ValueKind == JsonValueKind.Array && arr.GetArrayLength() > 0 &&
                    arr[0].ValueKind == JsonValueKind.Array)
                    arr = arr[0];
                var floats = new float[arr.GetArrayLength()];
                int i = 0;
                foreach (var val in arr.EnumerateArray())
                    floats[i++] = val.GetSingle();
                results.Add(("", floats));
            }
        }

        return results;
    }

    /// <summary>
    /// Bulk blob embedding. Posts a single request to docgrok router with a
    /// list of blob names; the router forwards to the backend's bulk endpoint
    /// (e.g. CLIP /v1/embeddings) which parallel-downloads and runs a single
    /// batched forward. Returns one float[] per input blob, in input order.
    /// Retries 429/5xx/transient errors.
    /// </summary>
    public async Task<List<float[]>> EmbedBlobBatchAsync(
        string modelOrPipeline,
        string blobAccountUrl,
        string blobContainer,
        List<string> blobNames,
        CancellationToken ct)
    {
        if (blobNames.Count == 0) return new List<float[]>();

        var request = new Dictionary<string, object?>
        {
            ["model_id"] = modelOrPipeline,
            ["blob_names"] = blobNames,
            ["blob_account_url"] = blobAccountUrl,
            ["blob_container"] = blobContainer,
        };

        HttpResponseMessage resp = null!;
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                resp = await _http.PostAsJsonAsync("/embed", request, ct);

                if ((int)resp.StatusCode == 429)
                {
                    var retryAfter = resp.Headers.RetryAfter?.Delta
                        ?? TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("EmbedBlobBatch 429, attempt {Attempt}, retry after {Delay}s",
                        attempt, retryAfter.TotalSeconds);
                    await Task.Delay(retryAfter, ct);
                    continue;
                }

                if ((int)resp.StatusCode >= 500)
                {
                    var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                    _logger.LogWarning("EmbedBlobBatch {Status}, attempt {Attempt}, retry after {Delay}s",
                        resp.StatusCode, attempt, delay.TotalSeconds);
                    await Task.Delay(delay, ct);
                    continue;
                }
                break;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                var delay = TimeSpan.FromSeconds(Math.Min(Math.Pow(2, attempt), 60));
                _logger.LogWarning("EmbedBlobBatch call failed: {Error}, attempt {Attempt}, retry after {Delay}s",
                    ex.Message, attempt, delay.TotalSeconds);
                await Task.Delay(delay, ct);
            }
        }

        resp.EnsureSuccessStatusCode();
        var json = await resp.Content.ReadAsStringAsync(ct);
        using var doc = JsonDocument.Parse(json);

        var results = new List<float[]>(blobNames.Count);
        if (doc.RootElement.TryGetProperty("chunks", out var chunks))
        {
            foreach (var chunk in chunks.EnumerateArray())
            {
                var embArr = chunk.GetProperty("embedding");
                var target = embArr;
                if (target.ValueKind == JsonValueKind.Array && target.GetArrayLength() > 0 &&
                    target[0].ValueKind == JsonValueKind.Array)
                    target = target[0];
                var floats = new float[target.GetArrayLength()];
                int i = 0;
                foreach (var val in target.EnumerateArray())
                    floats[i++] = val.GetSingle();
                results.Add(floats);
            }
        }
        if (results.Count != blobNames.Count)
            throw new EmbeddingClientException(
                200,
                null,
                $"EmbedBlobBatch returned {results.Count} embeddings for {blobNames.Count} blobs");
        return results;
    }
}
