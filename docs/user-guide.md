# OmniVec User Guide

OmniVec is a universal vector ingestion platform. It connects document sources (blob storage, CosmosDB) to vector destinations (CosmosDB with vector search), automatically generating embeddings via configurable embedding pipelines.

**API Base URL**: `http://20.242.139.166`
**Web UI**: `http://20.242.139.166/ui`

---

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Source** | Where documents live (Azure Blob Storage, CosmosDB) |
| **Destination** | Where embeddings are stored (CosmosDB with vector index) |
| **Pipeline** | Connects source(s) to a destination via an embedding model |
| **Job** | A single document processing unit (download → embed → store) |
| **DocGrok Pipeline** | The embedding model/provider used to generate vectors |

### Architecture

OmniVec runs three components from a single container image:

| Component | Replicas | Role |
|-----------|----------|------|
| **API** | 1+ | FastAPI REST API + serves the web UI |
| **Controller** | 1 | Monitors active pipelines, enumerates blob sources, watches CosmosDB Change Feed, creates PENDING jobs, monitors job health |
| **Worker** | 1-N | Claims PENDING jobs, processes them (download → embed → store), listens to Storage Queue for blob events |

### How It Works

```
Source (blob/CosmosDB)
    ↓  controller detects new/changed documents
PENDING Job created
    ↓  worker claims job
Download content → DocGrok (embedding) → Write to destination
    ↓
COMPLETED Job (embedding stored)
```

**Event-driven blob processing:**
```
Blob uploaded → Event Grid → Storage Queue → Worker → PENDING Job → Process
```

---

## 1. Sources

A source defines where your documents are. OmniVec supports two source types.

### Azure Blob Storage Source

For files stored in Azure Blob Storage (PDFs, text files, images, etc.).

```bash
curl -X POST http://20.242.139.166/api/sources \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-pdf-storage",
    "type": "azure-blob",
    "config": {
      "account_url": "https://mystorageaccount.blob.core.windows.net",
      "container": "documents",
      "prefix": "invoices/",
      "file_types": ["pdf", "txt", "docx", "md", "json", "csv"]
    }
  }'
```

**Config fields:**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `account_url` | Yes* | — | Storage account URL (for managed identity auth) |
| `connection_string` | Yes* | — | Connection string (alternative to account_url) |
| `container` | Yes | — | Blob container name |
| `prefix` | No | `""` | Only process blobs under this prefix |
| `file_types` | No | `["txt","json","pdf","md","csv"]` | File extensions to process |

*One of `account_url` or `connection_string` is required.

**Processing modes:**
- **Polling (controller):** The controller enumerates all blobs matching the prefix and file types every 10 seconds. For each new blob (not already processed), it creates a PENDING job.
- **Event-driven:** When Event Grid is configured, blob uploads trigger immediate processing via Storage Queue without waiting for the next poll cycle.
- **Backfill:** When a pipeline is activated with `process_existing: true`, all existing blobs in the container are enumerated and processed.

### CosmosDB Source

For documents stored in Azure CosmosDB. The content to embed is read from a document field.

```bash
curl -X POST http://20.242.139.166/api/sources \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-cosmos-documents",
    "type": "cosmosdb",
    "config": {
      "endpoint": "https://my-cosmos-account.documents.azure.com:443/",
      "database": "mydb",
      "container": "documents",
      "content_field": "content",
      "content_mode": "field"
    }
  }'
```

**Config fields:**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `endpoint` | Yes | — | CosmosDB account endpoint |
| `database` | Yes | — | Database name |
| `container` | Yes | — | Container name |
| `content_field` | No | `"content"` | Document field containing text to embed |
| `content_mode` | No | `"field"` | How to get content: `field`, `blob_url`, `http_url`, `s3_url` |
| `query` | No | `"SELECT * FROM c"` | Custom query to filter documents |
| `use_change_feed` | No | `true` | Use Change Feed for real-time detection |

