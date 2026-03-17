# OmniVec + DocGrok — Architecture Document

---

## 1. Executive Summary

**OmniVec** is a universal vector ingestion platform that connects diverse data sources (CosmosDB, Azure Blob Storage, S3, HTTP) to vector databases. It automates the end-to-end pipeline of discovering documents, extracting content, generating embeddings, and storing vectors — with real-time change detection, autoscaling, and a web-based management UI.

**DocGrok** is the embedded AI model orchestration layer. It provides a unified API for routing embedding requests to self-hosted GPU models (DSE-Qwen2, CLIP, BGE) or external providers (Azure OpenAI, OpenAI), with support for multi-step transform pipelines (e.g., PDF → OCR → text → embeddings).

Together, they form a complete **Data → Vectors** platform deployed on Azure Kubernetes Service (AKS).

---

## 2. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              OmniVec Platform                               │
│                                                                             │
│  ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌──────────────────────┐   │
│  │  Web UI   │──▶│ OmniVec   │──▶│  DocGrok  │──▶│  Embedding Models   │   │
│  │ (nginx)   │   │   API     │   │  Router   │   │  (GPU / External)   │   │
│  └──────────┘   └─────┬─────┘   └─────┬─────┘   └──────────────────────┘   │
│                       │               │                                     │
│            ┌──────────┼───────────────┼──────────────┐                      │
│            ▼          ▼               ▼              ▼                      │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌──────────────┐            │
│  │Controller │  │  Workers  │  │  Pipeline  │  │  DocGrok     │            │
│  │(bookkeep)│  │(job proc) │  │  Worker    │  │  Controller  │            │
│  └──────┬───┘  └─────┬─────┘  │(OCR/PDF)   │  │(health/scale)│            │
│         │            │        └────────────┘  └──────────────┘            │
│         ▼            ▼                                                     │
│  ┌───────────────────────────┐     ┌──────────────────────┐                │
│  │    Azure CosmosDB         │     │  Azure Blob Storage  │                │
│  │  (metadata + vectors)     │     │  (document source)   │                │
│  └───────────────────────────┘     └──────────────────────┘                │
│                                                                             │
│  ┌───────────────────┐   ┌──────────────────┐                              │
│  │  Change Feed       │   │  Event Grid +    │                              │
│  │  Processor (.NET)  │   │  Service Bus     │                              │
│  │  (CosmosDB CDC)    │   │  (Blob events)   │                              │
│  └───────────────────┘   └──────────────────┘                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. OmniVec Components

### 3.1 Web UI (omnivec-web)

| Attribute | Detail |
|-----------|--------|
| **Technology** | Static HTML/JS + nginx reverse proxy |
| **Image** | `omnivec-web:v1` |
| **Replicas** | 2 |
| **Resources** | 64Mi–128Mi RAM, 50m–250m CPU |
| **Service** | LoadBalancer (external IP) |

**Features:**
- **Dashboard** — Pipeline status, job stats, throughput metrics
- **Sources** — Create/edit/delete data sources with connection testing
- **Destinations** — Manage vector store targets
- **Pipelines** — Create pipelines linking sources → models → destinations
- **Vector Search** — Multi-index search playground with parallel queries and merge strategies
- **DocGrok Health** — Model status, GPU utilization, endpoint health
- **DocGrok Deployments** — Scale models and pipeline workers, view pods
- **OmniVec Health** — Component health, CosmosDB connectivity, worker status
- **OmniVec Deployments** — Scale API, workers, changefeed processors

**Routing (nginx):**
- `/api/*` → `omnivec-api:80` (OmniVec control plane)
- `/docgrok/*` → `docgrok:80` (DocGrok router)
- `/*` → Static files (SPA)

---

### 3.2 OmniVec API (omnivec-api) — Control Plane

| Attribute | Detail |
|-----------|--------|
| **Technology** | Python FastAPI |
| **Image** | `omnivec-api:v1` |
| **Replicas** | 2 |
| **Resources** | 256Mi–1Gi RAM, 250m–1 CPU |
| **Service** | ClusterIP (internal) |
| **Port** | 8080 |

