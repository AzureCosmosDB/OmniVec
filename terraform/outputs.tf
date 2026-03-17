# Additional outputs for Helm values generation

output "helm_values" {
  description = "Values to use in Helm chart"
  value = {
    global = {
      namespace = "omnivec"
    }
    azure = {
      cosmos_endpoint             = azurerm_cosmosdb_account.omnivec.endpoint
      servicebus_namespace        = "${azurerm_servicebus_namespace.omnivec.name}.servicebus.windows.net"
      workload_identity_client_id = azurerm_user_assigned_identity.omnivec.client_id
    }
  }
}