**How it works:** The controller subscribes to the container's Change Feed. When documents are created or updated, it checks whether embedding is needed (no existing embedding, or content has changed based on SHA256 hash). If so, it creates a PENDING job. The worker reads the document content, embeds it, and **patches the embedding directly into the original document** (no separate vector document is created).

**Content hash skipping:** After the first embedding, a `content_hash` (SHA256) is stored alongside the embedding. On subsequent Change Feed events, if the content hash matches, the document is skipped — no expensive embedding call is made. Only actual content changes trigger re-embedding.

### Test a Source Connection

Before creating a source, you can test connectivity. Test connections have a **10-second timeout** and return clear error messages on failure.

```bash
# Test before saving
curl -X POST http://20.242.139.166/api/sources/test-connection \
  -H "Content-Type: application/json" \
  -d '{
    "type": "azure-blob",
    "config": {
      "account_url": "https://mystorageaccount.blob.core.windows.net",
      "container": "documents"
    }
  }'

# Test an existing source
curl -X POST http://20.242.139.166/api/sources/src-abc12345/test
```

**Common test connection errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| "Access denied" | Managed identity lacks permissions | Grant `Storage Blob Data Reader` (blob) or `Cosmos DB Built-in Data Contributor` (CosmosDB) |
| "Resource not found" | Wrong account URL, database, or container name | Verify the endpoint and resource names |
| "Connection timed out" | Network issue or firewall blocking access | Check NSG rules and storage account firewall settings |

---

## 2. Destinations

A destination defines where embeddings are stored. Currently, CosmosDB with vector search is supported.

### CosmosDB Vector Destination

Your CosmosDB container must have a **vector embedding policy** and **vector index** configured.

```bash
curl -X POST http://20.242.139.166/api/destinations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-vector-store",
    "type": "cosmosdb-vector",
    "config": {
      "endpoint": "https://my-cosmos-account.documents.azure.com:443/",
      "database": "documents",
      "container": "vectors",
      "vector_field": "embedding",
      "vector_dimensions": 3072,
      "vector_index_type": "quantizedFlat"
    }
  }'
```

**Config fields:**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `endpoint` | Yes | — | CosmosDB account endpoint |
| `database` | Yes | — | Database name |
| `container` | Yes | — | Container name (must have vector policy) |
| `vector_field` | No | `"embedding"` | Field name for the embedding vector |
| `vector_dimensions` | No | `1024` | Number of embedding dimensions |
| `vector_index_type` | No | `"quantizedFlat"` | Index type: `flat`, `quantizedFlat`, `diskANN` |
| `partition_key_path` | No | `"/source_id"` | Partition key path for the container |
| `id_field` | No | `"id"` | Document ID field |

**Prerequisites:** The CosmosDB container must be created with:
- A **vector embedding policy** specifying path, data type, dimensions, and distance function
- A **vector index** on the embedding path

Example container creation (using ARM REST API for vector support):
```bash
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.DocumentDB/databaseAccounts/{account}/sqlDatabases/{db}/containers/{container}?api-version=2024-05-15" \
  --body '{
    "location": "eastus",
    "properties": {
      "resource": {
        "id": "vectors",
        "partitionKey": {"paths": ["/source_id"], "kind": "Hash"},
        "indexingPolicy": {
          "indexingMode": "consistent",
          "includedPaths": [{"path": "/*"}],
          "excludedPaths": [{"path": "/embedding/*"}, {"path": "/_etag/?"}],
          "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]
        },
        "vectorEmbeddingPolicy": {
          "vectorEmbeddings": [{
            "path": "/embedding",
            "dataType": "float32",
            "dimensions": 3072,
            "distanceFunction": "cosine"
          }]
        }
      }
    }
  }'
```

### Test a Destination Connection

```bash
curl -X POST http://20.242.139.166/api/destinations/test-connection \
  -H "Content-Type: application/json" \
  -d '{
    "type": "cosmosdb-vector",
    "config": {
      "endpoint": "https://my-cosmos-account.documents.azure.com:443/",
      "database": "documents",
      "container": "vectors"
    }
  }'
```

This returns vector index details (dimensions, distance function, index type).

---

## 3. DocGrok Embedding Pipelines

