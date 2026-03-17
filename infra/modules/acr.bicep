// Azure Container Registry — Basic tier
param registryName string
param location string
param tags object = {}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
  tags: tags
}

output loginServer string = acr.properties.loginServer
output registryName string = acr.name
output registryId string = acr.id
