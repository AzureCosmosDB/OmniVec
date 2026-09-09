using System.Net.Http.Json;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace OmniVec.Worker.Services;

/// <summary>Bounded HTTP retries; delivery retries remain the queue's responsibility.</summary>
public class DocGrokClient
{
    private const int MaxAttempts = 4;
    private readonly HttpClient _http;
    private readonly ILogger<DocGrokClient> _logger;
    internal Func<TimeSpan, CancellationToken, Task> DelayAsync { get; set; } = Task.Delay;
    // API JSON, not HTML: escaping '+' in base64 as \u002B defeats the 4/3 wire budget.
    private static readonly JsonSerializerOptions InlineDocumentJson = new(JsonSerializerDefaults.Web)
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public DocGrokClient(HttpClient http, ILogger<DocGrokClient> logger)
    {
        _http = http;
        _logger = logger;
    }

    private static Dictionary<string, object?> RequestFor(string modelOrPipeline)
        => new() { [modelOrPipeline.StartsWith("mdl-") ? "model_id" : "pipeline"] = modelOrPipeline };

    private async Task<JsonDocument> PostAsync(
        string path, Dictionary<string, object?> request, CancellationToken ct, bool inline = false)
    {
        for (var attempt = 1; ; attempt++)
        {
            TimeSpan delay;
            try
            {
                using var response = inline
                    ? await _http.PostAsJsonAsync(path, request, InlineDocumentJson, ct)
                    : await _http.PostAsJsonAsync(path, request, ct);
                var body = await response.Content.ReadAsStringAsync(ct);
                if (response.IsSuccessStatusCode) return JsonDocument.Parse(body);
                var status = (int)response.StatusCode;
                if (!IsTransientStatus(status) || attempt >= MaxAttempts)
                    throw new EmbeddingClientException(status, body,
                        $"DocGrok {path} returned {status} after {attempt} attempt(s): {body[..Math.Min(500, body.Length)]}");
                var retryAfter = response.Headers.RetryAfter?.Delta
                    ?? (response.Headers.RetryAfter?.Date - DateTimeOffset.UtcNow);
                delay = TimeSpan.FromSeconds(Math.Clamp(
                    retryAfter?.TotalSeconds ?? Math.Pow(2, attempt), 0, 30));
            }
            catch (HttpRequestException) when (attempt < MaxAttempts)
            {
                delay = TimeSpan.FromSeconds(Math.Pow(2, attempt));
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested && attempt < MaxAttempts)
            {
                delay = TimeSpan.FromSeconds(Math.Pow(2, attempt));
            }
            catch (OperationCanceledException ex) when (!ct.IsCancellationRequested)
            {
                throw new TimeoutException($"DocGrok {path} timed out after {attempt} attempts", ex);
            }
            _logger.LogWarning("DocGrok {Path} transient failure; retry {Attempt}/{Max} in {Delay}s",
                path, attempt + 1, MaxAttempts, delay.TotalSeconds);
            await DelayAsync(delay, ct);
        }
    }

    internal static bool IsTransientStatus(int status) => status is 408 or 429 || status >= 500;
    internal static bool IsInputError(int status) => status is 400 or 413 or 415 or 422;