DocGrok is the embedding engine. You select a DocGrok pipeline when creating an OmniVec pipeline. Each DocGrok pipeline specifies which model/provider generates embeddings.

### Available Pipelines

| Pipeline Name | Use Case |
|---------------|----------|
| `azure-openai-text-embedding-3-large-auto` | Azure OpenAI text-embedding-3-large (3072 dims). Best for text. |
| `text-azure` | Azure OpenAI text embeddings |
| `text-bge` | BGE text embedding model (local) |
| `pdf-text-azure` | PDF → text extraction → Azure OpenAI embedding |
| `pdf-vision` | PDF → page images → vision model embedding |
| `pdf-ocr-text` | PDF → OCR text extraction → embedding |
| `image-clip` | Image → CLIP embedding |

### List Available Pipelines

```bash
curl http://20.242.139.166/api/docgrok/pipelines
```

### Create a Custom Pipeline

```bash
curl -X POST http://20.242.139.166/api/docgrok/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-custom-pipeline",
    "function": "embed-text",
    "model": "text-embedding-3-large",
    "provider": "azure-openai"
  }'
```

---

## 4. Pipelines

A pipeline connects one or more sources to a destination through an embedding model. This is where everything comes together.

### Create a Pipeline

```bash
curl -X POST http://20.242.139.166/api/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "name": "embed-my-documents",
    "description": "Embed all documents from blob storage",
    "sources": [
      {
        "source_id": "src-abc12345",
        "filters": {}
      }
    ],
    "docgrok_pipeline": "azure-openai-text-embedding-3-large-auto",
    "destination_id": "dst-xyz67890",
    "process_existing": true
  }'
```

**Fields:**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Unique pipeline name |
| `description` | No | `""` | Human-readable description |
| `sources` | Yes | — | List of `{source_id, filters}` objects |
| `docgrok_pipeline` | Yes | — | Name of the DocGrok pipeline to use |
| `destination_id` | Yes | — | Destination ID for storing embeddings |
| `process_existing` | No | `true` | Process existing documents immediately |
| `metadata_mapping` | No | `{}` | Map source fields to destination fields |

**`process_existing` behavior:**
- `true` (default): Pipeline starts as **ACTIVE**. The controller immediately begins enumerating the source and creating jobs for all existing documents.
- `false`: Pipeline starts as **PAUSED**. No processing until you explicitly activate it.

### Pipeline Lifecycle

```
Created (process_existing=true)  →  ACTIVE  →  Processing documents...
Created (process_existing=false) →  PAUSED  →  (waiting)

ACTIVE  ──pause──→  PAUSED  ──resume──→  ACTIVE
```

### Backfill Testing

To verify that pre-existing documents get processed when a pipeline is created:

1. Upload documents to your source (blob container or CosmosDB) **before** creating the pipeline
2. Create the pipeline with `process_existing: true`
3. The controller will enumerate all existing documents and create PENDING jobs
4. Workers process each job — check the Jobs page in the UI for progress

### Manage Pipelines

```bash
# List all pipelines with stats
curl http://20.242.139.166/api/pipelines

# Get specific pipeline
curl http://20.242.139.166/api/pipelines/pip-abc12345

# Pause a pipeline
curl -X POST http://20.242.139.166/api/pipelines/pip-abc12345/pause

# Resume a pipeline
curl -X POST http://20.242.139.166/api/pipelines/pip-abc12345/resume

# Activate for processing
curl -X POST http://20.242.139.166/api/pipelines/pip-abc12345/run

# Delete a pipeline
curl -X DELETE http://20.242.139.166/api/pipelines/pip-abc12345
```

---

## 5. Jobs

Jobs are the individual work units. Each job represents one document being processed (downloaded, embedded, stored).

### Job Lifecycle

```
PENDING  →  PROCESSING  →  COMPLETED
                ↓
              FAILED  →  (auto-retry up to 3 times)  →  PENDING
                ↓
              FAILED (max retries exceeded)
```

