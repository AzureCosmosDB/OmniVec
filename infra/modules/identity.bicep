// User-assigned managed identity for workload identity federation
param name string
param location string
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
output identityId string = identity.id
output tenantId string = identity.properties.tenantId
