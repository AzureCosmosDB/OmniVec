// AcrPull role assignment — allows AKS kubelet to pull images from ACR
param acrName string
param aksClusterName string
param kubeletObjectId string

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, acrName, aksClusterName, acrPullRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: kubeletObjectId
    principalType: 'ServicePrincipal'
  }
}
