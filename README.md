# OmniVec — Universal Vector Ingestion Platform

OmniVec connects your data sources (CosmosDB, Azure Blob Storage, PostgreSQL, S3, HTTP) to vector databases, automating the full pipeline: discover documents → extract content → generate embeddings → store vectors. It deploys on Azure Kubernetes Service with a web UI, CLI, and REST API.

```
Sources                  Processing              Destinations
────────                 ──────────              ────────────
Azure Blob Storage ─┐                         ┌→ CosmosDB Vector
CosmosDB           ─┼─→ DocGrok Pipelines ───┤→ pgvector
PostgreSQL         ─┤   (embed, OCR, chunk)   └→ MSSQL
S3 / HTTP          ─┘
```

---

## Quick Start (5 minutes reading)

### Prerequisites

- **Azure subscription** with permissions to create resource groups
- **Azure CLI** (`az`) — [install](https://aka.ms/install-azure-cli), then run `az login`
- **Azure Developer CLI** (`azd`) — [install](https://aka.ms/install-azd)
- **PowerShell 7+** (`pwsh`) — [install](https://aka.ms/install-powershell) (Windows/macOS/Linux)

> `kubectl` and `helm` are installed automatically by the deployment hooks if not already present.

### Deploy

```bash
# 1. Clone the repo
git clone https://github.com/AzureCosmosDB/OmniVec
cd OmniVec

# 2. Create an azd environment
azd init
# Or: azd env new my-omnivec --location eastus2

# 3. Set required configuration (or let the hook prompt you interactively)
azd env set AZURE_LOCATION                eastus2
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE   Standard_B4ms
azd env set OMNIVEC_SYSTEM_NODE_COUNT     2
azd env set OMNIVEC_GPU_NODE_VM_SIZE      ""           # empty = no GPU pool
azd env set OMNIVEC_GPU_NODE_COUNT        0
azd env set OMNIVEC_METADATA_STORE        cosmosdb-serverless
azd env set OMNIVEC_ENABLE_BLOB_SOURCE    true

# 4. Deploy everything (infrastructure + application)
azd up
```

`azd up` runs two hooks automatically:

1. **preprovision** — validates prerequisites, checks for existing deployments, collects any unset config values interactively
2. **postprovision** — imports pre-built images (or builds from source), configures AKS, deploys via Helm

When complete, the console prints:
- **OmniVec URL** — `http://<instance-id>.<region>.cloudapp.azure.com/ui`
- **Admin Token** — for API/CLI authentication

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AZURE_LOCATION` | Yes | — | Azure region (e.g., `eastus2`, `westus3`) |
| `OMNIVEC_SYSTEM_NODE_VM_SIZE` | Yes | prompted | VM SKU for the system node pool (e.g., `Standard_B4ms`, `Standard_D4s_v3`) |
| `OMNIVEC_SYSTEM_NODE_COUNT` | Yes | `2` | Number of system nodes |
| `OMNIVEC_GPU_NODE_VM_SIZE` | No | `""` | GPU VM SKU (empty string to skip GPU pool) |
| `OMNIVEC_GPU_NODE_COUNT` | No | `0` | Number of GPU nodes (0 = external models only) |
| `OMNIVEC_METADATA_STORE` | Yes | prompted | `cosmosdb-serverless` or `cosmosdb-provisioned` |
| `OMNIVEC_ENABLE_BLOB_SOURCE` | Yes | prompted | `true` = create Storage Account + Service Bus + Event Grid |
| `OMNIVEC_SHARED_REGISTRY_TOKEN` | No | prompted | Token for importing pre-built images from `omnivecregistry.azurecr.io` (skip to build from source) |
| `OMNIVEC_BUILD_MODE` | No | auto-detect | `acr` (cloud build) or `docker` (local build) |
| `OMNIVEC_BUILD` | No | `false` | Set to `true` to force building from source instead of importing |
| `OMNIVEC_ADMIN_TOKEN` | No | auto-generated | Admin bearer token for API authentication |

### What Gets Deployed?

| Resource | Azure Service | Purpose |
|----------|---------------|---------|
| AKS Cluster | Azure Kubernetes Service | Runs all OmniVec + DocGrok pods |
| System Node Pool | configurable VM SKU | API, controller, worker, changefeed, web |
| GPU Node Pool (optional) | NC-series VMs | Self-hosted embedding models (BGE, CLIP, DSE-Qwen2) |
| Container Registry | Azure Container Registry | Docker images for all components |
| CosmosDB Account | Azure Cosmos DB (NoSQL) | Metadata store (sources, pipelines, jobs) |
| Key Vault | Azure Key Vault | Secure storage for model API keys |
| Storage Account (optional) | Azure Blob Storage | Document source for blob ingestion |
| Service Bus (optional) | Azure Service Bus | Job queue for blob events |
| Event Grid (optional) | Azure Event Grid | Real-time blob change notifications |
| Managed Identity | User-Assigned MI | Workload identity — no secrets in pods |

---

## Concepts

### Sources

A **source** is a connection to a data store that OmniVec reads documents from. Sources store **connection info only** — no content extraction config.

| Source Type | Config |
|-------------|--------|
| `cosmosdb` | endpoint, database, container |
| `azure-blob` | account_url, container, prefix |
| `postgresql` | host, port, database, table |
| `s3` | bucket, prefix, region |
| `http` | url, method, headers, auth_type |
| `mssql` | host, port, database, table |

> Content extraction (which fields to embed, content mode, file type filters) is configured on the **pipeline source** entry — not on the source itself. This lets different pipelines use different content strategies for the same source.

### Pipelines

A **pipeline** is the processing definition that ties everything together.

```
Source(s) → Content Extraction → DocGrok Model/Pipeline → Destination
```

Pipeline configuration:

| Field | Description |
|-------|-------------|
| `sources` | List of pipeline source entries (see below) |
| `docgrok_pipeline` | Model ID (`mdl-*`) or transform pipeline name (`text-azure`, `pdf-vision`) |
| `destination_id` | Where to write vectors |
| `vector_index_path` | Selected from the destination's vector indexing policy (e.g., `/embedding`) |
| `processing_mode` | `queue` (CFP → jobs → worker) or `inline` (CFP processes directly) |
| `content_strategy` | `truncate` (one vector per doc) or `chunk` (split into chunks) |
| `chunk_config` | Chunk size, overlap, unit, doc_id_pattern |
| `doc_id_pattern` | Template for vector document IDs: `{source}`, `{source_ref}`, `{chunk}` |
| `process_existing` | `true` = backfill existing documents on creation |

Each **pipeline source** entry carries content extraction config:

| Field | Default | Description |
|-------|---------|-------------|
| `source_id` | — | Source to read from |
| `content_fields` | `["content"]` | Document field(s) to concatenate for embedding |
| `content_mode` | `"field"` | `field` (direct value), `blob_url`, `http_url`, `s3_url` |
| `file_types` | `["txt","json","pdf","docx","md","csv"]` | File extensions to process (blob/S3 sources) |
| `url_content_types` | `["txt","json","pdf"]` | Content types for URL modes |
| `filters` | `{}` | Additional filters |

### Destinations

A **destination** is where vectors are stored.

| Destination Type | Config |
|------------------|--------|
| `cosmosdb-vector` | endpoint, database, container |
| `pgvector` | host, port, database, table |
| `mssql` | host, port, database, table |

When you create or test a destination, OmniVec probes the container's **vector indexing policy** and returns available vector paths (with dimensions, distance function, and index type). You select one of these paths as `vector_index_path` when creating a pipeline.

### Models

Embedding models registered with OmniVec via DocGrok:

| Type | Examples | ID Format |
|------|----------|-----------|
| **Native (GPU)** | DSE-Qwen2, CLIP, BGE, BGE-Small | `mdl-{hash}` |
| **External** | Azure OpenAI text-embedding-3-small/large | `mdl-ext-{hash}` |

Models are configured via the UI, CLI (`omnivec model add`), or API (`POST /api/models`).

---

## Running the E2E Demo

The automated demo creates a complete pipeline from scratch, including a test CosmosDB account, sample documents, inline and queue mode tests, and vector search verification.

```powershell
# Full automated run (creates new infra)
pwsh scripts/e2e-demo.ps1

# Against existing deployment
pwsh scripts/e2e-demo.ps1 -Existing -EnvName <azd-env-name> \
  -AdminToken <token> \
  -AoaiEndpoint https://<resource>.openai.azure.com \
  -AoaiKey <key>

# Resume from a specific step
pwsh scripts/e2e-demo.ps1 -FromStep 5

# Cleanup everything when done
pwsh scripts/e2e-demo.ps1 -Cleanup -EnvName <azd-env-name>
```

| Flag | Description |
|------|-------------|
| `-Existing` | Use an existing deployment instead of creating new infra |
| `-EnvName` | azd environment name (maps to resource group `rg-omnivec-<name>`) |
| `-AdminToken` | Admin bearer token (from `azd env get-value OMNIVEC_ADMIN_TOKEN`) |
| `-AoaiEndpoint` | Azure OpenAI endpoint URL |
| `-AoaiKey` | Azure OpenAI API key |
| `-FromStep` | Resume from step N (1–9) |
| `-Cleanup` | Delete all resources and local env config |

---

## CLI Reference

The OmniVec CLI (`omnivec`) is a single standalone binary — download and run.

### Install

| Platform | Binary |
|----------|--------|
| Linux (amd64) | `cli/omnivec` or `omnivec-linux-amd64` |
| macOS (Apple Silicon) | `cli/omnivec-darwin-arm64` |
| Windows | `cli/omnivec-windows-amd64.exe` |

Or build from source (requires Go 1.24+):
```bash
cd cli && go build -o ../bin/omnivec . && cd ..
```

### Configure

```bash
omnivec config set server http://<omnivec-url>
omnivec config set token <admin-token>
omnivec status   # verify connectivity
```

### Key Commands

```bash
# Sources
omnivec source list
omnivec source create --name "My Data" --type azure-blob --config '{"account_url":"...","container":"docs"}'
omnivec source test <id>

# Destinations
omnivec dest list
omnivec dest create --name "Vectors" --type cosmosdb-vector --config '{"endpoint":"...","database":"...","container":"..."}'
omnivec dest test <id>

# Pipelines
omnivec pipeline list
omnivec pipeline create --name "Embed Docs" --source <src-id> --destination <dst-id> \
  --model text-azure --content-fields content --vector-index-path /embedding --process-existing
omnivec pipeline pause <id>
omnivec pipeline resume <id>
omnivec pipeline reset <id>

# Jobs
omnivec job list --pipeline <id> --status completed
omnivec job stats

# Search
omnivec search "your query" --index <dst-id> --top-k 5

# Operations
omnivec deployment list
omnivec deployment scale omnivec-worker --replicas 5
omnivec status
```

See [docs/cli-guide.md](docs/cli-guide.md) for the full CLI reference.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           OmniVec Platform (AKS)                        │
│                                                                         │
│  ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌──────────────────┐   │
│  │  Web UI   │──▶│ OmniVec   │──▶│  DocGrok  │──▶│ Embedding Models │   │
│  │ (nginx)   │   │   API     │   │  Router   │   │ (GPU / External) │   │
│  └──────────┘   └─────┬─────┘   └───────────┘   └──────────────────┘   │
│                       │                                                 │
│            ┌──────────┼──────────────────────────────┐                  │
│            ▼          ▼                              ▼                  │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────────────────────┐      │
│  │  Controller   │  │ Workers  │  │  Change Feed Processor (.NET)│      │
│  │ (bookkeeper)  │  │(job proc)│  │  (CosmosDB CDC, 15 replicas)│      │
│  └──────────────┘  └──────────┘  └──────────────────────────────┘      │
│                                                                         │
│  Azure CosmosDB (metadata)  ·  Azure Blob Storage  ·  Service Bus      │
└─────────────────────────────────────────────────────────────────────────┘
```

| Component | Technology | Replicas | Role |
|-----------|-----------|----------|------|
| `omnivec-web` | nginx + static HTML/JS | 2 | Web UI + reverse proxy |
| `omnivec-api` | Python FastAPI | 2 | REST API (control plane) |
| `omnivec-controller` | Python | 1 | Source monitoring, job creation, metrics |
| `omnivec-worker` | Python | 1–10 (HPA) | Job processing (download → embed → store) |
| `omnivec-changefeed` | .NET | 15 | CosmosDB Change Feed processor (real-time CDC) |
| `docgrok` | Rust (Axum) | 1 | Embedding router (model discovery + routing) |
| `docgrok-controller` | Rust | 1 | Model health monitoring, scale state |
| `docgrok-pipeline-worker` | Python + PaddleOCR | 1 | Multi-step transforms (PDF → OCR → embed) |

See [docs/architecture.md](docs/architecture.md) for detailed architecture documentation.

---

## Updating a Deployment

Running `azd up` on an existing environment is safe and idempotent:

1. The **preprovision** hook detects the existing resource group (`rg-omnivec-<env>`), imports config from RG tags, and skips all prompts.
2. Bicep deployment runs with the same parameters — unchanged resources are not modified.
3. The **postprovision** hook checks if images in the shared registry have updated digests. Only changed images are re-imported.
4. If images were updated, all deployments are automatically restarted (`kubectl rollout restart`).
5. Config is saved as RG tags — another developer on a different machine can `azd env refresh` and `azd up` with no extra setup.

To force a source build instead of image import:
```bash
azd env set OMNIVEC_BUILD true
azd up
```

---

## Troubleshooting

### 401 Unauthorized from changefeed/controller

Internal services call the API without a Bearer token. The API bypasses auth for requests with `Host: omnivec-api` (internal K8s DNS). Ensure you are running the latest API image.

### readMetadata RBAC Error

**Symptom:** `Request blocked by Auth: principal does not have required RBAC permissions to perform action readMetadata`

**Fix:** Grant **both** roles to the managed identity on any CosmosDB account OmniVec accesses:
1. `Cosmos DB Built-in Data Contributor` (SQL RBAC) — for data operations
2. `Cosmos DB Account Reader Role` (ARM RBAC) — for SDK initialization

### DeploymentNotFound from Azure OpenAI

The `deployment` field in model registration must match the exact Azure OpenAI deployment name (as shown in the Azure Portal under Deployments), not the model name.

### Pipeline shows 0% embedded

The vector documents don't have `pipeline_id` / `embedded_at` fields. Use the latest API image which writes these fields via `_sync_write_vector`.

### Pods stuck in ImagePullBackOff

1. Verify images exist: `az acr repository list --name <acr-name>`
2. If images are missing, re-run: `azd hooks run postprovision`
3. Or force source build: `azd env set OMNIVEC_BUILD true && azd hooks run postprovision`

### External IP not assigned

```bash
kubectl get svc omnivec-web -n omnivec
```

If the IP takes more than 5 minutes, check the AKS cluster's load balancer health and NSG rules.

### Cleanup

```bash
# Delete all Azure resources and local config
azd down --purge --force
```

---

## Components

| Directory | Description |
|-----------|-------------|
| `api/` | Control plane API (Python FastAPI) |
| `web/` | Web UI (static HTML/JS + nginx) |
| `connectors/ingestion/dotnet/` | .NET Change Feed Processor connector |
| `connectors/worker/dotnet/` | .NET embedding worker |
| `docgrok/` | Document intelligence engine (git submodule) |
| `cli/` | Go CLI for managing pipelines, sources, and jobs |
| `infra/` | Azure Bicep infrastructure-as-code |
| `helm/` | Kubernetes Helm charts |
| `hooks/` | azd lifecycle hooks (preprovision/postprovision) |
| `scripts/` | Automation scripts (E2E demo, etc.) |

## License

MIT
