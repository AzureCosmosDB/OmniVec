// Azure Container Registry — Premium tier (faster imports, geo-replication, throughput)
param registryName string
param location string
param tags object = {}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
  }
  tags: tags
}

output loginServer string = acr.properties.loginServer
output registryName string = acr.name
output registryId string = acr.id