- **PENDING**: Job created, waiting for a worker to claim it
- **PROCESSING**: Worker has claimed the job and is processing it
- **COMPLETED**: Embedding generated and stored successfully (or skipped if content unchanged)
- **FAILED**: Processing failed (timeout, DocGrok error, etc.)

### Monitor Jobs

```bash
# List all jobs (latest first)
curl http://20.242.139.166/api/jobs

# Filter by pipeline
curl "http://20.242.139.166/api/jobs?pipeline_id=pip-abc12345"

# Filter by status
curl "http://20.242.139.166/api/jobs?status=failed"

# Get job stats
curl http://20.242.139.166/api/jobs/stats

# Retry a failed job
curl -X POST http://20.242.139.166/api/jobs/job-abc12345678/retry

# Cancel a pending job
curl -X POST http://20.242.139.166/api/jobs/job-abc12345678/cancel
```

### Automatic Retries

The controller monitors job health every 10 seconds:
- **Stuck jobs**: If PROCESSING for more than 10 minutes → marked FAILED
- **Failed jobs**: Automatically reset to PENDING (up to 3 retries)

---

## 6. Processing Modes

### Mode 1: Blob Storage → New Vector Documents

When the source is Azure Blob Storage, the pipeline creates **new documents** in the destination container for each processed blob.

**Source document** (blob: `invoices/invoice-001.pdf`)
**Destination document:**
```json
{
  "id": "job-abc12345678",
  "embedding": [0.012, -0.034, ...],
  "source": "my-pdf-storage",
  "source_id": "src-abc12345",
  "source_ref": "invoices/invoice-001.pdf",
  "pipeline": "embed-my-documents",
  "blobUrl": "https://mystorageaccount.blob.core.windows.net/documents/invoices/invoice-001.pdf"
}
```

### Mode 2: CosmosDB → Patch-in-Place

When the source is CosmosDB, the pipeline **patches the original document** with the embedding. No new document is created.

**Before processing:**
```json
{
  "id": "doc-12345678",
  "source_id": "src-test-batch",
  "title": "Machine Learning Basics",
  "content": "Machine learning is a subset of AI..."
}
```

**After processing:**
```json
{
  "id": "doc-12345678",
  "source_id": "src-test-batch",
  "title": "Machine Learning Basics",
  "content": "Machine learning is a subset of AI...",
  "embedding": [0.012, -0.034, ...],
  "content_hash": "a1b2c3d4e5f6...",
  "embedded_at": "2026-02-12T10:07:18",
  "embedding_dims": 3072,
  "pipeline_id": "pip-59720cb3",
  "pipeline_name": "embed-my-documents"
}
```

All original fields are preserved. Only the embedding and metadata fields are added via CosmosDB patch operation.

### Content Change Detection (CosmosDB Sources)

After the initial embedding, OmniVec tracks content changes using SHA256:

| Scenario | Action |
|----------|--------|
| New document (no embedding) | Embed and store hash |
| Document updated, content changed (hash mismatch) | Re-embed with new hash |
| Document updated, content unchanged (hash match) | Skip (no embedding call) |
| Document updated, non-content field changed | Skip (hash still matches) |

This prevents unnecessary embedding API calls when only metadata changes.

---

## 7. Using Source and Destination as Same Container

A powerful pattern is using the same CosmosDB container as both source and destination. Documents are enriched in-place with embeddings.

### Setup

```bash
# 1. Create source pointing to the container
curl -X POST http://20.242.139.166/api/sources \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-documents-source",
    "type": "cosmosdb",
    "config": {
      "endpoint": "https://my-cosmos.documents.azure.com:443/",
      "database": "documents",
      "container": "vectors",
      "content_field": "content"
    }
  }'

# 2. Create destination pointing to the SAME container
curl -X POST http://20.242.139.166/api/destinations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-documents-destination",
    "type": "cosmosdb-vector",
    "config": {
      "endpoint": "https://my-cosmos.documents.azure.com:443/",
      "database": "documents",
      "container": "vectors",
      "vector_field": "embedding",
      "vector_dimensions": 3072
    }
  }'

# 3. Create pipeline connecting them
curl -X POST http://20.242.139.166/api/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "name": "enrich-documents",
    "sources": [{"source_id": "src-XXXXXXXX"}],
    "docgrok_pipeline": "azure-openai-text-embedding-3-large-auto",
    "destination_id": "dst-YYYYYYYY",
    "process_existing": true
  }'
```

