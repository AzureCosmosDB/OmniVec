// Key Vault — stores model API keys securely with RBAC authorization
// Uses createMode 'recover' when a soft-deleted vault with the same name exists,
// avoiding the "vault already exists in deleted state" conflict on re-deployment.
param vaultName string
param location string
param tags object = {}
param identityPrincipalId string

@description('Set to true when a soft-deleted vault with this name exists and should be recovered instead of created fresh.')
param recoverSoftDeleted bool = false

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // Recover from soft-delete if a previous vault exists, otherwise create fresh
    createMode: recoverSoftDeleted ? 'recover' : 'default'
    // Note: enablePurgeProtection should be true in production
    // Disabled here for dev/test to allow clean teardown with azd down --purge
  }
  tags: tags
}

// Key Vault Secrets Officer — managed identity can get/set/delete secrets
var secretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identityPrincipalId, secretsOfficerRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsOfficerRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output vaultUri string = vault.properties.vaultUri
output vaultName string = vault.name
