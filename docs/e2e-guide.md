# OmniVec End-to-End Guide

Run a complete vector ingestion pipeline from scratch -- provision infrastructure, create a CosmosDB source, embed documents with Azure OpenAI, write vectors, and verify with search.

## Prerequisites

- **Azure CLI** (`az login` completed)
- **Azure Developer CLI** (`azd`) -- [install](https://aka.ms/install-azd)
- **PowerShell 7+** (`pwsh`) -- [install](https://aka.ms/install-powershell)
- **Azure OpenAI** resource with an embedding deployment (e.g., `text-embedding-3-small`)

## Quick Start (Fully Automated)

```powershell
git clone https://github.com/AzureCosmosDB/OmniVec
cd OmniVec

# Set Azure OpenAI credentials
$env:AOAI_ENDPOINT = "https://<your-resource>.openai.azure.com"
$env:AOAI_KEY = "<your-api-key>"

# Run everything
pwsh scripts/e2e-demo.ps1
```

The script handles all 9 steps automatically. The CLI binary is downloaded from GitHub Releases if not present. Total time: ~20 minutes.

To resume from a specific step (e.g., after infra is already provisioned):

```powershell
pwsh scripts/e2e-demo.ps1 -FromStep 5
```

---

## Step-by-Step Manual Guide

### Step 1: Create azd Environment

```powershell
azd env new omnivec-e2e-demo --location eastus2 --subscription <subscription-id>

# Pre-configure (skips interactive prompts)
azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_D4s_v3"
azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
azd env set OMNIVEC_GPU_NODE_VM_SIZE "Standard_NC6s_v3"
azd env set OMNIVEC_GPU_NODE_COUNT 0
azd env set OMNIVEC_BUILD_MODE "acr"
```

### Step 2: Provision Infrastructure

```powershell
azd up --no-prompt
```

This creates (~15 minutes):

| Resource | Purpose |
|----------|---------|
| AKS Cluster | Runs OmniVec pods (API, controller, worker, changefeed, web, DocGrok) |
| CosmosDB (metadata) | Internal state: sources, destinations, pipelines, jobs, tokens |
| ACR | Container registry for OmniVec images |
| Storage Account | Blob source support |
| Service Bus | Queue-mode pipeline processing |
| Managed Identity | Workload identity for Azure resource access |

The postprovision hook automatically:
- Imports pre-built images from the shared registry (`omnivecregistry.azurecr.io`) with `latest` tag
- Generates an admin token (`OMNIVEC_ADMIN_TOKEN`)
- Deploys all services via Helm

### Step 3: Get Connection Details

```powershell
# Read provisioned values
$ADMIN_TOKEN = azd env get-value OMNIVEC_ADMIN_TOKEN
$AKS_CLUSTER = azd env get-value AZURE_AKS_CLUSTER_NAME
$RESOURCE_GROUP = azd env get-value AZURE_RESOURCE_GROUP
$IDENTITY_CLIENT_ID = azd env get-value AZURE_IDENTITY_CLIENT_ID

# Connect to AKS
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --overwrite-existing

# Get the external IP
kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}'

# Verify health
curl http://<IP>/health
# Expected: {"status":"healthy","service":"OmniVec","version":"1.0.0"}
```

Web UI: `http://<IP>/ui` (login with admin token)

### Step 4: Configure CLI

Build from source (requires [Go 1.24+](https://go.dev/dl/)):

```powershell
mkdir bin
cd cli
go build -o ../bin/omnivec.exe .
cd ..
```

Or download from [GitHub Releases](https://github.com/AzureCosmosDB/OmniVec/releases) (v0.5.0+).

The e2e demo script builds the CLI automatically if `bin/omnivec.exe` is not present and Go is installed.

Configure:

```powershell
omnivec config set server http://<IP>
omnivec config set token <admin-token>
omnivec status
```

### Step 5: Create Test CosmosDB Account

The metadata CosmosDB account is for OmniVec internals only. Source and destination data goes in a **separate** account.

**Why a separate account?** The metadata account stores pipelines, jobs, tokens. Mixing user data with internal state creates operational risk and makes cleanup harder.

#### Create the account

If your subscription has a policy requiring `disableLocalAuth=true`, use ARM REST:

```powershell
$armPayload = @{
    location = "eastus2"; kind = "GlobalDocumentDB"
    properties = @{
        databaseAccountOfferType = "Standard"; disableLocalAuth = $true
        enableAutomaticFailover = $false
        consistencyPolicy = @{ defaultConsistencyLevel = "Session" }
        locations = @(@{ locationName = "eastus2"; failoverPriority = 0; isZoneRedundant = $false })
        capabilities = @(@{ name = "EnableServerless" }, @{ name = "EnableNoSQLVectorSearch" })
    }
}
$armFile = [System.IO.Path]::GetTempFileName()
$armPayload | ConvertTo-Json -Depth 10 | Set-Content -Path $armFile -Encoding UTF8
az rest --method PUT --url "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DocumentDB/databaseAccounts/<account>?api-version=2024-05-15" --body "@$armFile" -o none
```

Wait for provisioning (~3 minutes):
```powershell
az cosmosdb show --name <account> --resource-group <rg> --query provisioningState -o tsv
```

#### Grant RBAC (two roles required)

The AKS managed identity needs **two** roles on the test account:

| Role | Type | Why |
|------|------|-----|
| Cosmos DB Built-in Data Contributor | SQL RBAC | Read/write data operations |
| Cosmos DB Account Reader Role | ARM RBAC | SDK `readMetadata` call during client initialization |

```powershell
$PRINCIPAL_ID = az identity show --name omnivec-identity-<token> --resource-group <rg> --query principalId -o tsv

# SQL Data Contributor
az cosmosdb sql role assignment create --account-name <account> --resource-group <rg> --role-definition-id "00000000-0000-0000-0000-000000000002" --principal-id $PRINCIPAL_ID --scope "/"

# ARM Account Reader
az role assignment create --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Cosmos DB Account Reader Role" --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DocumentDB/databaseAccounts/<account>"
```

Wait 30 seconds for RBAC propagation.

#### Create database and containers

```powershell
az cosmosdb sql database create --account-name <account> --name testdb --resource-group <rg>
az cosmosdb sql container create --account-name <account> --database-name testdb --name test-documents --resource-group <rg> --partition-key-path "/id"
```

The `vectors` container requires a vector embedding policy. Create it via Python SDK on the API pod (the `az cli` has issues with vector policy parameters on Windows):

```powershell
kubectl exec deployment/omnivec-api -n omnivec -- python3 -c "
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
import os
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('<endpoint>', credential=cred)
db = client.get_database_client('testdb')
vp = {'vectorEmbeddings': [{'path': '/embedding', 'dataType': 'float32', 'distanceFunction': 'cosine', 'dimensions': 1536}]}
ip = {'includedPaths': [{'path': '/*'}], 'excludedPaths': [{'path': '/embedding/*'}], 'vectorIndexes': [{'path': '/embedding', 'type': 'quantizedFlat'}]}
db.create_container(id='vectors', partition_key={'paths': ['/id'], 'kind': 'Hash'}, vector_embedding_policy=vp, indexing_policy=ip)
"
```

### Step 6: Register Embedding Model

```powershell
curl -X POST http://<IP>/api/models \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "azure-openai-embed",
    "type": "azure-openai",
    "endpoint": "https://<resource>.openai.azure.com",
    "api_key": "<key>",
    "model": "text-embedding-3-small",
    "deployment": "text-embedding-3-small",
    "dimensions": 1536,
    "api_version": "2024-06-01"
  }'
```

**Important**: The `deployment` field must match the actual deployment name in your Azure OpenAI resource. This is the deployment you created in the Azure Portal, not the model name.

Model IDs follow the format:
- `mdl-ext-*` -- external models (Azure OpenAI, OpenAI, custom)
- `mdl-native-*` -- GPU-deployed models (DSE-Qwen2, CLIP, BGE)

Or via CLI:
```
omnivec model add --provider azure-openai-embed --type azure-openai --endpoint <url> --api-key <key> --model text-embedding-3-small --dimensions 1536
```

### Step 7: Create Source and Destination

```
omnivec source create --name demo-source --type cosmosdb --endpoint <test-endpoint> --database testdb --container test-documents --auth-type managed-identity --client-id <identity-client-id>
```

Or via API:
```powershell
# Source
curl -X POST http://<IP>/api/sources \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-cosmosdb-source",
    "type": "cosmosdb",
    "config": {
      "endpoint": "<test-cosmos-endpoint>",
      "database": "testdb",
      "container": "test-documents",
      "auth_type": "managed-identity",
      "client_id": "<identity-client-id>"
    }
  }'

# Destination
curl -X POST http://<IP>/api/destinations \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-vector-store",
    "type": "cosmosdb-vector",
    "config": {
      "endpoint": "<test-cosmos-endpoint>",
      "database": "testdb",
      "container": "vectors",
      "auth_type": "managed-identity",
      "client_id": "<identity-client-id>",
      "vector_dimensions": 1536
    }
  }'
```

Sources are created **enabled** by default.

### Step 8: Create Pipeline, Insert Documents, Activate

Pipelines are created in **paused** state. Insert documents first, then resume.

```powershell
# Create pipeline (paused)
curl -X POST http://<IP>/api/pipelines \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-pipeline",
    "sources": [{"source_id": "<src-id>", "filters": {}}],
    "destination_id": "<dst-id>",
    "docgrok_pipeline": "<model-id>",
    "process_existing": true
  }'
```

Insert test documents via the API pod (managed identity):

```powershell
kubectl exec deployment/omnivec-api -n omnivec -- python3 -c "
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
import os
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('<test-endpoint>', credential=cred)
c = client.get_database_client('testdb').get_container_client('test-documents')
docs = [
    {'id': 'doc-001', 'title': 'Azure Cosmos DB', 'content': 'Azure Cosmos DB is a globally distributed multi-model database.', 'category': 'database'},
    {'id': 'doc-002', 'title': 'Azure Kubernetes Service', 'content': 'AKS simplifies deploying managed Kubernetes clusters.', 'category': 'compute'},
    {'id': 'doc-003', 'title': 'Azure Blob Storage', 'content': 'Blob Storage stores massive amounts of unstructured data.', 'category': 'storage'},
]
for doc in docs:
    c.upsert_item(doc)
    print(f'Inserted: {doc[\"id\"]}')
"
```

Resume and activate:
```powershell
curl -X POST http://<IP>/api/pipelines/<pip-id>/resume -H "Authorization: Bearer <token>"
curl -X POST http://<IP>/api/pipelines/<pip-id>/run -H "Authorization: Bearer <token>"
```

The change feed processor starts from the beginning and picks up all documents. Wait ~60 seconds for processing.

### Step 9: Verify Results

```
omnivec pipeline show <pip-id>
omnivec job list
```

Expected output:
```
Stats
  Documents Processed:   3
  Failed:                0
  Pending:               0
  Processing:            0
  Total Jobs:            3

Health Checks
  [ok] Triggers / Event Bus   change_feed: managed_by_controller
  [ok] Source                  connected
  [ok] Destination             connected
  [ok] Model                   DocGrok service healthy
```

All jobs should show `completed`:
```
ID                PIPELINE      SOURCE REF  STATUS     ERROR  CREATED
job-xxx-001       pip-xxx       doc-001     completed  -      30s ago
job-xxx-002       pip-xxx       doc-002     completed  -      30s ago
job-xxx-003       pip-xxx       doc-003     completed  -      30s ago
```

#### Vector Search

```powershell
curl -X POST http://<IP>/api/playground/search \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is cosmos db", "destination_id": "<dst-id>", "top_k": 3}'
```

#### Pipeline Reset

```
omnivec pipeline reset <pip-id> -y
```

Clears all jobs and stats. The change feed processor restarts and reprocesses all documents from the beginning.

---

## Test 2: Inline Mode (Same Container as Source and Destination)

In inline mode, the embedding is patched directly into the source document — no separate vectors container needed. The source container IS the destination.

This test reuses the same `test-documents` container from Test 1 as both source and destination. The container needs a vector embedding policy, so we recreate it.

### Recreate the source container with vector policy

Since the container now stores embeddings too, it needs a vector policy:

```powershell
kubectl exec -i deployment/omnivec-api -n omnivec -- python3 - "<test-cosmos-endpoint>" <<'PYEOF'
import sys, os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient(sys.argv[1], credential=cred)
db = client.get_database_client("testdb")
try:
    db.delete_container("test-documents")
except: pass
vp = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": 1536}]}
ip = {"includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": "/embedding/*"}], "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
db.create_container(id="test-documents", partition_key={"paths": ["/id"], "kind": "Hash"}, vector_embedding_policy=vp, indexing_policy=ip)
print("test-documents recreated with vector policy (1536d, cosine, quantizedFlat)")
PYEOF
```

### Create source and destination pointing to the SAME container

Both source and destination use `test-documents`:

```powershell
# Source — same as Test 1
curl -X POST http://<IP>/api/sources \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "inline-source",
    "type": "cosmosdb",
    "config": {
      "endpoint": "<test-cosmos-endpoint>",
      "database": "testdb",
      "container": "test-documents",
      "auth_type": "managed-identity",
      "client_id": "<identity-client-id>"
    }
  }'

# Destination — SAME endpoint, database, and container as source
curl -X POST http://<IP>/api/destinations \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "inline-destination",
    "type": "cosmosdb-vector",
    "config": {
      "endpoint": "<test-cosmos-endpoint>",
      "database": "testdb",
      "container": "test-documents",
      "auth_type": "managed-identity",
      "client_id": "<identity-client-id>",
      "vector_dimensions": 1536
    }
  }'
```

### Create pipeline (paused), insert docs, resume

```powershell
# Create pipeline
curl -X POST http://<IP>/api/pipelines \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-inline-pipeline",
    "sources": [{"source_id": "<src-id>", "filters": {}}],
    "destination_id": "<dst-id>",
    "docgrok_pipeline": "<model-id>",
    "process_existing": true
  }'

# Insert documents into test-documents (same container used as source AND destination)
kubectl exec -i deployment/omnivec-api -n omnivec -- python3 - "<test-cosmos-endpoint>" <<'PYEOF'
import sys, os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient(sys.argv[1], credential=cred)
c = client.get_database_client("testdb").get_container_client("test-documents")
docs = [
    {"id": "doc-001", "title": "Azure Cosmos DB", "content": "Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling."},
    {"id": "doc-002", "title": "Azure Kubernetes Service", "content": "AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead."},
    {"id": "doc-003", "title": "Azure Blob Storage", "content": "Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale."},
]
for doc in docs:
    c.upsert_item(doc)
    print(f"Inserted: {doc['id']} - {doc['title']}")
PYEOF

# Resume pipeline
curl -X POST http://<IP>/api/pipelines/<pip-id>/resume -H "Authorization: Bearer <token>"
curl -X POST http://<IP>/api/pipelines/<pip-id>/run -H "Authorization: Bearer <token>"
```

Wait 60 seconds for processing.

### Verify inline embedding

After processing, the source documents now have the `embedding` field patched in-place. The original document fields (`title`, `content`, `category`) are preserved and `embedding`, `embedding_dims`, `pipeline_id`, `embedded_at` are added:

```powershell
kubectl exec -i deployment/omnivec-api -n omnivec -- python3 - "<test-cosmos-endpoint>" <<'PYEOF'
import sys, os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient(sys.argv[1], credential=cred)
c = client.get_database_client("testdb").get_container_client("test-documents")
for doc in c.query_items("SELECT c.id, c.title, c.embedding_dims, c.pipeline_id, c.embedded_at FROM c", enable_cross_partition_query=True):
    dims = doc.get("embedding_dims", "none")
    pip = doc.get("pipeline_id", "none")
    at = doc.get("embedded_at", "none")
    print(f"  {doc['id']} — {doc['title']} — dims: {dims}, pipeline: {pip}, at: {at}")
PYEOF
```

Expected output:
```
  doc-001 — Azure Cosmos DB — dims: 1536, pipeline: pip-xxx, at: 2026-03-23T...
  doc-002 — Azure Kubernetes Service — dims: 1536, pipeline: pip-xxx, at: 2026-03-23T...
  doc-003 — Azure Blob Storage — dims: 1536, pipeline: pip-xxx, at: 2026-03-23T...
```

The embedding was patched into the original document — no separate vectors container needed. You can search directly on `test-documents`.

### How inline mode differs from separate mode

| Aspect | Separate Containers | Inline (Same Container) |
|--------|-------------------|------------------------|
| Write operation | `upsert_item` to destination | `patch_item` on source document |
| Document structure | New doc with id + embedding + metadata | Original doc + embedding field added |
| Container count | 2 (source + vectors) | 1 (source = destination) |
| Vector policy | Required on destination container | Required on source container |
| Use case | Keep raw data separate from vectors | Keep everything together |
| Search | Search destination container | Search source container directly |

---

## Architecture

```
                    +------------------+
                    |   Azure OpenAI   |
                    | text-embedding-  |
                    |   3-small        |
                    +--------+---------+
                             ^
                             | embed
                             |
+----------+    change    +--+---+    write    +----------+
|  Source   |    feed     | Job  |   vectors   |  Dest    |
| CosmosDB +------------>+Proc- +------------->+ CosmosDB |
| test-docs |   detect    |essor |   upsert    | vectors  |
+----------+   new docs   +------+             +----------+
     ^                       ^
     |                       |
     |              +--------+--------+
     |              |   Controller    |
     |              | (bookkeeper)    |
     |              +-----------------+
     |
     |         +-----------------------+
     +-------->| Change Feed Processor |
               |  (.NET, 15 replicas)  |
               +-----------------------+
```

**Flow:**
1. Documents are inserted into the source container (`test-documents`)
2. The .NET Change Feed Processor detects new/modified documents
3. Jobs are created in the metadata store
4. The worker picks up jobs, calls DocGrok router for embedding (Azure OpenAI)
5. Vectors are upserted into the destination container (`vectors`)
6. Pipeline stats are updated (documents_processed, embedded_count, completion_pct)

**Same vs Separate Container:**
- If source and destination are the **same** container: patch in-place (add embedding field to existing doc)
- If **different** containers: upsert to destination (create new doc with embedding)

---

## Troubleshooting

### 401 Unauthorized from changefeed/controller

**Symptom:** Changefeed logs show `401 Unauthorized` when calling `/api/sources`.

**Cause:** Internal services (changefeed, controller, worker) call the API without a Bearer token.

**Fix:** The API bypasses auth for requests with `Host: omnivec-api` (internal K8s DNS). Ensure you are running the latest API image with the internal auth bypass.

### NotFound on Patch (Entity does not exist)

**Symptom:** Jobs fail with `(NotFound) Entity with the specified id does not exist in the system`.

**Cause:** The worker tries to `patch_item` in the destination container, but the document only exists in the source container.

**Fix:** Use the latest API image. When source and destination are different containers, it uses `upsert_item` instead of `patch_item`.

### readMetadata RBAC Error

**Symptom:** `Request blocked by Auth: principal does not have required RBAC permissions to perform action readMetadata`.

**Cause:** The managed identity only has the SQL Data Contributor role. The Python SDK's `CosmosClient()` constructor calls `readMetadata` which requires ARM-level access.

**Fix:** Grant **both** roles:
1. Cosmos DB Built-in Data Contributor (SQL role) -- for data operations
2. Cosmos DB Account Reader Role (ARM role) -- for SDK initialization

### Source Created Disabled

**Symptom:** Source shows `enabled: false` after creation.

**Cause:** Older API versions disabled sources when the connection test failed.

**Fix:** Use the latest API image. Sources are now always created enabled. Connection test failures are reported as warnings only.

### DeploymentNotFound from Azure OpenAI

**Symptom:** Jobs fail with `DeploymentNotFound: The API deployment 'xxx' not found`.

**Cause:** The `deployment` field in the model registration doesn't match the actual Azure OpenAI deployment name.

**Fix:** When registering the model, set `deployment` to the exact name of your Azure OpenAI deployment (as shown in the Azure Portal under Deployments).

### Git Bash Path Mangling

**Symptom:** `az cosmosdb sql container create --partition-key-path "/id"` fails with `partition key path 'C:/Program Files/Git/id'`.

**Cause:** Git Bash on Windows converts `/id` to a Windows path.

**Fix:** Use PowerShell (`pwsh`) instead of Git Bash, or prefix with `MSYS_NO_PATHCONV=1`.

### Pipeline Shows 0% Embedded

**Symptom:** Pipeline stats show `Source Docs: 3, Embedded: 0, Progress: 0%`.

**Cause:** The vector documents don't have `pipeline_id` and `embedded_at` fields, which the stats query uses to count embedded docs.

**Fix:** Use the latest API image. The `_sync_write_vector` function now writes `pipeline_id`, `embedded_at`, and `embedding_dims` to each vector document.

---

## Cleanup

```powershell
# Delete everything (infra + local env)
azd down --purge --force

# Remove local env config
Remove-Item -Recurse -Force .azure/omnivec-e2e-demo
```

This deletes all Azure resources including both the infrastructure and test CosmosDB accounts.
