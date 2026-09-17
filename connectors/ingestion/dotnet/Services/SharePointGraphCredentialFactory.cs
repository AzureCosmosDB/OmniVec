using Azure.Core;
using Azure.Identity;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

internal static class SharePointGraphCredentialFactory
{
    public static TokenCredential Create(SharePointPipelineIdentity? identity)
    {
        if (identity is null)
            return new DefaultAzureCredential();
        if (!Guid.TryParse(identity.TenantId, out _) || !Guid.TryParse(identity.ClientId, out _))
            throw new InvalidOperationException("SharePoint pipeline identity requires valid tenant_id and client_id GUIDs");

        var tokenFile = Environment.GetEnvironmentVariable("AZURE_FEDERATED_TOKEN_FILE")?.Trim();
        if (string.IsNullOrEmpty(tokenFile))
            throw new InvalidOperationException(
                "SharePoint cross-tenant identity requires the Azure Workload Identity federated token file");

        return new WorkloadIdentityCredential(new WorkloadIdentityCredentialOptions
        {
            TenantId = identity.TenantId,
            ClientId = identity.ClientId,
            TokenFilePath = tokenFile,
        });
    }
}