**Key API Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/sources` | List all data sources |
| `POST` | `/api/sources` | Create a new source (with connection test) |
| `PUT` | `/api/sources/{id}` | Update source configuration |
| `DELETE` | `/api/sources/{id}` | Delete a source |
| `POST` | `/api/sources/{id}/sync` | Trigger source sync (enumerate & create jobs) |
| `GET` | `/api/destinations` | List all destinations |
| `POST` | `/api/destinations` | Create a destination (with connection test) |
| `GET` | `/api/pipelines` | List all pipelines |
| `POST` | `/api/pipelines` | Create a pipeline |
| `PUT` | `/api/pipelines/{id}` | Update pipeline settings |
| `DELETE` | `/api/pipelines/{id}` | Delete pipeline and its jobs |
| `GET` | `/api/pipelines/{id}/stats` | Get pipeline run statistics |
| `POST` | `/api/pipelines/{id}/reset` | Reset pipeline for reprocessing |
| `GET` | `/api/jobs` | List jobs (filterable by pipeline, status) |
| `POST` | `/api/jobs/{id}/retry` | Retry a failed job |
| `GET` | `/api/metrics` | Global processing metrics |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/deployments` | List OmniVec k8s deployments |
| `POST` | `/api/deployments/{name}/scale` | Scale a deployment |
| `POST` | `/api/search` | Multi-index vector search |

**Data Storage:**
- All metadata (sources, destinations, pipelines, jobs, metrics) stored in **Azure CosmosDB** container `metadata` with partition key `/doc_type`
- Document types: `source`, `destination`, `pipeline`, `job`, `metrics`

---

### 3.3 OmniVec Controller (Bookkeeper)

| Attribute | Detail |
|-----------|--------|
| **Technology** | Python (same image as API, different entrypoint) |
| **Replicas** | 1 (singleton) |
| **Resources** | 1Gi–2Gi RAM, 250m–500m CPU |

**Responsibilities:**
- **Source enumeration** — When a sync is triggered, iterates source documents and creates pending jobs
- **CosmosDB change feed monitoring** — Detects new/updated documents in source containers
- **Blob storage enumeration** — Lists blobs matching file type filters
- **Job lifecycle management** — Marks stale processing jobs as failed
- **Metrics aggregation** — Rolls up job stats into pipeline and global metrics

---

### 3.4 OmniVec Workers (Job Processors)

| Attribute | Detail |
|-----------|--------|
| **Technology** | Python (same image as API, different entrypoint) |
| **Replicas** | 1–10 (HPA autoscaled) |
| **Resources** | 512Mi–2Gi RAM, 500m–2 CPU |
| **Autoscaling** | HPA at 70% CPU utilization |

**Job Processing Flow:**

```
1. Poll pending jobs from CosmosDB (status = "pending")
2. Download content from source:
   - CosmosDB: read document field
   - Blob Storage: download blob
   - HTTP: fetch URL
3. Determine content type (PDF, TXT, JSON, images, etc.)
4. Apply content strategy:
   a. TRUNCATE: Send full text to DocGrok /embed endpoint
   b. CHUNK: Split text → send chunks to DocGrok /embed/batch
5. Write vector(s) to destination CosmosDB container
6. Update job status (completed/failed)
```

**Content Strategy — Truncate:**
- Sends entire document text as a single embedding request
- Produces one vector document per source document
- `doc_id_pattern`: configurable, default `{source}` (filename without extension)

**Content Strategy — Chunk:**
- Splits text into overlapping chunks (configurable size and overlap)
- Sends all chunks in a single batch embed request
- Produces N vector documents per source document (one per chunk)
- `doc_id_pattern`: configurable, default `{source}-chunk-{chunk}`
- Supports break-on-paragraph and break-on-sentence for cleaner chunks
- Cleans up old chunks before writing new ones (prefix-based deletion)

**Batch Processing:**
- Groups multiple jobs and sends texts in a single `/embed/batch` call
- Falls back to individual processing for binary content (images, raw PDFs)

---

### 3.5 Change Feed Processor (CFP)

| Attribute | Detail |
|-----------|--------|
| **Technology** | .NET (Azure CosmosDB SDK change feed processor) |
| **Image** | `omnivec-changefeed:v1` |
| **Replicas** | 15 (fixed, matches CosmosDB physical partitions) |
| **Resources** | 128Mi–512Mi RAM, 100m–500m CPU |

**Purpose:**
- Real-time Change Data Capture (CDC) from CosmosDB source containers
- Uses the CosmosDB SDK's built-in change feed processor with lease-based partitioning
- Each replica owns a subset of physical partitions (like Kafka consumer groups)
- On detecting a new/changed document, creates a pending job in the metadata store
- Supports `process_existing: true` to backfill all existing documents on pipeline creation

