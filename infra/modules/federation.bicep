// Federated identity credentials for workload identity
// Links AKS service accounts to the managed identity via OIDC

param identityName string
param oidcIssuerUrl string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

// Federated credential for omnivec namespace (omnivec-api service account)
resource fedCredOmnivec 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'omnivec-api-federation'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:omnivec:omnivec-api'
    audiences: ['api://AzureADTokenExchange']
  }
}

// Federated credential for docgrok namespace (docgrok-sa service account)
resource fedCredDocgrok 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'docgrok-sa-federation'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:omnivec:docgrok-sa'
    audiences: ['api://AzureADTokenExchange']
  }
  dependsOn: [fedCredOmnivec]
}
