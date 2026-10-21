using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace OmniVec.ChangeFeed.Services;

internal static class InlineEmbeddingClient
{
    private const int MaxRequestBytes = 2 * 1024 * 1024;
    private const int MaxBatchTexts = 50;

    private static IEnumerable<List<string>> RequestBatches(string model, List<string> texts)
    {
        var envelopeBytes = JsonSerializer.SerializeToUtf8Bytes(new Dictionary<string, object>
        {
            [model.StartsWith("mdl-") ? "model_id" : "pipeline"] = model,
            ["texts"] = Array.Empty<string>(),
        }).Length;
        var batch = new List<string>();
        var bytes = envelopeBytes;
        foreach (var text in texts)
        {
            var textBytes = JsonEncodedText.Encode(text).EncodedUtf8Bytes.Length + 2;
            if (envelopeBytes + (long)textBytes > MaxRequestBytes)
                throw new InvalidOperationException(
                    "Inline embedding text exceeds the 2 MiB JSON request limit; use chunk processing");
            if (batch.Count > 0 && (batch.Count == MaxBatchTexts
                || bytes + (long)textBytes + 1 > MaxRequestBytes))
            {
                yield return batch;
                batch = new List<string>();
                bytes = envelopeBytes;
            }
            bytes += textBytes + (batch.Count > 0 ? 1 : 0);
            batch.Add(text);
        }
        if (batch.Count > 0)
            yield return batch;
    }

    internal static bool HasCurrentEmbedding(Dictionary<string, object?> row,
        OmniVec.ChangeFeed.Models.Pipeline pipeline, string contentHash)
    {
        var metadata = new Newtonsoft.Json.Linq.JObject();
        foreach (var field in new[] { "pipeline_id", "content_hash", "embedded_at" })
            if (row.TryGetValue(field, out var value) && value is not null)
                metadata[field] = Newtonsoft.Json.Linq.JToken.FromObject(value);
        return SourceWatcher.HasCurrentEmbedding(metadata, pipeline, contentHash);
    }

    internal static async Task<List<float[]>> EmbedAsync(HttpClient client, string model,
        List<string> texts, CancellationToken ct, Func<TimeSpan, CancellationToken, Task>? delay = null)
    {
        delay ??= Task.Delay;
        var vectors = new List<float[]>();
        foreach (var chunk in RequestBatches(model, texts))
        {
            JsonDocument response;
            for (var attempt = 1; ; attempt++)
            {
                try
                {
                    var payload = new Dictionary<string, object>
                    {
                        [model.StartsWith("mdl-") ? "model_id" : "pipeline"] = model,
                        ["texts"] = chunk,
                    };
                    using var result = await client.PostAsJsonAsync("/embed/batch", payload, ct);
                    result.EnsureSuccessStatusCode();
                    response = JsonDocument.Parse(await result.Content.ReadAsStringAsync(ct));
                    break;
                }
                catch (HttpRequestException ex) when (attempt < 4
                    && (ex.StatusCode is null or HttpStatusCode.TooManyRequests or HttpStatusCode.RequestTimeout
                        || (int?)ex.StatusCode >= 500))
                { await delay(TimeSpan.FromSeconds(Math.Pow(2, attempt)), ct); }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested && attempt < 4)
                { await delay(TimeSpan.FromSeconds(Math.Pow(2, attempt)), ct); }
                catch (OperationCanceledException ex) when (!ct.IsCancellationRequested)
                { throw new TimeoutException($"Inline embedding timed out after {attempt} attempts", ex); }
            }
            using (response)
            {
                var outputs = response.RootElement.GetProperty("outputs");
                if (outputs.GetArrayLength() != chunk.Count)
                    throw new InvalidOperationException("Incomplete inline embeddings; retaining source checkpoint");
                foreach (var output in outputs.EnumerateArray())
                {
                    var value = output.GetArrayLength() > 0 && output[0].ValueKind == JsonValueKind.Array ? output[0] : output;
                    var vector = value.EnumerateArray().Select(element => element.GetSingle()).ToArray();
                    if (vector.Length == 0 || vector.Any(number => !float.IsFinite(number)))
                        throw new InvalidOperationException("Invalid inline embedding; retaining source checkpoint");
                    vectors.Add(vector);
                }
            }
        }
        return vectors;
    }
}