    private static float[] Vector(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Array && value.GetArrayLength() > 0
            && value[0].ValueKind == JsonValueKind.Array)
            value = value[0];
        if (value.ValueKind != JsonValueKind.Array)
            throw new InvalidOperationException("DocGrok embedding must be an array");
        var vector = value.EnumerateArray().Select(element => element.GetSingle()).ToArray();
        if (vector.Length == 0 || vector.Any(number => !float.IsFinite(number)))
            throw new InvalidOperationException("DocGrok returned an empty or non-finite embedding");
        return vector;
    }

    private static List<(string ChunkText, float[] Embedding)> Chunks(JsonElement root)
    {
        if (!root.TryGetProperty("chunks", out var chunks) || chunks.ValueKind != JsonValueKind.Array)
            throw new InvalidOperationException("Document processor response must contain a chunks array");
        if (root.TryGetProperty("dim_skipped", out var dimSkipped) && dimSkipped.GetInt32() > 0)
            throw new InvalidOperationException("Document processor dropped chunks with invalid dimensions");
        if (chunks.GetArrayLength() == 0)
        {
            if (root.TryGetProperty("skipped", out var skipped) && skipped.ValueKind == JsonValueKind.True
                && root.TryGetProperty("skip_reason", out var reason)
                && reason.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(reason.GetString()))
                return [];
            throw new InvalidOperationException("Document processor returned no chunks without an explicit skip reason");
        }
        return chunks.EnumerateArray().Select(chunk => (
            chunk.TryGetProperty("text", out var text) ? text.GetString() ?? "" : "",
            Vector(chunk.GetProperty("embedding")))).ToList();
    }

    public async Task<List<float[]>> EmbedBatchAsync(
        string modelOrPipeline, List<string> texts, CancellationToken ct)
    {
        var request = RequestFor(modelOrPipeline);
        request["texts"] = texts;
        using var json = await PostAsync("/embed/batch", request, ct);
        var results = json.RootElement.GetProperty("outputs").EnumerateArray().Select(Vector).ToList();
        if (results.Count != texts.Count)
            throw new InvalidOperationException($"DocGrok returned {results.Count} vectors for {texts.Count} texts");
        return results;
    }

    public async Task<List<(string ChunkText, float[] Embedding)>> EmbedBlobAsync(
        string modelOrPipeline, string blobAccountUrl, string? blobConnectionString,
        string blobContainer, string blobName, CancellationToken ct)
    {
        var request = RequestFor(modelOrPipeline);
        request["blob_name"] = blobName;
        request["blob_container"] = blobContainer;
        if (!string.IsNullOrEmpty(blobConnectionString)) request["blob_connection_string"] = blobConnectionString;
        else request["blob_account_url"] = blobAccountUrl;
        using var json = await PostAsync("/embed", request, ct);
        if (json.RootElement.TryGetProperty("chunks", out _)) return Chunks(json.RootElement);
        var vectors = json.RootElement.GetProperty("outputs").EnumerateArray().Select(Vector).ToList();
        if (vectors.Count == 0) throw new InvalidOperationException("Blob processing returned no embeddings");
        return vectors.Select(vector => ("", vector)).ToList();
    }

    public async Task<List<(string ChunkText, float[] Embedding)>> EmbedDataAsync(
        string modelOrPipeline, byte[] data, string fileName, CancellationToken ct)
    {
        if (data.LongLength > 50L * 1024 * 1024)
            throw new ArgumentOutOfRangeException(nameof(data), "SharePoint input exceeds 50 MiB");
        var request = RequestFor(modelOrPipeline);
        request["data"] = Convert.ToBase64String(data);
        request["requestId"] = fileName;
        request["source_name"] = fileName;
        using var json = await PostAsync("/process", request, ct, inline: true);
        var chunks = Chunks(json.RootElement);
        if (chunks.Count == 0) _logger.LogInformation("Document processor explicitly skipped {File}: {Reason}",
            fileName, json.RootElement.GetProperty("skip_reason").GetString());
        return chunks;
    }

    public async Task<List<float[]>> EmbedBlobBatchAsync(
        string modelOrPipeline, string blobAccountUrl, string blobContainer,
        List<string> blobNames, CancellationToken ct)
    {
        if (blobNames.Count == 0) return [];
        var request = RequestFor(modelOrPipeline);
        request["blob_names"] = blobNames;
        request["blob_account_url"] = blobAccountUrl;
        request["blob_container"] = blobContainer;
        using var json = await PostAsync("/embed", request, ct);
        var vectors = Chunks(json.RootElement).Select(chunk => chunk.Embedding).ToList();
        if (vectors.Count != blobNames.Count)
            throw new InvalidOperationException($"DocGrok returned {vectors.Count} vectors for {blobNames.Count} blobs");
        return vectors;
    }
}