### What Happens

1. The controller reads the Change Feed from the beginning
2. All existing documents are detected
3. PENDING jobs are created for each document without an embedding
4. Workers process each document: read content → generate embedding → patch in-place
5. New documents added later are automatically detected and processed
6. Content updates trigger re-embedding; metadata-only changes are skipped

---

## 8. Event-Driven Blob Processing

For real-time blob processing without polling delay, OmniVec supports event-driven processing via Azure Event Grid and Storage Queues.

### How It Works

```
Blob uploaded to storage account
    ↓
Azure Event Grid (system topic on storage account)
    ↓
Event Grid Subscription (filters: BlobCreated)
    ↓
Azure Storage Queue ("blob-events")
    ↓
OmniVec Worker (polls queue)
    ↓
Matches blob to active pipeline → creates PENDING job → processes
```

### Setup

Event Grid triggers are managed via the API or UI:

```bash
# Create Event Grid subscription for a blob source
curl -X POST http://20.242.139.166/api/triggers/eventgrid/create \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "src-abc12345"
  }'

# List active triggers
curl http://20.242.139.166/api/triggers/eventgrid/list

# Check trigger status
curl http://20.242.139.166/api/triggers/status

# Delete a trigger
curl -X DELETE http://20.242.139.166/api/triggers/eventgrid/src-abc12345
```

### Requirements

- Event Grid system topic on the storage account
- Storage Queue (`blob-events`) for receiving events
- Worker deployment with `STORAGE_CONN_STRING` environment variable

---

## 9. Operations — Deployment Management

The Operations page lets you monitor and control the OmniVec infrastructure directly from the UI.

### Access

Navigate to **Operations > Deployments** in the web UI, or use the API:

```bash
# List all deployments with pod details
curl http://20.242.139.166/api/operations/deployments
```

Returns:
```json
[
  {
    "name": "omnivec-api",
    "replicas": 1,
    "ready_replicas": 1,
    "available_replicas": 1,
    "status": "Running",
    "image": "cdbmvsacr4cc259.azurecr.io/omnivec-api:v50",
    "pods": [
      {"name": "omnivec-api-65795ff4b7-lljzw", "status": "Running", "restarts": 0, "age": "2h15m"}
    ]
  }
]
```

### Scale Deployments

```bash
# Scale worker to 3 replicas
curl -X POST http://20.242.139.166/api/operations/deployments/omnivec-worker/scale \
  -H "Content-Type: application/json" \
  -d '{"replicas": 3}'

# Pause controller (scale to 0)
curl -X POST http://20.242.139.166/api/operations/deployments/omnivec-controller/scale \
  -H "Content-Type: application/json" \
  -d '{"replicas": 0}'

# Resume controller
curl -X POST http://20.242.139.166/api/operations/deployments/omnivec-controller/scale \
  -H "Content-Type: application/json" \
  -d '{"replicas": 1}'
```

### Restart Deployments

```bash
# Rolling restart of worker
curl -X POST http://20.242.139.166/api/operations/deployments/omnivec-worker/restart
```

### Deployment Status

| Status | Meaning |
|--------|---------|
| **Running** | All replicas ready |
| **Degraded** | Some replicas not ready |
| **Stopped** | Scaled to 0 (paused) |

### UI Controls

Each deployment card in the UI shows:
- Name, image tag, status badge
- Ready/desired replica count
- Pod table (name, status, restarts, age)
- Action buttons: Scale +/-, Restart, Pause/Resume

The page auto-refreshes every 10 seconds.

---

## 10. Metrics & Monitoring

### Dashboard Metrics

```bash
curl http://20.242.139.166/api/metrics
```

