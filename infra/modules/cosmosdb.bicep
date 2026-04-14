// CosmosDB — Serverless NoSQL account + omnivec database + metadata container + SQL RBAC
param accountName string
param location string
param tags object = {}
param identityPrincipalId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    enableAutomaticFailover: false
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      { name: 'EnableServerless' }
      { name: 'EnableNoSQLVectorSearch' }
    ]
  }
  tags: tags
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: 'omnivec'
  properties: {
    resource: {
      id: 'omnivec'
    }
  }
}

resource metadataContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'metadata'
  properties: {
    resource: {
      id: 'metadata'
      partitionKey: {
        paths: ['/doc_type']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [
          { path: '/*' }
        ]
      }
    }
  }
}

// Built-in "Cosmos DB Built-in Data Contributor" role (SQL RBAC — data operations)
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource sqlRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: account
  name: guid(account.id, identityPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: identityPrincipalId
    scope: account.id
  }
}

// "Cosmos DB Account Reader Role" (ARM RBAC — required for SDK initialization / readMetadata)
var accountReaderRoleId = 'fbdf93bf-df7d-467e-a4d2-9458aa1360c8'

resource cosmosAccountReaderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, identityPrincipalId, accountReaderRoleId)
  scope: account
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', accountReaderRoleId)
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = account.properties.documentEndpoint
output accountName string = account.name
output accountId string = account.id
