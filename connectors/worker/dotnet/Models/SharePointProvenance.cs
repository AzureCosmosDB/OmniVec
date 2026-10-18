namespace OmniVec.Worker.Models;

public sealed record SharePointProvenance(
    string ResourceTenantId,
    string SiteId,
    string DriveId,
    string ItemId,
    string? SourceETag)
{
    public static SharePointProvenance FromMessage(EmbeddingMessage message)
    {
        var deleted = string.Equals(message.MessageType, "delete", StringComparison.OrdinalIgnoreCase);
        var provenance = new SharePointProvenance(
            message.SharePointResourceTenantId ?? "", message.SharePointSiteId ?? "",
            message.SharePointDriveId ?? "", message.SharePointItemId ?? "",
            deleted ? null : message.SharePointETag);
        provenance.Validate(requireETag: !deleted);
        return provenance with { ResourceTenantId = Guid.Parse(provenance.ResourceTenantId).ToString() };
    }

    public void Validate(bool requireETag)
    {
        if (!Guid.TryParse(ResourceTenantId, out var tenantId) || tenantId == Guid.Empty
            || string.IsNullOrWhiteSpace(SiteId) || SiteId.Length > 512
            || string.IsNullOrWhiteSpace(DriveId) || DriveId.Length > 256
            || string.IsNullOrWhiteSpace(ItemId) || ItemId.Length > 256
            || (requireETag && string.IsNullOrWhiteSpace(SourceETag))
            || SourceETag?.Length > 2048)
            throw new InvalidOperationException("Missing or invalid SharePoint provenance; reindex legacy messages");
    }
}