**Why 15 replicas?**
- CosmosDB distributes data across physical partitions
- Each CFP instance claims leases on specific partitions
- 15 replicas = 15 concurrent lease holders = maximum parallelism across partitions
- Scaling beyond the number of physical partitions provides no benefit

---

### 3.6 Blob Watcher

| Attribute | Detail |
|-----------|--------|
| **Technology** | Python (same image as API) |
| **Replicas** | 1 |
| **Status** | Optional (disabled by default) |

**Purpose:**
- Monitors Azure Blob Storage for new/modified files
- Uses Event Grid → Service Bus → Queue polling pattern
- On blob event, creates a pending job for matching pipelines
- Supports file type filtering (e.g., only process PDFs)

---

## 4. Data Model

### 4.1 Sources

Sources define where documents come from.

| Source Type | Description | Configuration |
|-------------|-------------|---------------|
| `cosmosdb` | Azure CosmosDB container | endpoint, database, container, content_field, content_mode |
| `azure-blob` | Azure Blob Storage container | account_url, container, prefix, file_types |
| `s3` | AWS S3 bucket | bucket, prefix, region, extensions |
| `http` | HTTP/HTTPS endpoint | url, method, headers, auth_type |

**CosmosDB Content Modes:**
- `field` — Content is directly in a document field (default)
- `blob_url` — Field contains an Azure Blob URL to download
- `http_url` — Field contains an HTTP URL to fetch
- `s3_url` — Field contains an S3 URL to download

### 4.2 Destinations

Destinations define where vectors are stored.

| Destination Type | Description | Configuration |
|------------------|-------------|---------------|
| `cosmosdb-vector` | CosmosDB with vector indexing | endpoint, database, container, vector_field, vector_dimensions, vector_index_type |

**Vector Index Types:**
- `flat` — Exact nearest-neighbor (small datasets)
- `quantizedFlat` — Quantized flat index (medium datasets)
- `diskANN` — Microsoft DiskANN index (large-scale, high-performance)

### 4.3 Pipelines

Pipelines connect sources to destinations through an embedding model.

```
Source ──▶ Content Extraction ──▶ DocGrok Model/Pipeline ──▶ Destination
           (text, PDF, image)      (embed / embed/batch)      (CosmosDB vectors)
```

**Pipeline Configuration:**
- `sources` — List of source IDs with optional filters
- `docgrok_pipeline` — Model ID (`mdl-*`) or transform pipeline (`trp-*`)
- `destination_id` — Where to write vectors
- `content_strategy` — `truncate` (one vector per doc) or `chunk` (split into chunks)
- `chunk_config` — Chunk size, overlap, unit (chars/tokens), doc_id_pattern
- `doc_id_pattern` — Template for vector document IDs
- `processing_mode` — `queue` (CFP→jobs→worker) or `inline`
- `process_existing` — Whether to backfill existing documents

### 4.4 Jobs

Jobs track individual document processing tasks.

| Status | Description |
|--------|-------------|
| `pending` | Created, waiting for worker |
| `processing` | Worker has claimed and is processing |
| `completed` | Successfully embedded and stored |
| `failed` | Processing error (retryable) |
| `cancelled` | Manually cancelled |

### 4.5 Trigger Types

| Trigger | Description | Mechanism |
|---------|-------------|-----------|
| `change-feed` | Real-time CosmosDB CDC | Change Feed Processor (.NET) |
| `event-grid` | Real-time blob events | Event Grid → Service Bus → Blob Watcher |
| `schedule` | Cron-based | Periodic source enumeration |
| `manual` | On-demand | API call to `/api/sources/{id}/sync` |

---

## 5. DocGrok — AI Model Orchestration

### 5.1 DocGrok Router (Orchestrator)

| Attribute | Detail |
|-----------|--------|
| **Technology** | Rust (Axum web framework) |
| **Image** | `docgrok-router:v1` |
| **Replicas** | 1 |
| **Resources** | 256Mi–1Gi RAM, 250m–1 CPU |
| **Service** | ClusterIP port 80 (internal to cluster) |

**Why Rust?**
- Sub-millisecond routing latency
- Minimal memory footprint
- Async I/O for high-throughput embedding proxy
- Zero-cost abstractions for model resolution logic

