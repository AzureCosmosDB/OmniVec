// Service Bus namespace + jobs queue + RBAC
param namespaceName string
param location string
param tags object = {}
param identityPrincipalId string
@description('Provision a separate stage-1 pipeline lane topic with no catch-all subscription. Does not activate producers or workers.')
param enablePipelineLanes bool = false

@allowed([
  'Standard'
  'Premium'
])
param skuName string = 'Standard'

@allowed([
  1
  2
  4
  8
  16
])
param premiumMessagingUnits int = 1

resource sbNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: namespaceName
  location: location
  sku: skuName == 'Premium'
    ? {
        name: 'Premium'
        tier: 'Premium'
        capacity: premiumMessagingUnits
      }
    : {
        name: 'Standard'
        tier: 'Standard'
      }
  properties: {
    disableLocalAuth: true
  }
  tags: tags
}

resource jobsQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'jobs'
  properties: {
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    deadLetteringOnMessageExpiration: true
  }
}

// Blob events queue — Event Grid subscriptions on blob storage accounts deliver
// BlobCreated/BlobDeleted/BlobRenamed events here. BlobEventConsumer in the
// .NET ingestion service consumes and fans out to active pipelines.
resource blobEventsQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'blob-events'
  properties: {
    maxDeliveryCount: 10
    lockDuration: 'PT5M'
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P1D'
  }
}

// Embeddings topic — changefeed publishes, .NET worker subscribes
resource embeddingsTopic 'Microsoft.ServiceBus/namespaces/topics@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'embeddings'
  properties: {
    maxSizeInMegabytes: 5120
    defaultMessageTimeToLive: 'P1D'
  }
}

resource workerSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2022-10-01-preview' = {
  parent: embeddingsTopic
  name: 'worker'
  properties: {
    maxDeliveryCount: 10
    lockDuration: 'PT5M'
    deadLetteringOnMessageExpiration: true
  }
}

// The 2026-01-01 ARM API supports topic UserMetadata; the legacy 2022 schema does not.
resource pipelineLanesTopic 'Microsoft.ServiceBus/namespaces/topics@2026-01-01' = if (enablePipelineLanes) {
  parent: sbNamespace
  name: 'embeddings-lanes-v1'
  properties: {
    maxSizeInMegabytes: 5120
    defaultMessageTimeToLive: 'P1D'
    userMetadata: 'omnivec-pipeline-lanes-v1'
  }
}

// Azure Service Bus Data Owner role
var serviceBusDataOwnerRoleId = '090c5cfd-751d-490a-894a-3ce6f1109419'

resource sbDataOwnerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sbNamespace.id, identityPrincipalId, serviceBusDataOwnerRoleId)
  scope: sbNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataOwnerRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output namespaceName string = sbNamespace.name
output endpoint string = '${sbNamespace.name}.servicebus.windows.net'
output skuName string = skuName
output messagingUnits int = skuName == 'Premium' ? premiumMessagingUnits : 0
output blobEventsQueueName string = blobEventsQueue.name
output blobEventsQueueResourceId string = blobEventsQueue.id