Returns:
```json
{
  "events_processed": 165,
  "events_failed": 3,
  "avg_processing_time_ms": 1250.5,
  "today": {"processed": 52, "failed": 0},
  "daily": [
    {"date": "2026-02-12", "processed": 52, "failed": 0, "processing_time_ms": 65026.3}
  ],
  "pipelines": {
    "pip-59720cb3": {"processed": 52, "failed": 0, "processing_time_ms": 65026.3}
  }
}
```

### Health Check

```bash
curl http://20.242.139.166/health
```

Returns system health including DocGrok status, source/destination/pipeline counts, and job stats.

---

## 11. Vector Search (Playground)

Once documents have embeddings, you can search them:

```bash
curl -X POST http://20.242.139.166/api/playground/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How does machine learning work?",
    "destination_id": "dst-YYYYYYYY",
    "top_k": 5
  }'
```

Returns the most similar documents ranked by cosine similarity.

---

## 12. Complete Example: End-to-End

### Scenario A: Embed text documents in CosmosDB (patch-in-place)

**Step 1:** Insert documents into your CosmosDB container (must have `content` field):

```python
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

client = CosmosClient("https://my-cosmos.documents.azure.com:443/", DefaultAzureCredential())
container = client.get_database_client("documents").get_container_client("vectors")

container.upsert_item({
    "id": "doc-001",
    "source_id": "my-batch",
    "title": "What is AI?",
    "content": "Artificial intelligence is the simulation of human intelligence by machines."
})
```

**Step 2:** Create source, destination, and pipeline via the UI or API (see sections above).

**Step 3:** Monitor progress on the Jobs page or via API:
```bash
curl "http://20.242.139.166/api/jobs?pipeline_id=pip-XXXXXXXX"
```

**Step 4:** Search your embedded documents:
```bash
curl -X POST http://20.242.139.166/api/playground/search \
  -H "Content-Type: application/json" \
  -d '{"query": "Tell me about AI", "destination_id": "dst-YYYYYYYY", "top_k": 5}'
```

**Step 5:** Add more documents — they are automatically detected and embedded via Change Feed.

**Step 6:** Update content — only changed content is re-embedded (SHA256 hash comparison).

### Scenario B: Backfill blob storage documents

1. Upload 100 files to a blob container
2. Create a source pointing to that container
3. Create a destination (CosmosDB with vector index)
4. Create a pipeline with `process_existing: true`
5. The controller enumerates all 100 blobs and creates PENDING jobs
6. Workers process each blob: download → embed → write vector document
7. Monitor progress on the Jobs page

---

## 13. Authentication

OmniVec uses **Azure Managed Identity** (DefaultAzureCredential) for all Azure service connections. No keys or connection strings needed for:
- CosmosDB (requires data plane RBAC: `Cosmos DB Built-in Data Contributor`)
- Azure Blob Storage (requires `Storage Blob Data Reader` for reading, `Storage Blob Data Contributor` for writing)

CosmosDB data plane RBAC is assigned via CLI (not available in portal):
```bash
az cosmosdb sql role assignment create \
  --account-name my-cosmos-account \
  --resource-group my-rg \
  --role-definition-id "00000000-0000-0000-0000-000000000002" \
  --principal-id "<managed-identity-principal-id>" \
  --scope "/"
```

**Important:** CosmosDB data plane RBAC propagation can take 5-10 minutes after granting.

---

## 14. Deployment

### Building and Deploying

OmniVec uses a single Docker image for all three components. Build via Azure Container Registry (no local Docker daemon needed):

```bash
# Build new image
cd /home/cdbmvs/omnivec
az acr build --registry cdbmvsacr4cc259 --image omnivec-api:vXX --file api/Dockerfile .

# Deploy to all components
kubectl set image deployment/omnivec-api api=cdbmvsacr4cc259.azurecr.io/omnivec-api:vXX -n omnivec
kubectl set image deployment/omnivec-controller controller=cdbmvsacr4cc259.azurecr.io/omnivec-api:vXX -n omnivec
kubectl set image deployment/omnivec-worker worker=cdbmvsacr4cc259.azurecr.io/omnivec-api:vXX -n omnivec
```