**Key Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/embed` | Single text/image embedding (model or pipeline) |
| `POST` | `/embed/batch` | Batch text embedding (model or pipeline) |
| `GET` | `/models` | List all available models (native + external) |
| `GET` | `/models/{id}` | Get model details |
| `GET` | `/health` | Router health |
| `GET` | `/health/models` | Health status of all models |
| `POST` | `/models/{id}/scale` | Scale model replicas |
| `GET` | `/deployments` | List DocGrok k8s deployments |
| `POST` | `/deployments/{name}/scale` | Scale a deployment |
| `GET` | `/pipelines` | List transform pipelines |
| `POST` | `/pipelines` | Create a transform pipeline |
| `PUT` | `/pipelines/{id}` | Update a transform pipeline |
| `DELETE` | `/pipelines/{id}` | Delete a transform pipeline |

**Model Discovery:**
The router discovers models from two sources:

1. **Native Models** (self-hosted on GPU):
   - Discovered via Kubernetes API — scans deployments with label `role=model`
   - Monitors health via periodic `/health` calls
   - Model ID format: `mdl-{hash}` (e.g., `mdl-a1b2c3d4`)
   - Examples: DSE-Qwen2, CLIP, BGE, BGE-Small

2. **External Models** (cloud API providers):
   - Configured in Helm values under `providers`
   - Supported: Azure OpenAI, OpenAI
   - Model ID format: `mdl-ext-{hash}`
   - Example: `text-embedding-3-small` via Azure OpenAI

**Embed Request Routing:**

```
Client sends: POST /embed { "model_id": "mdl-xxx", "text": "..." }
                    — or —
              POST /embed { "pipeline": "trp-xxx", "text": "..." }

Router resolves:
  If model_id starts with "mdl-ext-" → call external API (Azure OpenAI)
  If model_id starts with "mdl-"     → forward to native model service
  If pipeline specified               → resolve pipeline config → route to worker
```

### 5.2 Model Types and Capabilities

| Model | Type | Dims | GPU | Use Case |
|-------|------|------|-----|----------|
| **DSE-Qwen2** | Visual Document | 1536 | 1x V100 (16Gi) | PDF page images → embeddings |
| **CLIP ViT-L/14** | Image | 768 | 1x V100 (4Gi) | General image embeddings |
| **BGE-Large-EN** | Text | 1024 | 1x V100 (4Gi) | English text embeddings |
| **BGE-Small-EN** | Text | 384 | 1x V100 (2Gi) | Lightweight text embeddings |
| **text-embedding-3-small** | Text (External) | 1536 | — | Azure OpenAI text embeddings |
| **text-embedding-3-large** | Text (External) | 3072 | — | Azure OpenAI high-dim text embeddings |

### 5.3 Transform Pipelines

Transform pipelines define multi-step processing workflows that go beyond simple embedding.

**Example: `trp-pdf-ocr-embed`**
```
PDF Binary ──▶ Pipeline Worker ──▶ PaddleOCR ──▶ Text ──▶ Embedding Model ──▶ Vectors
                (Python/FastAPI)    (OCR engine)           (via DocGrok Router)
