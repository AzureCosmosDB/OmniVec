# OmniVec Azure Infrastructure
# Uses existing AKS cluster, creates supporting resources

terraform {
  required_version = ">= 1.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.80"
    }
  }
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}

# =============================================================================
# VARIABLES
# =============================================================================

variable "resource_group_name" {
  description = "Existing resource group name"
  type        = string
  default     = "CDB-MVS-RG"
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "prefix" {
  description = "Prefix for new resources"
  type        = string
  default     = "omnivec"
}

variable "aks_cluster_name" {
  description = "Existing AKS cluster name"
  type        = string
  default     = ""  # Leave empty to skip AKS data lookup
}

# =============================================================================
# EXISTING RESOURCES
# =============================================================================

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

data "azurerm_client_config" "current" {}

# Get existing AKS cluster (if name provided)
data "azurerm_kubernetes_cluster" "existing" {
  count               = var.aks_cluster_name != "" ? 1 : 0
  name                = var.aks_cluster_name
  resource_group_name = var.resource_group_name
}

# =============================================================================
# COSMOS DB
# =============================================================================

resource "azurerm_cosmosdb_account" "omnivec" {
  name                = "${var.prefix}-cosmos"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  enable_automatic_failover    = false
  local_authentication_disabled = true

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = data.azurerm_resource_group.main.location
    failover_priority = 0
  }

  tags = {
    Project   = "OmniVec"
    ManagedBy = "Terraform"
  }
}

# OmniVec Metadata Database
resource "azurerm_cosmosdb_sql_database" "omnivec" {
  name                = "omnivec"
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.omnivec.name
}

# Metadata Container (single container for all control plane state)
resource "azurerm_cosmosdb_sql_container" "metadata" {
  name                = "metadata"
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.omnivec.name
  database_name       = azurerm_cosmosdb_sql_database.omnivec.name
  partition_key_path  = "/doc_type"

  indexing_policy {
    indexing_mode = "consistent"
    included_path {
      path = "/*"
    }
  }
}

# =============================================================================
# Random suffix for globally unique resource names
# =============================================================================

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

# =============================================================================
# SERVICE BUS
# =============================================================================

resource "azurerm_servicebus_namespace" "omnivec" {
  name                = "${var.prefix}-sb-${random_string.suffix.result}"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  sku                 = "Standard"
  local_auth_enabled  = false

  tags = {
    Project   = "OmniVec"
    ManagedBy = "Terraform"
  }
}

resource "azurerm_servicebus_queue" "jobs" {
  name         = "jobs"
  namespace_id = azurerm_servicebus_namespace.omnivec.id

  max_delivery_count                   = 5
  lock_duration                        = "PT5M"
  dead_lettering_on_message_expiration = true
}

# =============================================================================
# STORAGE ACCOUNT (existing — for blob source ingestion)
# =============================================================================

data "azurerm_storage_account" "omnivec" {
  name                = "omnivecstore34719"
  resource_group_name = data.azurerm_resource_group.main.name
}

# =============================================================================
# EVENT GRID — System Topic on Storage Account
# =============================================================================

resource "azurerm_eventgrid_system_topic" "blob_events" {
  name                   = "${var.prefix}-blob-events"
  location               = data.azurerm_resource_group.main.location
  resource_group_name    = data.azurerm_resource_group.main.name
  source_arm_resource_id = data.azurerm_storage_account.omnivec.id
  topic_type             = "Microsoft.Storage.StorageAccounts"

  tags = {
    Project   = "OmniVec"
    ManagedBy = "Terraform"
  }
}

# =============================================================================
# MANAGED IDENTITY FOR WORKLOAD IDENTITY
# =============================================================================

resource "azurerm_user_assigned_identity" "omnivec" {
  name                = "${var.prefix}-identity"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name

  tags = {
    Project   = "OmniVec"
    ManagedBy = "Terraform"
  }
}

# CosmosDB SQL role assignment (data plane RBAC — required when local auth is disabled)
resource "azurerm_cosmosdb_sql_role_assignment" "omnivec_data" {
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.omnivec.name
  # Built-in "Cosmos DB Built-in Data Contributor" role definition ID
  role_definition_id  = "${azurerm_cosmosdb_account.omnivec.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.omnivec.principal_id
  scope               = azurerm_cosmosdb_account.omnivec.id
}

# Storage Blob Data Reader — download blobs for processing
resource "azurerm_role_assignment" "omnivec_blob_reader" {
  scope                = data.azurerm_storage_account.omnivec.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.omnivec.principal_id
}

# Service Bus Data Owner — send/receive messages on jobs queue
resource "azurerm_role_assignment" "omnivec_servicebus_owner" {
  scope                = azurerm_servicebus_namespace.omnivec.id
  role_definition_name = "Azure Service Bus Data Owner"
  principal_id         = azurerm_user_assigned_identity.omnivec.principal_id
}

# EventGrid EventSubscription Contributor — dynamically manage subscriptions
resource "azurerm_role_assignment" "omnivec_eventgrid_contributor" {
  scope                = data.azurerm_storage_account.omnivec.id
  role_definition_name = "EventGrid EventSubscription Contributor"
  principal_id         = azurerm_user_assigned_identity.omnivec.principal_id
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "resource_group_name" {
  value = data.azurerm_resource_group.main.name
}

output "cosmos_endpoint" {
  value = azurerm_cosmosdb_account.omnivec.endpoint
}

output "cosmos_account_name" {
  value = azurerm_cosmosdb_account.omnivec.name
}

output "servicebus_namespace" {
  value = azurerm_servicebus_namespace.omnivec.name
}

output "servicebus_endpoint" {
  value = "${azurerm_servicebus_namespace.omnivec.name}.servicebus.windows.net"
}

output "managed_identity_client_id" {
  value = azurerm_user_assigned_identity.omnivec.client_id
}

output "managed_identity_principal_id" {
  value = azurerm_user_assigned_identity.omnivec.principal_id
}

output "storage_account_name" {
  value = data.azurerm_storage_account.omnivec.name
}

output "storage_account_id" {
  value = data.azurerm_storage_account.omnivec.id
}

output "storage_blob_endpoint" {
  value = data.azurerm_storage_account.omnivec.primary_blob_endpoint
}

output "eventgrid_system_topic_name" {
  value = azurerm_eventgrid_system_topic.blob_events.name
}

output "helm_install_command" {
  value = <<-EOT
    helm upgrade --install omnivec ./helm/omnivec \
      --namespace omnivec \
      --create-namespace \
      --set azure.cosmos.endpoint="${azurerm_cosmosdb_account.omnivec.endpoint}" \
      --set azure.serviceBus.namespace="${azurerm_servicebus_namespace.omnivec.name}.servicebus.windows.net" \
      --set azure.workloadIdentity.clientId="${azurerm_user_assigned_identity.omnivec.client_id}"
  EOT
}