**Important:** Always increment the image tag version. With `imagePullPolicy: Always`, K8s will pull the new image on each deployment.

### Scaling Workers

Scale workers up for faster processing of large batches:

```bash
# Via kubectl
kubectl scale deployment/omnivec-worker --replicas=5 -n omnivec

# Via API
curl -X POST http://20.242.139.166/api/operations/deployments/omnivec-worker/scale \
  -H "Content-Type: application/json" \
  -d '{"replicas": 5}'
```

---

## Quick Reference

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| **Sources** | | |
| GET | `/api/sources` | List sources |
| POST | `/api/sources` | Create source |
| PUT | `/api/sources/{id}` | Update source |
| DELETE | `/api/sources/{id}` | Delete source |
| POST | `/api/sources/{id}/test` | Test source connection |
| POST | `/api/sources/test-connection` | Test connection before saving |
| **Destinations** | | |
| GET | `/api/destinations` | List destinations |
| POST | `/api/destinations` | Create destination |
| PUT | `/api/destinations/{id}` | Update destination |
| DELETE | `/api/destinations/{id}` | Delete destination |
| POST | `/api/destinations/{id}/test` | Test destination connection |
| POST | `/api/destinations/test-connection` | Test connection before saving |
| **Pipelines** | | |
| GET | `/api/pipelines` | List pipelines with stats |
| POST | `/api/pipelines` | Create pipeline |
| PUT | `/api/pipelines/{id}` | Update pipeline |
| DELETE | `/api/pipelines/{id}` | Delete pipeline |
| POST | `/api/pipelines/{id}/pause` | Pause pipeline |
| POST | `/api/pipelines/{id}/resume` | Resume pipeline |
| POST | `/api/pipelines/{id}/run` | Activate pipeline |
| **Jobs** | | |
| GET | `/api/jobs` | List jobs (supports `?pipeline_id=`, `?status=`) |
| GET | `/api/jobs/{id}` | Get job details |
| GET | `/api/jobs/stats` | Job statistics |
| POST | `/api/jobs/{id}/retry` | Retry failed job |
| POST | `/api/jobs/{id}/cancel` | Cancel pending job |
| **Operations** | | |
| GET | `/api/operations/deployments` | List deployments with pod details |
| POST | `/api/operations/deployments/{name}/scale` | Scale deployment (`{"replicas": N}`) |
| POST | `/api/operations/deployments/{name}/restart` | Rolling restart |
| **Triggers** | | |
| GET | `/api/triggers/status` | Trigger status |
| POST | `/api/triggers/eventgrid/create` | Create Event Grid trigger |
| DELETE | `/api/triggers/eventgrid/{source_id}` | Delete trigger |
| GET | `/api/triggers/eventgrid/list` | List triggers |
| **Other** | | |
| GET | `/api/metrics` | Processing metrics |
| POST | `/api/playground/search` | Vector similarity search |
| GET | `/api/docgrok/pipelines` | List embedding pipelines |
| GET | `/health` | System health |
| GET | `/ui` | Web UI |

### ID Prefixes

| Entity | Prefix | Example |
|--------|--------|---------|
| Source | `src-` | `src-abc12345` |
| Destination | `dst-` | `dst-xyz67890` |
| Pipeline | `pip-` | `pip-def45678` |
| Job | `job-` | `job-ghi901234567` |

### Current Infrastructure

| Resource | Name | Details |
|----------|------|---------|
| AKS Namespace | `omnivec` | API, Controller, Worker deployments |
| CosmosDB (control plane) | `omnivec-cosmos` | Database: `omnivec`, Container: `metadata` |
| ACR | `cdbmvsacr4cc259` | Image: `omnivec-api` |
| Storage (production) | `omnivecstore34719` | Container: `documents` |
| Storage (test) | `blobomnivectest` | Containers: `documents`, `predocuments` |
| CosmosDB (test) | `cosmosdb-omnivec-test` | Database: `documents`, Containers: `vectors`, `vectors2` |
| External IP | `20.242.139.166` | Load balancer for API |