```

**Pipeline Configuration:**
```json
{
  "id": "trp-pdf-ocr-embed",
  "name": "PDF OCR + Embed",
  "steps": [
    { "type": "ocr", "engine": "paddleocr" },
    { "type": "embed", "model": "mdl-ext-1a4fe0d0" }
  ]
}
```

**How it works:**
1. OmniVec worker sends `POST /embed { "pipeline": "trp-pdf-ocr-embed", "content": "<base64 PDF>" }` to DocGrok Router
2. Router looks up pipeline config, resolves the pipeline worker endpoint
3. Forwards request to Pipeline Worker
4. Pipeline Worker runs PaddleOCR to extract text from PDF
5. Pipeline Worker calls DocGrok Router `/embed { "model_id": "mdl-xxx", "text": "extracted text" }` to get embedding
6. Returns embedding back to OmniVec worker

For batch: `POST /embed/batch { "pipeline": "trp-xxx", "texts": [...] }` follows the same resolution pattern.

### 5.4 DocGrok Controller

| Attribute | Detail |
|-----------|--------|
| **Technology** | Rust (same binary as router, different mode) |
| **Replicas** | 1 |
| **Resources** | 128Mi–512Mi RAM, 100m–500m CPU |

**Responsibilities:**
- Periodic health checks on all registered models (every 60s)
- Updates model health status in shared state
- Monitors GPU utilization and pod resource consumption
- Persists scale state to CosmosDB (survives restarts)
- Restores deployment replicas on startup from saved state

### 5.5 Pipeline Worker

| Attribute | Detail |
|-----------|--------|
| **Technology** | Python FastAPI + PaddleOCR |
| **Image** | `docgrok-pipeline-worker:v1` |
| **Replicas** | 1 |
| **Resources** | 8Gi–12Gi RAM, 2–4 CPU (PaddleOCR is memory-intensive) |

**Capabilities:**
- PDF → PaddleOCR → text extraction
- Calls DocGrok Router for embedding after text extraction
- Returns both extracted text and embeddings
- Supports batch processing

**Why 8–12 Gi memory?**
PaddleOCR loads large ML models (detection + recognition + angle classification) into CPU memory. Processing multi-page PDFs requires significant memory for image buffers and intermediate results.

---

## 6. Infrastructure — Azure Resources

### 6.1 Provisioning

Infrastructure is provisioned via **Azure Developer CLI (azd)** with **Bicep** templates.

**Command:** `azd provision --profile <profile-name>`

### 6.2 Resource Map

| Resource | Service | Purpose |
|----------|---------|---------|
| **AKS Cluster** | Azure Kubernetes Service | Container orchestration |
| **System Node Pool** | Standard_D4s_v3 (2 nodes) | OmniVec + DocGrok workloads |
| **GPU Node Pool** | Standard_NC6s_v3 (4 nodes) | Self-hosted embedding models |
| **ACR** | Azure Container Registry | Docker image storage |
| **CosmosDB** | Azure Cosmos DB (NoSQL) | Metadata store + vector database |
| **Storage Account** | Azure Blob Storage | Document source (PDFs, images) |
| **Service Bus** | Azure Service Bus | Job queue for blob events |
| **Event Grid** | Azure Event Grid | Blob change notifications |
| **Managed Identity** | User-Assigned Managed Identity | Workload Identity for AKS pods |

### 6.3 Identity and Security

- **Workload Identity Federation**: AKS pods authenticate to Azure services using federated tokens
- No secrets stored in pods — managed identity handles all Azure SDK authentication
- **RBAC**: CosmosDB uses native RBAC (no master keys)
  - `Cosmos DB Built-in Data Reader` (role `00000000-0000-0000-0000-000000000001`)
  - `Cosmos DB Built-in Data Contributor` (role `00000000-0000-0000-0000-000000000002`)
- **ACR Pull**: AKS kubelet identity has `AcrPull` role on the container registry

---

## 7. Deployment Architecture

### 7.1 Kubernetes Namespace Layout

```
Namespace: omnivec
├── omnivec-web          (2 replicas, LoadBalancer)
├── omnivec-api          (2 replicas, ClusterIP)
├── omnivec-controller   (1 replica)
├── omnivec-worker       (1–10 replicas, HPA)
├── omnivec-changefeed   (15 replicas, fixed)
├── omnivec-blob-watcher (1 replica, optional)
├── docgrok              (1 replica, ClusterIP — the router)
├── docgrok-controller   (1 replica)
├── docgrok-pipeline-worker (1 replica, optional)
├── dse-qwen2            (0–N replicas, GPU, on-demand)
├── clip                 (0–N replicas, GPU, on-demand)
├── bge                  (0–N replicas, GPU, on-demand)
└── bge-small            (0–N replicas, GPU, on-demand)

Namespace: docgrok (standalone mode — not used when embedded in OmniVec)
```

### 7.2 Helm Charts

Two Helm charts manage deployment:

**`helm/omnivec/`** — Main chart
- Deploys all OmniVec components (web, api, controller, worker, changefeed, blob-watcher)
- Includes DocGrok as a subchart
- Configures workload identity, CosmosDB, storage, service bus

**`helm/docgrok/`** — Standalone DocGrok chart
- Can be deployed independently for model serving without OmniVec
- Defines model registry, external providers, router, controller, pipeline worker

### 7.3 Image Build Pipeline

```
Source Code ──▶ az acr build ──▶ ACR ──▶ kubectl rollout restart
                (cloud build)           (pulls new image)
