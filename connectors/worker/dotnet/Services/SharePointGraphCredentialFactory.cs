using Azure.Core;
using Azure.Identity;

namespace OmniVec.Worker.Services;

internal static class SharePointGraphCredentialFactory
{
    public static TokenCredential Create(string? tenantId, string? clientId)
    {
        tenantId = tenantId?.Trim();
        clientId = clientId?.Trim();
        if (string.IsNullOrEmpty(tenantId) && string.IsNullOrEmpty(clientId))
            return new DefaultAzureCredential();
        if (!Guid.TryParse(tenantId, out _) || !Guid.TryParse(clientId, out _))
            throw new InvalidOperationException(
                "SharePoint message requires valid graph tenant and client ID GUIDs together");

        var tokenFile = Environment.GetEnvironmentVariable("AZURE_FEDERATED_TOKEN_FILE")?.Trim();
        if (string.IsNullOrEmpty(tokenFile))
            throw new InvalidOperationException(
                "SharePoint cross-tenant identity requires the Azure Workload Identity federated token file");

        return new WorkloadIdentityCredential(new WorkloadIdentityCredentialOptions
        {
            TenantId = tenantId,
            ClientId = clientId,
            TokenFilePath = tokenFile,
        });
    }
}
