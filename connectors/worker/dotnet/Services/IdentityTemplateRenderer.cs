using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using OmniVec.Worker.Models;

namespace OmniVec.Worker.Services;

internal static class IdentityTemplateRenderer
{
    private static string Hash(string value, int? length = null)
    {
        var rendered = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
        return length is null ? rendered : rendered[..length.Value];
    }

    private static Dictionary<string, string> Values(EmbeddingMessage message, string sourcePartition) => new()
    {
        ["source"] = message.SourceRef,
        ["source_ref"] = message.SourceRef,
        ["source_hash"] = Hash(message.SourceRef),
        ["source_id"] = message.SourceId,
        ["source_partition"] = sourcePartition,
        ["pipeline"] = message.PipelineId,
        ["pipeline_hash"] = Hash(message.PipelineId, 16),
        ["destination"] = message.DestinationId,
        ["destination_hash"] = Hash(message.DestinationId, 16),
        ["model"] = message.DocgrokPipeline,
        ["model_hash"] = Hash(message.DocgrokPipeline, 16),
        ["job"] = message.MessageId,
    };

    private static string Render(string pattern, IReadOnlyDictionary<string, string> values, string label)
    {
        var variables = Regex.Matches(pattern, @"\{([^{}]+)\}").Select(m => m.Groups[1].Value).ToList();
        if (variables.Any(variable => !values.ContainsKey(variable))
            || Regex.Replace(pattern, @"\{[^{}]+\}", "").IndexOfAny(['{', '}']) >= 0)
            throw new ArgumentException($"{label} contains unsupported variables");
        return Regex.Replace(pattern, @"\{([^{}]+)\}", match => values[match.Groups[1].Value]);
    }

    internal static string RenderDocumentId(EmbeddingMessage message)
    {
        var pattern = string.IsNullOrWhiteSpace(message.DocIdPattern)
            ? "{source_hash}-{pipeline}"
            : message.DocIdPattern;
        var rendered = Render(pattern, Values(message, message.PartitionKeyValue), "doc_id_pattern");
        return ValidateDocumentId(rendered);
    }

    internal static string RenderPartitionKey(EmbeddingMessage message)
    {
        var sourcePartition = message.PartitionKeyValue;
        var pattern = string.IsNullOrWhiteSpace(message.PartitionKeyPattern)
            ? "{source_partition}"
            : message.PartitionKeyPattern;
        var rendered = Render(pattern, Values(message, sourcePartition), "partition_key_pattern");
        if (string.IsNullOrWhiteSpace(rendered) || rendered.Any(char.IsControl)
            || Encoding.UTF8.GetByteCount(rendered) > 2048)
            throw new ArgumentException("Rendered partition_key_pattern must be 1-2048 UTF-8 bytes without control characters");
        return rendered;
    }

    internal static string ValidateDocumentId(string rendered)
    {
        if (rendered.Contains('{') || rendered.Contains('}')
            || rendered.IndexOfAny(['/', '\\', '?', '#']) >= 0
            || rendered.Any(char.IsControl)
            || string.IsNullOrWhiteSpace(rendered)
            || Encoding.UTF8.GetByteCount(rendered) > 1023)
            throw new ArgumentException("Rendered doc_id_pattern is not a valid Cosmos document ID");
        return rendered;
    }
}