```

Images are built in Azure Container Registry (ACR) using cloud builds — no local Docker needed:
```bash
az acr build --registry <acr> --image <name>:v1 --file <Dockerfile> <context>
```

---

## 8. Scaling Architecture

### 8.1 OmniVec Scaling

| Component | Scaling Strategy | Trigger |
|-----------|-----------------|---------|
| **API** | Manual (2 replicas default) | API request load |
| **Workers** | HPA (1–10 replicas) | CPU utilization > 70% |
| **Changefeed** | Fixed (15 replicas) | Matches CosmosDB physical partitions |
| **Web** | Manual (2 replicas default) | Frontend traffic |

### 8.2 DocGrok Model Scaling

Models can be scaled via the UI or API:

```
POST /docgrok/deployments/{name}/scale
{ "replicas": 3 }
```

- Scale state is persisted to CosmosDB (survives pod restarts)
- DocGrok controller restores saved replica counts on startup
- Models can be scaled to 0 (stopped) to save GPU resources
- GPU nodes use Kubernetes device plugin for `nvidia.com/gpu` scheduling

### 8.3 CosmosDB Scaling

- **Request Units (RU/s)**: Autoscale provisioned throughput
- **Partitioning**: All containers partitioned by `/id`
- **Vector Indexing**: Configurable per container (flat, quantizedFlat, diskANN)
- **Change Feed**: Automatically scales with physical partitions

---

## 9. Data Flow — End to End

### 9.1 CosmosDB Source → Embedding → In-Place Vector

```
┌─────────────────┐    Change Feed     ┌──────────────┐
│  CosmosDB        │ ──────────────────▶│ Change Feed  │
│  Source Container │                   │ Processor    │
│  (documents)     │                   └──────┬───────┘
└─────────────────┘                           │ creates job
                                              ▼
                                       ┌──────────────┐
                                       │ CosmosDB      │
                                       │ metadata      │
                                       │ (pending job) │
                                       └──────┬───────┘
                                              │ worker picks up
                                              ▼
                                       ┌──────────────┐     ┌──────────────┐
                                       │  Worker       │────▶│  DocGrok     │
                                       │  (job proc)   │     │  Router      │
                                       │               │◀────│  → Model     │
                                       └──────┬───────┘     └──────────────┘
                                              │ embedding vector
                                              ▼
                                       ┌─────────────────┐
                                       │  CosmosDB        │
                                       │  Dest Container  │
                                       │  (doc + vector)  │
                                       └─────────────────┘
```

### 9.2 Blob Storage → PDF OCR → Chunk → Embed

```
┌──────────────┐  Event Grid   ┌──────────────┐
│ Azure Blob    │─────────────▶│ Blob Watcher  │
│ Storage       │              │ (or manual    │
│ (PDF files)   │              │  sync)        │
└──────────────┘              └──────┬───────┘
                                     │ creates job per file
                                     ▼
                              ┌──────────────┐
                              │  Worker       │──▶ Download PDF blob
                              │  (job proc)   │
                              │               │──▶ Send to DocGrok Pipeline
                              └──────┬───────┘       │
                                     │               ▼
                                     │        ┌──────────────┐
                                     │        │ Pipeline     │
                                     │        │ Worker       │
                                     │        │ (PaddleOCR)  │
                                     │        └──────┬───────┘
                                     │               │ extracted text
                                     │               ▼
                                     │        ┌──────────────┐
                                     │        │ DocGrok      │
                                     │        │ Router       │
                                     │        │ → Model      │
                                     │        └──────┬───────┘
                                     │               │ embeddings
                                     ◀───────────────┘
                                     │
                                     │ Split text into chunks
                                     │ Create chunk documents:
                                     │   report-chunk-000
                                     │   report-chunk-001
                                     │   report-chunk-002
                                     ▼
                              ┌─────────────────┐
                              │  CosmosDB        │
                              │  Vector Container│
                              │  (chunk vectors) │
                              └─────────────────┘
```

---

## 10. Vector Search

The platform provides a built-in vector search playground:

**Features:**
- Select multiple destination indices (checkbox dropdown)
- Enter natural language query
- Query is embedded using the same model as the pipeline
- Parallel vector search across selected indices
- Results merged with configurable strategy (interleave, concatenate, score-based)
- Results display similarity scores, source metadata, and index badges

**Search Flow:**
```
Query Text ──▶ DocGrok /embed ──▶ Query Vector ──▶ CosmosDB Vector Search
                                                    (per selected index)
                                                         │
                                                    Merge Results ──▶ Display
