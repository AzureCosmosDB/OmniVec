// Event Grid — System topic on storage account + BlobCreated → Storage Queue subscription
param topicName string
param location string
param tags object = {}
param storageAccountId string
param identityPrincipalId string

resource systemTopic 'Microsoft.EventGrid/systemTopics@2023-12-15-preview' = {
  name: topicName
  location: location
  properties: {
    source: storageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
  tags: tags
}

resource blobQueueSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2023-12-15-preview' = {
  parent: systemTopic
  name: 'omnivec-blob-queue'
  properties: {
    destination: {
      endpointType: 'StorageQueue'
      properties: {
        resourceId: storageAccountId
        queueName: 'blob-events'
        queueMessageTimeToLiveInSeconds: -1
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Storage.BlobCreated'
      ]
      subjectBeginsWith: '/blobServices/default/containers/documents'
    }
  }
}

// EventGrid EventSubscription Contributor role on storage account
var eventGridContributorRoleId = '428e0ff0-5e57-4d9c-a221-2c70d0e0a443'

resource eventGridRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccountId, identityPrincipalId, eventGridContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', eventGridContributorRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output systemTopicName string = systemTopic.name
