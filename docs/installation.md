# OmniVec Installation Guide

Deploy OmniVec from scratch using Azure Developer CLI (azd) + Bicep.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Azure CLI (`az`) | 2.50+ | https://learn.microsoft.com/en-us/cli/azure/install-azure-cli |
| Azure Developer CLI (`azd`) | 1.5+ | https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd |
| kubectl | 1.28+ | https://kubernetes.io/docs/tasks/tools/ |
| Helm | 3.12+ | https://helm.sh/docs/intro/install/ |

Docker is optional. If no Docker daemon is available, images are built remotely via `az acr build`.

## Quick Start

```bash
cd /path/to/omnivec

# 1. Login to Azure
az login

# 2. Initialize the azd environment
azd init

# 3. Deploy everything
azd up
```

That's it. The `azd up` command runs through three phases:

1. **Pre-provision** -- validates tools, checks for existing installations, collects configuration
2. **Provision** -- deploys all Azure resources via Bicep (~10-15 min for AKS + GPU nodes)
3. **Post-provision** -- builds/pushes container images, configures AKS, deploys Helm charts

## What Gets Created

Every resource in an installation is tagged with the same `omnivec-instance` identifier and shares a unique `resourceToken` suffix derived from your subscription, resource group, and environment name.

### Always created

| Resource | Name Pattern | Purpose |
|----------|-------------|---------|
| Managed Identity | `omnivec-identity-{token}` | Workload identity for all pods |
| CosmosDB Account | `omnivec-cosmos-{token}` | Serverless NoSQL -- metadata store |
| CosmosDB Database | `omnivec` | Single database |
| CosmosDB Container | `metadata` | All control plane state (PK: `/doc_type`) |
| Container Registry | `omnivecacr{token}` | Docker images (Basic tier) |
| AKS Cluster | `omnivec-aks-{token}` | Kubernetes (system + GPU node pools) |
| Federated Credentials | 2x | Workload identity for `omnivec` and `docgrok` namespaces |

### Created only with blob source enabled

| Resource | Name Pattern | Purpose |
|----------|-------------|---------|
| Storage Account | `omnivecstore{token}` | Blob storage for documents + queue |
| Blob Container | `documents` | Document uploads |
| Storage Queue | `blob-events` | Receives Event Grid notifications |
| Service Bus | `omnivec-sb-{token}` | Jobs queue (Standard tier) |
| Event Grid Topic | `omnivec-blob-events-{token}` | Routes BlobCreated events |

## Interactive Configuration

During `azd up`, the preprovision hook asks:

### 1. Existing installations

If OmniVec resources already exist in the subscription (detected via `omnivec-instance` tag), you're shown a list and asked whether to launch a new installation alongside them or cancel.

### 2. Metadata storage backend

```
Select metadata storage backend:
  1) Azure CosmosDB (Serverless NoSQL) -- recommended
  2) Azure CosmosDB (Provisioned throughput)
```

### 3. Blob storage source

```
Will you use Azure Blob Storage as a document source?
  1) Yes -- enable blob source ingestion (recommended)
  2) No  -- CosmosDB sources only (skip Service Bus + Event Grid)
```

Choosing "No" skips creation of Storage Account, Service Bus, and Event Grid, reducing cost and deployment time.

### 4. Node pools

**System pool** (API, controller, worker, changefeed):
- VM SKU: `Standard_D2s_v3` / `Standard_D4s_v3` / `Standard_D8s_v3`
- Node count (default: 2)

**GPU pool** (ML models):
- VM SKU: `Standard_NC6s_v3` / `Standard_NC12s_v3` / `Standard_NC4as_T4_v3`
- Node count (default: 4)

## Deployed Kubernetes Workloads

### `omnivec` namespace

| Deployment | Image | Replicas | Purpose |
|-----------|-------|----------|---------|
| omnivec-api | omnivec-api | 2 | REST API + UI (LoadBalancer) |
| omnivec-controller | omnivec-api | 1 | Background bookkeeper |
| omnivec-worker | omnivec-api | 1-10 (HPA) | Job processor |
| omnivec-changefeed | omnivec-changefeed | 1-5 (HPA) | CosmosDB change feed processor |

### `docgrok` namespace

| Deployment | Image | Replicas | Purpose |
|-----------|-------|----------|---------|
| docgrok | docgrok / docgrok-router | 1 | Embedding orchestrator |
| dse-qwen2 | dse-qwen2-api-ms | 1 | Visual document embeddings (GPU) |
| clip | docgrok-clip | 1 | Image embeddings (GPU) |
| bge | docgrok-bge | 1 | Text embeddings BGE-Large (GPU) |
| bge-small | docgrok-bge-small | 4 | Text embeddings BGE-Small (GPU) |

## Post-Deployment Verification

```bash
# Check pods
kubectl get pods -n omnivec
kubectl get pods -n docgrok

# Get external IP
kubectl get svc omnivec-api -n omnivec

# Test endpoints
curl http://<EXTERNAL_IP>/health
curl http://<EXTERNAL_IP>/ui
```

## Multiple Environments

azd supports multiple named environments, each producing a fully isolated installation:

```bash
# Development
azd env new dev
azd up

# Production (separate resource token, no collisions)
azd env new prod
azd up
```

Switch between them with `azd env select <name>`.

## Updating an Existing Installation

```bash
# Re-run to update infrastructure + redeploy
azd up

# Or just redeploy apps (skip Bicep)
azd deploy
```

## Tearing Down

```bash
# Remove ALL Azure resources for this environment
azd down --force --purge
```

This deletes every resource tagged with the environment's `omnivec-instance` identifier.

## File Structure

```
omnivec/
  azure.yaml                        # azd project definition
  infra/
    main.bicep                      # Main Bicep orchestrator
    main.parameters.json            # Parameters (env substitution)
    modules/
      identity.bicep                # Managed identity
      cosmosdb.bicep                # CosmosDB serverless + RBAC
      storage.bicep                 # Storage account + blob + queue
      servicebus.bicep              # Service Bus + jobs queue
      eventgrid.bicep               # Event Grid system topic
      acr.bicep                     # Container registry
      aks.bicep                     # AKS cluster (system + GPU)
  hooks/
    preprovision.sh                 # Prereq checks + config prompts
    postprovision.sh                # Image push + AKS setup + Helm
  helm/
    omnivec/                        # OmniVec Helm chart
    docgrok/                        # DocGrok Helm chart (subchart)
```

## Troubleshooting

**AKS nodes not ready / GPU pool pending**
GPU VM SKUs have limited regional availability. Check quota:
```bash
az vm list-skus --location eastus --size Standard_NC --output table
```

**ACR push fails (storage limit)**
Basic tier has 10 GB limit. Clean old images:
```bash
az acr repository list --name <acr-name> -o tsv
az acr repository delete --name <acr-name> --repository <repo> --yes
```

**Pods in CrashLoopBackOff**
Check probe timeouts. Liveness/readiness probes need >= 10s timeout:
```bash
kubectl describe pod <pod-name> -n omnivec
kubectl logs <pod-name> -n omnivec
```

**CosmosDB 403 errors**
Workload identity RBAC may not have propagated yet. SQL role assignments can take a few minutes:
```bash
az cosmosdb sql role assignment list --account-name <cosmos-name> --resource-group <rg>
```

**Helm install timeout**
GPU model pods take 2+ minutes to start (model loading). Increase timeout or check GPU node availability:
```bash
kubectl get nodes -l nvidia.com/gpu=present
kubectl describe pod -n docgrok -l app=dse-qwen2
```
