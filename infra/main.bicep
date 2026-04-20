// OmniVec — Main Bicep orchestrator
// Deploys all Azure resources for a fully self-contained OmniVec environment

targetScope = 'subscription'

// =============================================================================
// PARAMETERS
// =============================================================================

@minLength(1)
@maxLength(20)
param environmentName string

param location string

@description('Kubernetes version for AKS')
param kubernetesVersion string = '1.33'

@description('VM size for AKS system node pool')
param systemNodeVmSize string = 'Standard_D4s_v3'

@description('Initial system node count. Kept as string so azd env substitution (which always produces a string, even for ints) never breaks ARM type-coercion. Parsed/defaulted below.')
param systemNodeCount string = '2'

@description('VM size for AKS GPU node pool')
param gpuNodeVmSize string = 'Standard_NC6s_v3'

@description('Initial GPU node count (0 to skip GPU pool). String for the same reason as systemNodeCount.')
param gpuNodeCount string = '0'

@description('Enable blob storage as a document source (creates Storage Account, Service Bus, Event Grid). String form: "true"/"false"/"1"/"0"/"yes"/"no" (case-insensitive). Empty -> defaults to true.')
param enableBlobSource string = 'true'

// =============================================================================
// PARAMETER NORMALIZATION
// Defense in depth: azd env values can carry BOM (U+FEFF), CR, or whitespace
// from copy-paste or editor mis-saves. Hooks sanitize too, but Bicep
// normalizes one more time before int/bool coercion so a stray byte never
// manifests as an "InvalidTemplate" 10 minutes into a deploy.
// =============================================================================

var _sysCountClean  = trim(replace(replace(systemNodeCount, '\u{FEFF}', ''), '\r', ''))
var _gpuCountClean  = trim(replace(replace(gpuNodeCount,    '\u{FEFF}', ''), '\r', ''))
var _blobClean      = toLower(trim(replace(replace(enableBlobSource, '\u{FEFF}', ''), '\r', '')))

var systemNodeCountInt   = empty(_sysCountClean) ? 2 : int(_sysCountClean)
var gpuNodeCountInt      = empty(_gpuCountClean) ? 0 : int(_gpuCountClean)
// Empty -> preserves prior default (true). Explicit false-tokens -> false. Anything else -> true.
var enableBlobSourceBool = empty(_blobClean)
  ? true
  : !(_blobClean == 'false' || _blobClean == '0' || _blobClean == 'no')

// =============================================================================
// NAMING (must be computed before resource group to avoid circular dependency)
// =============================================================================

var rgName = 'rg-omnivec-${environmentName}'
// Construct the RG resource ID deterministically (avoids circular ref with tags → rg)
var rgResourceId = '${subscription().id}/resourceGroups/${rgName}'
var resourceToken = toLower(uniqueString(subscription().id, rgResourceId, environmentName))
var installationId = '${environmentName}-${resourceToken}'
var prefix = 'omnivec'
var identityName = '${prefix}-identity-${resourceToken}'
var tags = {
  'azd-env-name': environmentName
  'omnivec-instance': installationId
  Project: 'OmniVec'
  ManagedBy: 'azd-bicep'
}

// =============================================================================
// RESOURCE GROUP
// =============================================================================

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: rgName
  location: location
  tags: tags
}

// =============================================================================
// MODULES — deployed in dependency order
// =============================================================================

// 1. Managed Identity (no dependencies)
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: identityName
    location: location
    tags: tags
  }
}

