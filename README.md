# OmniVec

**Any data source → embeddings → vector search, deployed on Azure in one command.**

OmniVec automates the full vector ingestion pipeline: connect a data source, extract content, generate embeddings, and store vectors in a searchable destination. It runs on Azure Kubernetes Service and comes with a web UI, CLI, and REST API.

```
Sources                  Processing              Destinations
────────                 ──────────              ────────────
Azure Blob Storage ─┐                         ┌→ CosmosDB Vector
CosmosDB           ─┼─→ DocGrok Pipelines ───┤→ pgvector
PostgreSQL         ─┤   (embed, OCR, chunk)   └→ MSSQL
MSSQL              ─┘
```

This guide walks you through deploying OmniVec and running your first end-to-end pipeline.

---

## Part 1 — Deploy OmniVec

### Prerequisites

Install these before you begin:

| Tool | Install | Verify |
|------|---------|--------|
| **Azure CLI** (`az`) | [install](https://aka.ms/install-azure-cli) | `az version` |
| **Azure Developer CLI** (`azd`) | [install](https://aka.ms/install-azd) | `azd version` |
| **PowerShell 7+** (`pwsh`) | [install](https://aka.ms/install-powershell) | `pwsh --version` |
| **Git** | [install](https://git-scm.com) | `git --version` |

You also need:

- An **Azure subscription** with permission to create resource groups, AKS clusters, and CosmosDB accounts.

> `kubectl` and `helm` are installed automatically by the deployment hooks if not already present.

> **Cost estimate:** The default configuration (2× Standard_B4ms nodes, no GPU, CosmosDB serverless) costs roughly **$5–10/day**. Run `azd down --purge --force` when you're done to stop all charges.

### Deploy

> **Windows users:** Run these commands in PowerShell 7 (`pwsh`), not Command Prompt.

```bash
# Clone the repo (includes submodules)
git clone --recurse-submodules https://github.com/AzureCosmosDB/OmniVec
cd OmniVec

# Log in to Azure
az login
azd auth login

# Create a named environment
azd env new my-omnivec

# Deploy everything — infrastructure + application (~15–25 minutes)
azd up
```

When prompted, choose **1) Quick start** to use recommended defaults (no GPU, CosmosDB serverless, blob source enabled). Or pre-set config to skip all prompts:

```bash
azd env set AZURE_LOCATION              eastus2
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE Standard_B4ms
azd env set OMNIVEC_SYSTEM_NODE_COUNT   2
azd env set OMNIVEC_GPU_NODE_VM_SIZE    ""
azd env set OMNIVEC_GPU_NODE_COUNT      0
azd env set OMNIVEC_METADATA_STORE      cosmosdb-serverless
azd env set OMNIVEC_ENABLE_BLOB_SOURCE  true
azd up
```

What happens behind the scenes:

1. **preprovision hook** — validates tools, checks for an existing deployment, collects any missing config interactively.
2. **Bicep deployment** — provisions AKS, CosmosDB, ACR, Key Vault, Storage, Service Bus, and Event Grid.
3. **postprovision hook** — imports pre-built container images (or builds from source), deploys all services via Helm.

### Save these values

When deployment finishes, the console prints two important values. **Copy them now:**

| Value | What it is |
|-------|-----------|
| **OmniVec URL** | `http://<id>.<region>.cloudapp.azure.com/ui` — your web UI |
| **Admin Token** | Bearer token for API and CLI authentication |

If you missed them:

```bash
# Retrieve the admin token
azd env get-value OMNIVEC_ADMIN_TOKEN

# Check which environment is active
azd env list
```

---

### Open the UI

Open the **OmniVec URL** in your browser. You should see the OmniVec dashboard.

If the page doesn't load, wait 1–2 minutes for the load balancer to assign an external IP:

```bash
kubectl get svc omnivec-web -n omnivec
```

**Deployment is complete.** OmniVec is running. The next part walks through creating your first pipeline.

---

## Part 2 — Your first pipeline

This section requires an **Azure OpenAI resource** with an embedding model deployed. If you don't have one yet:

1. [Create an Azure OpenAI resource](https://learn.microsoft.com/azure/ai-services/openai/how-to/create-resource)
2. [Deploy an embedding model](https://learn.microsoft.com/azure/ai-services/openai/how-to/create-resource?pivots=web-portal#deploy-a-model) — choose `text-embedding-3-small` for a first run
3. Note these three values:
   - **Endpoint URL** — Azure Portal → your OpenAI resource → Overview
   - **API Key** — Azure Portal → Keys and Endpoint
   - **Deployment Name** — Azure Portal → Deployments → the exact name you gave the deployment (this is **not** the model name)

This walkthrough uses the UI. For CLI equivalents, see [docs/cli-guide.md](docs/cli-guide.md).

### Register an embedding model

1. Go to **Models** in the sidebar.
2. Click **Add Model**.
3. Choose **Azure OpenAI (External)**.
4. Fill in the three values from your Azure OpenAI resource:

   | Field | Value | Where to find it |
   |-------|-------|-------------------|
   | Endpoint | `https://<resource>.openai.azure.com` | Azure Portal → your OpenAI resource → Overview |
   | API Key | `xxxxxxxx` | Azure Portal → Keys and Endpoint |
   | Deployment Name | e.g. `text-embedding-3-small` | Azure Portal → Deployments (the exact name, not the model name) |

5. Click **Save**, then **Test** to confirm OmniVec can reach the model.

> **Common mistake:** The deployment name must match exactly what's shown in the Azure Portal under "Deployments." If you named your deployment `my-embeddings`, use `my-embeddings` — not `text-embedding-3-small`.

### Create a source

A source is a connection to data you want to embed. For this first run, use **CosmosDB** — create a new CosmosDB account for your data (separate from the OmniVec metadata account).

1. Create a new CosmosDB account in the Azure Portal:
   - Azure Portal → **Create a resource** → **Azure Cosmos DB** → **NoSQL**
   - **Account name**: e.g., `my-omnivec-data`
   - **Capacity mode**: Serverless
   - Enable **Vector Search** under Features
   - Click **Create** (takes ~3–5 minutes)
2. Create a database and source container:
   - Go to your new Cosmos DB account → **Data Explorer** → **New Container**
   - **Database id**: `demo` (create new)
   - **Container id**: `documents`
   - **Partition key**: `/id`
   - Click **OK**
3. Insert a few sample documents via Data Explorer → `documents` container → **New Item**:
   ```json
   { "id": "doc-001", "content": "OmniVec is a universal vector ingestion platform that processes documents from Azure CosmosDB into vector embeddings for semantic search.", "title": "About OmniVec" }
   ```
   ```json
   { "id": "doc-002", "content": "Azure Kubernetes Service simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.", "title": "About AKS" }
   ```
4. Grant the OmniVec managed identity access to this account:

   **Bash / Linux:**
   ```bash
   az cosmosdb sql role assignment create \
     --account-name "my-omnivec-data" \
     --resource-group "<your-rg>" \
     --role-definition-id "00000000-0000-0000-0000-000000000002" \
     --principal-id "<omnivec-identity-principal-id>" \
     --scope "/dbs"
   ```
   **PowerShell / Windows:**
   ```powershell
   az cosmosdb sql role assignment create `
     --account-name "my-omnivec-data" `
     --resource-group "<your-rg>" `
     --role-definition-id "00000000-0000-0000-0000-000000000002" `
     --principal-id "<omnivec-identity-principal-id>" `
     --scope "/dbs"
   ```
   Also grant **Cosmos DB Account Reader Role** via Access Control (IAM).

5. In OmniVec, go to **Sources** → **New Source**.
6. Choose **CosmosDB**.
7. Fill in:
   - **Name**: `My First Source`
   - **Endpoint**: your new Cosmos DB account URI
   - **Database**: `demo`
   - **Container**: `documents`
8. Click **Save**, then **Test Connection** to verify access.

### Create a destination

A destination is where vectors are stored. Use the **same CosmosDB account** with a separate container that has a vector embedding policy.

1. In your Cosmos DB account → **Data Explorer** → **New Container**:
   - **Database id**: `demo` (use existing)
   - **Container id**: `vectors`
   - **Partition key**: `/id`
   - Under **Container Vector Policy**, add a vector embedding:
     - **Path**: `/embedding`
     - **Data type**: `float32`
     - **Dimensions**: `1536` (matches `text-embedding-3-small`)
     - **Distance function**: `cosine`
   - Click **OK** to create
2. In OmniVec, go to **Destinations** → **New Destination**.
3. Choose **CosmosDB Vector**.
4. Fill in:
   - **Name**: `My First Destination`
   - **Endpoint**: same Cosmos DB account URI
   - **Database**: `demo`
   - **Container**: `vectors`
5. Click **Save**, then **Test Connection**.
6. Click **Fetch Embedding Policies** — you should see `/embedding` with dimensions `1536` and distance function `cosine`.

> **If Fetch Embedding Policies returns nothing:** your container doesn't have a vector embedding policy configured. Go back to Data Explorer and verify the container's vector policy includes a `/embedding` path. See the [Cosmos DB vector search docs](https://learn.microsoft.com/azure/cosmos-db/nosql/vector-search) for details.

### Create a pipeline

A pipeline ties source → model → destination together.

1. Go to **Pipelines** → **New Pipeline**.
2. Fill in:
   - **Name**: `My First Pipeline`
   - **Source**: select your CosmosDB source
   - **Destination**: select your CosmosDB vector destination
   - **Model**: select the Azure OpenAI model you registered
   - **Embedding Policy Path**: select the path from your destination (e.g., `/embedding`)
   - **Content Strategy**: `Truncate` (embeds full document text as a single vector — simplest for first run)
   - **Process Existing**: ✅ enable this (so documents already in the source get processed)
3. Click **Create**.

The pipeline starts processing immediately. You can watch progress on the pipeline detail page.

### Verify it worked

Within a few minutes, check these signals:

- [ ] **Pipeline health** shows green on the Pipelines page
- [ ] **Embedded count** increases to match your source document count
- [ ] **Completion** reaches 100%

Then test vector search:

1. Go to **Vector Search** in the sidebar.
2. Select your destination index.
3. Type: `vector ingestion platform`
4. Click **Search** — you should see your documents returned with similarity scores.

> **Expected result:** Your sample document about OmniVec should appear as the top result for "vector ingestion platform."

**Congratulations — you've deployed OmniVec and run a full vector ingestion pipeline.** 🎉

---

## Cleanup

To stop all charges, delete all Azure resources:

```bash
azd down --purge --force
```

This removes the resource group, all Azure services, and local environment config.

---

## Next steps

| Want to... | Go to |
|-----------|-------|
| Manage pipelines via CLI | [CLI Guide](docs/cli-guide.md) |
| Understand the architecture | [Architecture](docs/architecture.md) |
| Use the web UI in depth | [User Guide](docs/user-guide.md) |
| Run the automated E2E test suite | [E2E Demo](#automated-e2e-demo) below |
| Diagnose deployment or pipeline issues | [Diagnostics](#diagnostics) below |
| Add GPU-hosted models | [Models](#models) section below |

---

## Reference

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AZURE_LOCATION` | Yes | — | Azure region (e.g., `eastus2`, `westus3`) |
| `OMNIVEC_SYSTEM_NODE_VM_SIZE` | Yes | prompted | VM SKU for system nodes (e.g., `Standard_B4ms`) |
| `OMNIVEC_SYSTEM_NODE_COUNT` | Yes | `2` | Number of system nodes |
| `OMNIVEC_GPU_NODE_VM_SIZE` | No | `""` | GPU VM SKU (empty = no GPU pool) |
| `OMNIVEC_GPU_NODE_COUNT` | No | `0` | GPU nodes (0 = external models only) |
| `OMNIVEC_METADATA_STORE` | Yes | prompted | `cosmosdb-serverless` or `cosmosdb-provisioned` |
| `OMNIVEC_ENABLE_BLOB_SOURCE` | Yes | prompted | `true` = create Storage + Service Bus + Event Grid |
| `OMNIVEC_SHARED_REGISTRY_TOKEN` | No | prompted | Token for pre-built images (skip = build from source) |
| `OMNIVEC_BUILD_MODE` | No | auto-detect | `acr` (cloud build) or `docker` (local build) |
| `OMNIVEC_BUILD` | No | `false` | `true` = force building from source |
| `OMNIVEC_ADMIN_TOKEN` | No | auto-generated | Admin bearer token for API auth |

### What gets deployed

| Resource | Azure Service | Purpose |
|----------|---------------|---------|
| AKS Cluster | Azure Kubernetes Service | All OmniVec + DocGrok pods |
| System Node Pool | configurable VM SKU | API, controller, worker, changefeed, web |
| GPU Node Pool (optional) | NC-series VMs | Self-hosted embedding models |
| Container Registry | Azure Container Registry | Docker images |
| CosmosDB Account | Azure Cosmos DB (NoSQL) | Metadata store |
| Key Vault | Azure Key Vault | Model API keys |
| Storage Account (optional) | Azure Blob Storage | Blob ingestion source |
| Service Bus (optional) | Azure Service Bus | Job queue for blob events |
| Event Grid (optional) | Azure Event Grid | Real-time blob notifications |
| Managed Identity | User-Assigned MI | Workload identity (no secrets in pods) |

### Concepts

**Sources** store connection info only — endpoint, credentials, container/table. Content extraction settings (which fields to embed, file type filters) belong to the **pipeline**, not the source. This lets multiple pipelines process the same source differently.

| Source Type | Config |
|-------------|--------|
| `cosmosdb` | endpoint, database, container |
| `azure-blob` | account_url, container, prefix |
| `postgresql` | host, port, database, table |
| `mssql` | host, port, database, table |

**Destinations** are where vectors are stored. When you test a destination, OmniVec probes its vector indexing policy and returns available vector paths. You pick one when creating a pipeline.

| Destination Type | Config |
|------------------|--------|
| `cosmosdb-vector` | endpoint, database, container |
| `pgvector` | host, port, database, table |
| `mssql` | host, port, database, table |

**Pipelines** define the full flow: source(s) → content extraction → embedding model → destination. Key settings include `content_strategy` (`truncate` or `chunk`), `processing_mode` (`queue` or `inline`), and `process_existing` (backfill on creation).

### Models

| Type | Examples | ID Format |
|------|----------|-----------|
| **External** | Azure OpenAI `text-embedding-3-small` / `text-embedding-3-large` | `mdl-ext-{hash}` |
| **Native (GPU)** | DSE-Qwen2, CLIP, BGE, BGE-Small | `mdl-{hash}` |

External models (Azure OpenAI) are the easiest starting point — no GPU nodes needed. Native models require a GPU node pool.

### Updating a deployment

Running `azd up` on an existing environment is safe and idempotent:

1. Preprovision detects the existing resource group, imports config from RG tags, skips prompts.
2. Bicep runs — unchanged resources are not modified.
3. Postprovision re-imports only images with updated digests.
4. Updated images trigger automatic `kubectl rollout restart`.
5. Config is saved as RG tags — another developer can `azd env refresh` + `azd up` from a different machine.

Force a source build:
```bash
azd env set OMNIVEC_BUILD true
azd up
```

---

## Automated E2E demo

The scripted demo creates sources, destinations, pipelines, sample data, and validates vector search end-to-end. Use it after you're comfortable with the manual flow above.

**PowerShell (Windows/macOS/Linux):**

```powershell
# Against your existing deployment
pwsh scripts/e2e-demo.ps1 -Existing -EnvName my-omnivec `
  -AdminToken <token> `
  -AoaiEndpoint https://<resource>.openai.azure.com `
  -AoaiKey <key>

# Full automated run (creates new infra)
pwsh scripts/e2e-demo.ps1

# Resume from a specific step
pwsh scripts/e2e-demo.ps1 -FromStep 5

# Cleanup
pwsh scripts/e2e-demo.ps1 -Cleanup -EnvName my-omnivec
```

**Bash (Linux/macOS):**

```bash
# Against your existing deployment
./scripts/e2e-demo.sh --existing --env my-omnivec \
  --token <token> \
  --endpoint https://<resource>.openai.azure.com \
  --key <key>

# Full automated run (creates new infra)
./scripts/e2e-demo.sh --endpoint <url> --key <key>

# Resume from a specific step
./scripts/e2e-demo.sh --from-step 5

# Cleanup
./scripts/e2e-demo.sh --cleanup --env my-omnivec
```

| Flag (PS1 / Bash) | Description |
|------|-------------|
| `-Existing` / `--existing` | Use an existing deployment |
| `-EnvName` / `--env` | azd environment name |
| `-AdminToken` / `--token` | Admin token (`azd env get-value OMNIVEC_ADMIN_TOKEN`) |
| `-AoaiEndpoint` / `--endpoint` | Azure OpenAI endpoint URL |
| `-AoaiKey` / `--key` | Azure OpenAI API key |
| `-FromStep` / `--from-step` | Resume from step N (1–11) |
| `-Cleanup` / `--cleanup` | Delete test resources |

---

## Diagnostics

Run the diagnostics script to check deployment health and find common issues:

**PowerShell:**
```powershell
pwsh scripts/diagnose.ps1 -EnvName my-omnivec
```

**Bash:**
```bash
./scripts/diagnose.sh --env my-omnivec
```

It checks 11 areas: infrastructure, pods, Helm release, networking, auth/RBAC, images, node capacity, models, pipelines, Service Bus, and recent error logs. Every failure includes a copy-paste fix command.

**Deep-diagnose a single pipeline** (finds why it's stuck or not running):

```powershell
pwsh scripts/diagnose.ps1 -EnvName my-omnivec -Pipeline pip-abc123
```
```bash
./scripts/diagnose.sh --env my-omnivec --pipeline pip-abc123
```

Pipeline diagnostics detects: paused, error state, 0 source docs, changefeed not triggering, workers down, all jobs failing, stalled mid-progress, dimension mismatch, and more.

---

## Troubleshooting

### Missed the URL or admin token after deploy

```bash
azd env get-value OMNIVEC_ADMIN_TOKEN
azd env list
kubectl get svc omnivec-web -n omnivec    # shows the external IP
```

### DeploymentNotFound from Azure OpenAI

The `deployment` field must match the exact deployment name in the Azure Portal (under Deployments), not the model name.

### readMetadata RBAC error

**Symptom:** `principal does not have required RBAC permissions to perform action readMetadata`

**Fix:** Grant both roles to the managed identity on every CosmosDB account OmniVec accesses:
1. `Cosmos DB Built-in Data Contributor` (SQL RBAC)
2. `Cosmos DB Account Reader Role` (ARM RBAC)

### 401 Unauthorized from changefeed/controller

Internal services call the API without a Bearer token. The API bypasses auth for `Host: omnivec-api` (K8s internal DNS). Ensure you're running the latest API image.

### Pipeline shows 0% embedded

The vector documents are missing `pipeline_id`/`embedded_at` fields. Use the latest API image.

### Pods stuck in ImagePullBackOff

```bash
az acr repository list --name <acr-name>          # verify images exist
azd hooks run postprovision                         # re-import images
# or force source build:
azd env set OMNIVEC_BUILD true && azd hooks run postprovision
```

### External IP not assigned after 5 minutes

Check AKS load balancer health and NSG rules:
```bash
kubectl get svc omnivec-web -n omnivec
kubectl describe svc omnivec-web -n omnivec
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       OmniVec Platform (AKS)                            │
│                                                                         │
│  ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌──────────────────┐   │
│  │  Web UI   │──▶│  OmniVec  │──▶│  DocGrok  │──▶│ Embedding Models │   │
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

See [docs/architecture.md](docs/architecture.md) for details.

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
| `scripts/` | Automation scripts (E2E demo, diagnostics) |

## Release channels

This repo uses two long-lived branches to separate rapid iteration from stable testing:

| Branch | Purpose | Image tag | Who uses it |
|--------|---------|-----------|-------------|
| `main` | Stable releases | `:stable` + `:vX.Y.Z` | Testers, demos, customer-facing deployments |
| `dev`  | Active development | `:dev` | Active development, internal dogfooding |

**Default for `azd up` is `:stable`.** To opt into the dev channel:

```sh
azd env set OMNIVEC_IMAGE_TAG dev
azd up
```

**Promoting dev → main:**
1. Open a PR from `dev` → `main`, squash-merge when green.
2. Tag the merge commit: `git tag vX.Y.Z && git push --tags`.
3. The `build-and-push-images` workflow publishes `:stable` and `:vX.Y.Z` tags for all five images.

**CI auto-builds** (`.github/workflows/build-images.yml`):
- Push to `dev` → rebuild all images with `:dev` + `:sha-<short>` tags.
- Push to `main` → rebuild with `:stable` + `:sha-<short>`.
- Push tag `vX.Y.Z` → rebuild with `:stable` + `:vX.Y.Z`.

Required repo secrets for OIDC auth to ACR: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

## License

MIT
