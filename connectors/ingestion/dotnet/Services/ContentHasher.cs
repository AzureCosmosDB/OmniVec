using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace OmniVec.ChangeFeed.Services;

/// <summary>
/// SHA256 content hash — matches the Python logic in controller.py and cosmosdb_connector.py.
/// Used to skip documents whose content hasn't changed since last embedding.
/// </summary>
public class ContentHasher
{
    public string ComputeHash(string content)
    {
        var bytes = Encoding.UTF8.GetBytes(content);
        var hash = SHA256.HashData(bytes);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    /// <summary>
    /// Returns true if the document already has a content_hash that matches the current content.
    /// This means the document was already embedded and content hasn't changed — skip it.
    /// </summary>
    public bool IsContentUnchanged(JsonElement document, string contentField)
    {
        if (!document.TryGetProperty("content_hash", out var existingHash)
            || existingHash.ValueKind != JsonValueKind.String)
            return false;

        if (!document.TryGetProperty(contentField, out var contentElement)
            || contentElement.ValueKind != JsonValueKind.String)
            return false;

        var content = contentElement.GetString();
        if (string.IsNullOrEmpty(content))
            return false;

        var currentHash = ComputeHash(content);
        return currentHash == existingHash.GetString();
    }
}
