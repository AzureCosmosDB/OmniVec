// AKS cluster — system pool + GPU pool, workload identity, OIDC issuer
param clusterName string
param location string
param tags object = {}
param kubernetesVersion string
param systemNodeVmSize string
param systemNodeCount int
param gpuNodeVmSize string
param gpuNodeCount int

var gpuPool = gpuNodeCount > 0 ? [
  {
    name: 'gpu'
    count: gpuNodeCount
    minCount: 0
    maxCount: 8
    enableAutoScaling: true
    vmSize: gpuNodeVmSize
    osType: 'Linux'
    osSKU: 'Ubuntu'
    mode: 'User'
    type: 'VirtualMachineScaleSets'
    nodeTaints: [
      'nvidia.com/gpu=present:NoSchedule'
    ]
    nodeLabels: {
      'nvidia.com/gpu': 'present'
    }
  }
] : []

resource aks 'Microsoft.ContainerService/managedClusters@2026-01-02-preview' = {
  name: clusterName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName
    enableRBAC: true
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'calico'
      loadBalancerSku: 'standard'
    }
    agentPoolProfiles: concat([
      {
        name: 'system'
        count: systemNodeCount
        minCount: systemNodeCount
        maxCount: 5
        enableAutoScaling: true
        vmSize: systemNodeVmSize
        osType: 'Linux'
        osSKU: 'Ubuntu'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
      }
    ], gpuPool)
  }
  tags: tags
}

output clusterName string = aks.name
output oidcIssuerUrl string = aks.properties.oidcIssuerProfile.issuerURL
output kubeletObjectId string = aks.properties.identityProfile.kubeletidentity.objectId
