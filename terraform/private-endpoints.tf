# RES-1 — Private endpoints for OmniVec Azure dependencies (optional module)
#
# Adds private-endpoints + DNS-zone-group entries for each Azure data plane
# OmniVec depends on, so traffic from the AKS subnet stays on the Azure
# backbone instead of egressing to the public internet.
#
# Wiring:
#  1. Create an existing-VNet+subnet pair for the AKS nodepool (out of scope
#     here — your AKS module already owns this).
#  2. Set `enable_private_endpoints = true` and the supporting VNet inputs
#     in terraform.tfvars / your Azure DevOps pipeline variables.
#  3. Re-run `terraform apply`. Each resource also flips
#     `public_network_access_enabled = false` once its private endpoint is
#     healthy (acceptance gate).
#
# Per-resource toggles let you stage the rollout one service at a time
# (Cosmos first, then Blob, then Key Vault, then AOAI / Service Bus).
#
# What this module does NOT do:
#  - Doesn't create the VNet, subnet, or private DNS zones — those usually
#    belong to a hub network module owned by platform.
#  - Doesn't migrate existing data — data plane stays online during the
#    cutover; clients pick up the private DNS records once
#    `public_network_access_enabled = false`.

# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------

variable "enable_private_endpoints" {
  description = "Master switch — set true to provision private endpoints"
  type        = bool
  default     = false
}

variable "private_endpoint_subnet_id" {
  description = "Resource ID of the subnet that will host the private endpoint NICs"
  type        = string
  default     = ""
}

variable "private_dns_zone_ids" {
  description = "Map of service -> private DNS zone resource ID. Keys: cosmos_sql, blob, servicebus, keyvault, openai. Empty/missing value disables that service's PE."
  type        = map(string)
  default     = {}
}

# ----------------------------------------------------------------------
# Phase 2 inputs — Key Vault + AOAI live outside this module today, so we
# accept their resource IDs as inputs rather than baking in data sources.
# Empty string disables the corresponding endpoint.
# ----------------------------------------------------------------------
variable "key_vault_id" {
  description = "Resource ID of the Key Vault to front with a private endpoint. Empty disables the Key Vault PE."
  type        = string
  default     = ""
}

variable "openai_account_id" {
  description = "Resource ID of the Azure OpenAI (Cognitive Services) account. Empty disables the AOAI PE."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------
# Cosmos (SQL API) — metadata + vectors
# ----------------------------------------------------------------------
resource "azurerm_private_endpoint" "cosmos" {
  count               = var.enable_private_endpoints && lookup(var.private_dns_zone_ids, "cosmos_sql", "") != "" ? 1 : 0
  name                = "${azurerm_cosmosdb_account.omnivec.name}-pe"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${azurerm_cosmosdb_account.omnivec.name}-psc"
    private_connection_resource_id = azurerm_cosmosdb_account.omnivec.id
    subresource_names              = ["Sql"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_ids["cosmos_sql"]]
  }
}

# ----------------------------------------------------------------------
# Blob (attachment store) — references existing Storage Account
# ----------------------------------------------------------------------
resource "azurerm_private_endpoint" "blob" {
  count               = var.enable_private_endpoints && lookup(var.private_dns_zone_ids, "blob", "") != "" ? 1 : 0
  name                = "${data.azurerm_storage_account.omnivec.name}-blob-pe"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${data.azurerm_storage_account.omnivec.name}-blob-psc"
    private_connection_resource_id = data.azurerm_storage_account.omnivec.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_ids["blob"]]
  }
}

# ----------------------------------------------------------------------
# Service Bus
# ----------------------------------------------------------------------
resource "azurerm_private_endpoint" "servicebus" {
  count               = var.enable_private_endpoints && lookup(var.private_dns_zone_ids, "servicebus", "") != "" ? 1 : 0
  name                = "${azurerm_servicebus_namespace.omnivec.name}-pe"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${azurerm_servicebus_namespace.omnivec.name}-psc"
    private_connection_resource_id = azurerm_servicebus_namespace.omnivec.id
    subresource_names              = ["namespace"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_ids["servicebus"]]
  }
}

# ----------------------------------------------------------------------
# Key Vault (RES-1 phase 2)
# ----------------------------------------------------------------------
resource "azurerm_private_endpoint" "keyvault" {
  count = var.enable_private_endpoints && var.key_vault_id != "" && lookup(var.private_dns_zone_ids, "keyvault", "") != "" ? 1 : 0

  name                = "omnivec-keyvault-pe"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "omnivec-keyvault-psc"
    private_connection_resource_id = var.key_vault_id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_ids["keyvault"]]
  }
}

# ----------------------------------------------------------------------
# Azure OpenAI / Cognitive Services (RES-1 phase 2)
# ----------------------------------------------------------------------
resource "azurerm_private_endpoint" "openai" {
  count = var.enable_private_endpoints && var.openai_account_id != "" && lookup(var.private_dns_zone_ids, "openai", "") != "" ? 1 : 0

  name                = "omnivec-openai-pe"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "omnivec-openai-psc"
    private_connection_resource_id = var.openai_account_id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_ids["openai"]]
  }
}

# ----------------------------------------------------------------------
# Outputs — surface PE IDs so downstream pipelines can sanity-check DNS
# ----------------------------------------------------------------------
output "private_endpoint_ids" {
  value = {
    cosmos     = try(azurerm_private_endpoint.cosmos[0].id, null)
    blob       = try(azurerm_private_endpoint.blob[0].id, null)
    servicebus = try(azurerm_private_endpoint.servicebus[0].id, null)
    keyvault   = try(azurerm_private_endpoint.keyvault[0].id, null)
    openai     = try(azurerm_private_endpoint.openai[0].id, null)
  }
  description = "Resource IDs of provisioned private endpoints (null when disabled)."
}
