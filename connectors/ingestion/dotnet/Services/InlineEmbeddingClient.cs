using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace OmniVec.ChangeFeed.Services;

internal static class InlineEmbeddingClient
{
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
        foreach (var chunk in texts.Chunk(50))
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
                if (outputs.GetArrayLength() != chunk.Length)
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