// 2. CosmosDB (depends on identity for RBAC)
module cosmosdb 'modules/cosmosdb.bicep' = {
  name: 'cosmosdb'
  scope: rg
  params: {
    accountName: '${prefix}-cosmos-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// 3. Key Vault (stores model API keys securely)
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    vaultName: '${prefix}-kv-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// 4. Storage Account (only when blob source is enabled)
module storage 'modules/storage.bicep' = if (enableBlobSourceBool) {
  name: 'storage'
  scope: rg
  params: {
    accountName: 'ovstore${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// 4. Service Bus (only when blob source is enabled)
module servicebus 'modules/servicebus.bicep' = if (enableBlobSourceBool) {
  name: 'servicebus'
  scope: rg
  params: {
    namespaceName: '${prefix}-sb-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// 5. Event Grid (only when blob source is enabled)
module eventgrid 'modules/eventgrid.bicep' = if (enableBlobSourceBool) {
  name: 'eventgrid'
  scope: rg
  params: {
    topicName: '${prefix}-blob-events-${resourceToken}'
    location: location
    tags: tags
    storageAccountId: storage!.outputs.accountId
    identityPrincipalId: identity.outputs.principalId
  }
}

// 6. Application Insights (telemetry)
module appinsights 'modules/appinsights.bicep' = {
  name: 'appinsights'
  scope: rg
  params: {
    workspaceName: '${prefix}-logs-${resourceToken}'
    appInsightsName: '${prefix}-insights-${resourceToken}'
    location: location
    tags: tags
    principalId: aks.outputs.kubeletObjectId
  }
}

// 7. ACR (no dependencies)
module acr 'modules/acr.bicep' = {
  name: 'acr'
  scope: rg
  params: {
    registryName: '${prefix}acr${resourceToken}'
    location: location
    tags: tags
  }
}

// 7. AKS (depends on ACR)
module aks 'modules/aks.bicep' = {
  name: 'aks'
  scope: rg
  params: {
    clusterName: '${prefix}-aks-${resourceToken}'
    location: location
    tags: tags
    kubernetesVersion: kubernetesVersion
    systemNodeVmSize: systemNodeVmSize
    systemNodeCount: systemNodeCountInt
    gpuNodeVmSize: gpuNodeVmSize
    gpuNodeCount: gpuNodeCountInt
  }
}

// =============================================================================
// AcrPull role for AKS kubelet identity
// =============================================================================

module acrPullRole 'modules/acr-pull-role.bicep' = {
  name: 'acr-pull-role'
  scope: rg
  params: {
    acrName: '${prefix}acr${resourceToken}'
    aksClusterName: '${prefix}-aks-${resourceToken}'
    kubeletObjectId: aks.outputs.kubeletObjectId
  }
}

// =============================================================================
// FEDERATED IDENTITY CREDENTIALS
// Links AKS OIDC issuer to managed identity for workload identity
// =============================================================================

module federation 'modules/federation.bicep' = {
  name: 'federation'
  scope: rg
  params: {
    identityName: identityName
    oidcIssuerUrl: aks.outputs.oidcIssuerUrl
  }
  dependsOn: [identity]
}

// =============================================================================
// OUTPUTS — consumed by postprovision hook
// =============================================================================

output AZURE_OMNIVEC_INSTANCE_ID string = installationId
output AZURE_AKS_CLUSTER_NAME string = aks.outputs.clusterName
output AZURE_ACR_LOGIN_SERVER string = acr.outputs.loginServer
output AZURE_ACR_NAME string = acr.outputs.registryName
output AZURE_COSMOS_ENDPOINT string = cosmosdb.outputs.endpoint
output AZURE_COSMOS_ACCOUNT_NAME string = cosmosdb.outputs.accountName
output AZURE_ENABLE_BLOB_SOURCE string = enableBlobSourceBool ? 'true' : 'false'
output AZURE_STORAGE_ACCOUNT_NAME string = enableBlobSourceBool ? storage!.outputs.accountName : ''
output AZURE_STORAGE_BLOB_ENDPOINT string = enableBlobSourceBool ? storage!.outputs.primaryBlobEndpoint : ''
output AZURE_STORAGE_QUEUE_ENDPOINT string = enableBlobSourceBool ? storage!.outputs.queueEndpoint : ''
output AZURE_SERVICEBUS_NAMESPACE string = enableBlobSourceBool ? servicebus!.outputs.namespaceName : ''
output AZURE_SERVICEBUS_ENDPOINT string = enableBlobSourceBool ? servicebus!.outputs.endpoint : ''
output AZURE_IDENTITY_CLIENT_ID string = identity.outputs.clientId
output AZURE_KEYVAULT_URI string = keyvault.outputs.vaultUri
output AZURE_APPINSIGHTS_CONNECTION_STRING string = appinsights.outputs.connectionString
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = appinsights.outputs.workspaceId
output AZURE_RESOURCE_GROUP string = rg.name