```

---

## 11. Monitoring and Observability

### 11.1 Metrics

- **Per-Pipeline**: documents processed, failed, processing time
- **Per-Day**: daily aggregations of processed/failed counts
- **Global**: total events processed, total processing time
- **Throughput**: docs/sec (overall and last-60-seconds window)

### 11.2 Health Checks

| Component | Health Endpoint | Checks |
|-----------|----------------|--------|
| Web | `/nginx-health` | nginx alive |
| API | `/health` | FastAPI + CosmosDB connectivity |
| DocGrok Router | `/health` | Router alive |
| DocGrok Models | `/health/models` | Per-model health status |
| Models | `/health` | Model loaded, GPU available |

### 11.3 Deployment Visibility

Both OmniVec and DocGrok provide deployment management UIs:
- View pod status, replica counts, resource utilization
- Scale deployments up/down from the UI
- Restart deployments
- View pod logs

---

## 12. Configuration Reference

### 12.1 Environment Variables

| Variable | Component | Description |
|----------|-----------|-------------|
| `DOCGROK_URL` | API, Worker | DocGrok router internal URL |
| `AZURE_COSMOS_ENDPOINT` | All | CosmosDB endpoint |
| `AZURE_CLIENT_ID` | All | Workload identity client ID |
| `DSE_QWEN2_URL` | Router | DSE-Qwen2 model service URL |
| `CLIP_URL` | Router | CLIP model service URL |
| `BGE_URL` | Router | BGE model service URL |
| `DOCGROK_ROUTER_URL` | Pipeline Worker | Router URL for embedding calls |
| `DEFAULT_MODEL_ID` | Pipeline Worker | Default model for embedding step |

### 12.2 Chunk Configuration

```json
{
  "chunk_size": 1000,
  "chunk_overlap": 200,
  "chunk_unit": "chars",
  "store_text": false,
  "text_field": "text",
  "doc_id_pattern": "{source}-chunk-{chunk}"
}
```

**Pattern Variables:**
| Variable | Description | Example |
|----------|-------------|---------|
| `{source}` | Filename without extension | `report` |
| `{source_ref}` | Full source path (/ replaced with -) | `docs-report.pdf` |
| `{source_hash}` | 12-char SHA256 of source_ref | `a1b2c3d4e5f6` |
| `{chunk}` | Zero-padded chunk index | `003` |
| `{pipeline}` | Pipeline ID | `pip-b91b19a5` |
| `{pipeline_hash}` | 8-char SHA256 of pipeline ID | `1a2b3c4d` |

---

## 13. Supported Content Types

| Category | Extensions | Processing |
|----------|-----------|------------|
| **Text** | txt, json, csv, md, html, xml | Direct text extraction |
| **Documents** | pdf, docx, pptx, xlsx | PDF: OCR via PaddleOCR or text extraction |
| **Images** | png, jpg, jpeg, gif, webp | Visual embedding (DSE-Qwen2, CLIP) |
| **Audio** | mp3, wav, m4a | Future: speech-to-text + embedding |

---

## 14. Deployment Profiles

OmniVec supports multiple deployment profiles via Azure Developer CLI:

| Profile | Description | GPU Nodes |
|---------|-------------|-----------|
| `dev-eastus2` | Development | 0–4 |
| `dev2-eastus2` | Development 2 | 0–4 |
| `dev3-eastus2` | Development 3 (current) | 4 |
| `prod-westus2` | Production | 8+ |

Each profile provisions a complete, isolated environment with its own:
- Resource Group
- AKS Cluster
- CosmosDB Account
- ACR
- Storage Account
- Service Bus
- Event Grid

---

## 15. Technology Stack Summary

| Layer | Technology |
|-------|-----------|
| **Frontend** | HTML/CSS/JS, nginx |
| **API** | Python 3.11, FastAPI, Pydantic |
| **Router** | Rust, Axum, Tokio |
| **OCR** | PaddleOCR (PaddlePaddle) |
| **Models** | PyTorch, Transformers, vLLM |
| **Change Feed** | .NET 8, Azure CosmosDB SDK |
| **Database** | Azure CosmosDB NoSQL (with vector indexing) |
| **Storage** | Azure Blob Storage |
| **Messaging** | Azure Service Bus, Event Grid |
| **Container Runtime** | AKS (Kubernetes 1.33) |
| **GPU** | NVIDIA V100 (NC6s_v3) |
| **IaC** | Bicep, Azure Developer CLI (azd) |
| **CI/CD** | ACR cloud builds, Helm |
| **Identity** | Azure Workload Identity, CosmosDB RBAC |
