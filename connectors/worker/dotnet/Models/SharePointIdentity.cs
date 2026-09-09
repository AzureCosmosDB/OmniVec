using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace OmniVec.Worker.Models;

public static class SharePointIdentity
{
    public static string Create(EmbeddingMessage message)
    {
        var parts = new[] { message.SourceId, message.SharePointSiteId,
            message.SharePointDriveId, message.SharePointItemId, message.PipelineId };
        if (parts.Any(string.IsNullOrWhiteSpace))
            throw new InvalidOperationException("SharePoint synchronization requires source, site, drive, item and pipeline IDs");
        return "sp-" + Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(JsonSerializer.Serialize(parts)))).ToLowerInvariant();
    }
}
