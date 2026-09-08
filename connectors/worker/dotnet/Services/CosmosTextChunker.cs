using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Models;

namespace OmniVec.Worker.Services;

internal static class CosmosTextChunker
{
    internal static readonly HashSet<string> Reserved = new(StringComparer.Ordinal)
    {
        "id", "source_id", "source_ref", "pipeline_id", "pipeline_name", "pipeline_generation",
        "embedded_at", "content_hash", "embedding_dims", "chunk_index", "chunk_count", "ttl",
        "_omnivec_sync", "chunk_source_partition",
    };

    internal static void Validate(TextChunkConfig config)
    {
        if (config.Size < 100 || config.Overlap < 0 || config.Overlap >= config.Size
            || config.Unit is not ("chars" or "tokens") || config.Extra?.Count > 0)
            throw new ArgumentException("Invalid Cosmos chunk config: size >= 100, 0 <= overlap < size, unit chars/tokens; no unknown options");
        if (string.IsNullOrWhiteSpace(config.TextField) || config.TextField.Contains('/')
            || Reserved.Contains(config.TextField) || config.TextField.StartsWith('_'))
            throw new ArgumentException("Chunk text_field conflicts with Cosmos metadata");
        var variables = Regex.Matches(config.DocIdPattern, @"\{([^{}]+)\}").Select(m => m.Groups[1].Value).ToList();
        if (!variables.Contains("chunk") || variables.Any(v => v is not
                ("source" or "source_ref" or "source_hash" or "chunk" or "pipeline" or "pipeline_hash"))
            || Regex.Replace(config.DocIdPattern, @"\{[^{}]+\}", "").IndexOfAny(['{', '}']) >= 0)
            throw new ArgumentException("Chunk doc_id_pattern must contain {chunk} and only supported variables");
    }

    internal static List<string> Split(string text, TextChunkConfig config)
    {
        Validate(config);
        if (string.IsNullOrWhiteSpace(text)) return [];
        // Python chunker uses whitespace words, not model/BPE tokens.
        var units = config.Unit == "tokens"
            ? Regex.Split(text.Trim(), @"\s+")
            : text.EnumerateRunes().Select(r => r.ToString()).ToArray();
        if (units.Length <= config.Size) return [text];
        var chunks = new List<string>();
        for (int start = 0; start < units.Length;)
        {
            var end = Math.Min(start + config.Size, units.Length);
            if (config.Unit == "chars" && end < units.Length)
            {
                var search = start + (int)(config.Size * 0.8);
                int Find(string first, string second)
                {
                    for (var i = end - 2; i >= search; i--)
                        if (units[i] == first && units[i + 1] == second) return i;
                    return -1;
                }
                var boundary = Find("\n", "\n");
                if (boundary <= search)
                    boundary = new[] { ".", "!", "?" }.SelectMany(p => new[] { " ", "\n" }.Select(s => Find(p, s))).Max();
                if (boundary > search) end = boundary + 2;
            }
            var chunk = string.Join(config.Unit == "tokens" ? " " : "", units[start..end]).Trim();
            if (chunk.Length > 0) chunks.Add(chunk);
            if (end == units.Length) break;
            // Large overlaps and early sentence breaks must still make progress.
            start = Math.Max(start + 1, end - config.Overlap);
        }
        return chunks;
    }

    private static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();

    internal static string DocId(EmbeddingMessage message, TextChunkConfig config, int index)
    {
        Validate(config);
        var source = message.SourceRef.Replace('\\', '/').Split('/').Last();
        var dot = source.LastIndexOf('.');
        if (dot > 0) source = source[..dot];
        var values = new Dictionary<string, string>
        {
            ["source"] = source, ["source_ref"] = message.SourceRef.Replace('/', '-').Replace('\\', '-'),
            ["source_hash"] = Hash(message.SourceRef)[..12], ["chunk"] = index.ToString("D3", CultureInfo.InvariantCulture),
            ["pipeline"] = message.PipelineId, ["pipeline_hash"] = Hash(message.PipelineId)[..8],
        };
        var suffix = Regex.Replace(config.DocIdPattern, @"\{([^{}]+)\}", m => values[m.Groups[1].Value]);
        // Always namespace user patterns: identical source refs across registrations,
        // source partitions and pipelines must not overwrite one another in /id.
        var identity = Hash(JsonSerializer.Serialize(new[] {
            message.PipelineId, message.SourceId, message.PartitionKeyValue, message.SourceRef }));
        var id = $"ovc-{identity}-{suffix}";
        if (id.IndexOfAny(['/', '\\', '?', '#']) >= 0 || id.Any(char.IsControl) || Encoding.UTF8.GetByteCount(id) > 1023)
            throw new ArgumentException("Rendered chunk doc_id_pattern is not a valid Cosmos ID (maximum 1023 UTF-8 bytes)");
        return id;
    }

    internal static EmbeddingResult Result(EmbeddingMessage msg, TextChunkConfig config,
        string text, float[] vector, int index, int count)
    {
        if (vector.Length == 0 || vector.Any(v => !float.IsFinite(v)))
            throw new InvalidOperationException("Chunk embedding is empty or non-finite");
        return new(DocId(msg, config, index), msg.SourceRef, vector, msg.ContentHash,
            msg.PartitionKeyValue, msg.PipelineId, msg.PipelineName, msg.PipelineGeneration, text,
            SourceId: msg.SourceId, StoreContent: config.StoreText, ContentField: config.TextField,
            MetadataFields: msg.MetadataFields, ChunkIndex: index, ChunkCount: count);
    }
}
