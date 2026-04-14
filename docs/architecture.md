# OmniVec Architecture

## 1. Overview

OmniVec is a **Universal Vector Ingestion Platform** deployed on Azure Kubernetes Service (AKS). It watches data sources, extracts content, generates vector embeddings via DocGrok, and writes vectors to destination stores.

**DocGrok** is the embedded AI model orchestration layer — a unified API for routing embedding requests to self-hosted GPU models (DSE-Qwen2, CLIP, BGE) or external providers (Azure OpenAI), with multi-step transform pipelines (PDF → OCR → text → embeddings).

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

## 2. OmniVec Components

### 2.1 Web UI (omnivec-web)

| Attribute | Detail |
|-----------|--------|
| Technology | Static HTML/JS + nginx reverse proxy |
| Image | `omnivec-web:latest` |
| Replicas | 2 |
| Service | LoadBalancer (external IP) |

**Routing (nginx):**
- `/api/*` → `omnivec-api:80` (control plane)
- `/docgrok/*` → `docgrok:80` (DocGrok router)
- `/*` → Static files (SPA)

**Features:** Dashboard, source/destination/pipeline management, vector search playground, DocGrok health, deployment scaling, light/dark theme toggle.

### 2.2 OmniVec API (omnivec-api) — Control Plane

| Attribute | Detail |
|-----------|--------|
| Technology | Python FastAPI |
| Image | `omnivec-api:latest` |
| Replicas | 2 |
| Port | 8080 |

**Key API Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST/PUT/DELETE` | `/api/sources` | CRUD for data sources |
| `POST` | `/api/sources/{id}/test` | Test source connection |
| `GET/POST/PUT/DELETE` | `/api/destinations` | CRUD for vector destinations |
| `POST` | `/api/destinations/{id}/test` | Test + probe vector indexes |
| `GET/POST/PUT/DELETE` | `/api/pipelines` | CRUD for pipelines |
| `POST` | `/api/pipelines/{id}/pause\|resume\|run\|reset` | Pipeline lifecycle |
| `GET` | `/api/jobs` | List/filter jobs |
| `POST` | `/api/jobs/{id}/retry\|cancel` | Job management |
| `GET/POST` | `/api/models` | Model registration |
| `POST` | `/api/search` | Multi-index vector search |
| `GET` | `/api/deployments` | K8s deployment management |
| `GET` | `/health` | Health check |

**Data Storage:** All metadata (sources, destinations, pipelines, jobs, metrics) in CosmosDB container `metadata` with partition key `/doc_type`.

### 2.3 OmniVec Controller (Bookkeeper)

| Attribute | Detail |
|-----------|--------|
| Replicas | 1 (singleton) |

**Responsibilities:**
- Source enumeration — iterates source documents and creates pending jobs
- CosmosDB change feed monitoring — detects new/updated documents
- Blob storage enumeration — lists blobs matching file type filters
- Job lifecycle management — marks stale processing jobs as failed
- Metrics aggregation — rolls up job stats

### 2.4 OmniVec Workers (Job Processors)

| Attribute | Detail |
|-----------|--------|
| Replicas | 1–10 (HPA autoscaled at 70% CPU) |

**Processing flow:**
1. Poll pending jobs from CosmosDB
2. Download content from source (field value, blob, HTTP URL)
3. Determine content type
4. Apply content strategy: `truncate` (single vector) or `chunk` (split → multiple vectors)
5. Send to DocGrok `/embed/batch`
6. Write vector(s) to destination
7. Update job status

### 2.5 Change Feed Processor (CFP)

| Attribute | Detail |
|-----------|--------|
| Technology | .NET (Azure CosmosDB SDK) |
| Image | `omnivec-changefeed:latest` |
| Replicas | 15 (fixed, matches CosmosDB physical partitions) |

Real-time Change Data Capture from CosmosDB source containers.

**Component hierarchy:**
```
SourceDiscoveryService (BackgroundService)
    │  Polls API every 30s for active pipelines + CosmosDB sources
    ▼
SourceWatcherManager
    │  Reconciles running watchers against desired state
    │  Handles generation-based resets (stop old, start new)
    ▼
SourceWatcher [per source]
    │  Wraps one ChangeFeedProcessor instance
    │  Handles onChanges delegate (inline or queue mode)
    ▼
LeaseContainerManager
    │  Creates/manages lease containers: leases-{source_id}
    ▼
ChangeFeedProcessor (Azure SDK)
```

**Inline Mode (high-throughput):** The CFP processes documents end-to-end without the job queue:
- Extract content, compute content_hash, filter eligible docs
- Sub-batch (100 native / 50 external) to DocGrok `/embed/batch`
- `PatchByPartitionBatch` back to source container

**Queue Mode:** CFP creates pending jobs in metadata store → Workers pick up and process.

### 2.6 Blob Watcher (optional)

Monitors Azure Blob Storage via Event Grid → Service Bus → Queue polling pattern. Creates pending jobs for matching pipelines.

---

## 3. Data Model

All control plane state lives in CosmosDB container `metadata` with partition key `/doc_type`.

| Entity | ID Prefix | doc_type | Purpose |
|--------|-----------|----------|---------|
| Source | `src-` | `source` | Connection info to a data store |
| Destination | `dst-` | `destination` | Where to write vectors |
| Pipeline | `pip-` | `pipeline` | Binds source(s) to model + destination |
| Job | `job-` | `job` | Single document processing unit |

### 3.1 Source

Stores connection info only. No content extraction config.

```python
class Source:
    id, name, type, config, triggers, schedule, enabled, created_at, updated_at
```

### 3.2 Destination

Connection to a vector store. When tested, returns `vector_indexes` from the container's vector indexing policy.

```python
class Destination:
    id, name, type, config, enabled, created_at, updated_at
```

### 3.3 PipelineSource

Content extraction config — how a pipeline reads content from a specific source.

```python
class PipelineSource:
    source_id: str
    filters: Dict = {}
    content_fields: List[str] = ["content"]
    content_mode: str = "field"        # "field", "blob_url", "http_url", "s3_url"
    url_content_types: List[str] = ["txt", "json", "pdf"]
    content_type_field: Optional[str] = None
    file_types: List[str] = ["txt", "json", "pdf", "docx", "md", "csv"]
```

### 3.4 Pipeline

```python
class Pipeline:
    id, name, description
    sources: List[PipelineSource]
    docgrok_pipeline: str              # Model ID or transform pipeline name
    destination_id: str
    vector_index_path: str             # From destination's vector policy (e.g., "/embedding")
    status: "active" | "paused" | "error"
    process_existing: bool = True
    metadata_mapping: Dict = {}
    processing_mode: str = "queue"     # "queue" or "inline"
    content_strategy: str = "truncate" # "truncate" or "chunk"
    chunk_config: Optional[ChunkConfig]
    doc_id_pattern: str = "{source}"
```

### 3.5 ChunkConfig

```python
class ChunkConfig:
    chunk_size: int = 1000
    chunk_overlap: int = 200
    chunk_unit: str = "chars"          # "chars" or "tokens"
    store_text: bool = False
    text_field: str = "text"
    doc_id_pattern: str = "{source}-chunk-{chunk}"
```

**Pattern variables:** `{source}` (filename), `{source_ref}` (full path), `{source_hash}` (12-char SHA256), `{chunk}` (zero-padded index), `{pipeline}` (pipeline ID).

---

## 4. DocGrok — AI Model Orchestration

### 4.1 DocGrok Router

| Attribute | Detail |
|-----------|--------|
| Technology | Rust (Axum web framework) |
| Service | ClusterIP port 80 |

**Key Endpoints:** `/embed`, `/embed/batch`, `/models`, `/health`, `/pipelines`

**Model discovery:**
- **Native models:** Discovered via Kubernetes API — scans deployments with label `role=model`
- **External models:** Configured in Helm values under `providers`

**Routing logic:**
- `mdl-ext-*` → call external API (Azure OpenAI)
- `mdl-*` → forward to native model service
- Pipeline name → resolve pipeline config → route to pipeline worker

### 4.2 Model Types

| Model | Type | Dimensions | GPU | Use Case |
|-------|------|-----------|-----|----------|
| DSE-Qwen2 | Visual Document | 1536 | V100 16Gi | PDF page images |
| CLIP ViT-L/14 | Image | 768 | V100 4Gi | Image embeddings |
| BGE-Large-EN | Text | 1024 | V100 4Gi | English text |
| BGE-Small-EN | Text | 384 | V100 2Gi | Lightweight text |
| text-embedding-3-small | Text (External) | 1536 | — | Azure OpenAI |
| text-embedding-3-large | Text (External) | 3072 | — | Azure OpenAI |

### 4.3 Transform Pipelines

Multi-step processing workflows:

```
PDF Binary → Pipeline Worker → PaddleOCR → Text → Embedding Model → Vectors
```

Available pipelines: `text-azure`, `text-bge`, `pdf-text-azure`, `pdf-vision`, `pdf-ocr-text`, `image-clip`.

### 4.4 DocGrok Controller

Health checks on all models (every 60s), persists scale state to CosmosDB, restores replica counts on startup.

### 4.5 Pipeline Worker

Python FastAPI + PaddleOCR. Memory-intensive (8–12 Gi) due to OCR model loading.

---

## 5. Infrastructure

### 5.1 Provisioning

Infrastructure is provisioned via `azd up` with Bicep templates (`infra/main.bicep`).

### 5.2 Resource Map

| Resource | Service | Purpose |
|----------|---------|---------|
| AKS Cluster | Azure Kubernetes Service | Container orchestration |
| System Node Pool | Configurable VM SKU (2+ nodes) | OmniVec + DocGrok workloads |
| GPU Node Pool | NC-series VMs (0+ nodes) | Self-hosted embedding models |
| ACR | Azure Container Registry | Docker image storage |
| CosmosDB | Azure Cosmos DB (NoSQL) | Metadata store |
| Storage Account | Azure Blob Storage | Document source (optional) |
| Service Bus | Azure Service Bus | Job queue for blob events (optional) |
| Event Grid | Azure Event Grid | Blob change notifications (optional) |
| Key Vault | Azure Key Vault | Model API key storage |
| Managed Identity | User-Assigned MI | Workload Identity federation |

### 5.3 Identity and Security

- **Workload Identity Federation:** AKS pods authenticate via federated tokens — no secrets in pods
- **CosmosDB RBAC:** Native SQL RBAC (no master keys)
  - `Cosmos DB Built-in Data Contributor` (role `00000000-0000-0000-0000-000000000002`)
  - `Cosmos DB Account Reader Role` (ARM RBAC — required for SDK `readMetadata`)
- **ACR Pull:** AKS kubelet identity has `AcrPull` role

### 5.4 Kubernetes Namespace Layout

```
Namespace: omnivec
├── omnivec-web              (2 replicas, LoadBalancer)
├── omnivec-api              (2 replicas, ClusterIP)
├── omnivec-controller       (1 replica)
├── omnivec-worker           (1–10 replicas, HPA)
├── omnivec-changefeed       (15 replicas)
├── omnivec-blob-watcher     (1 replica, optional)
├── omnivec-dotnet-worker    (optional)
├── docgrok                  (1 replica, ClusterIP — router)
├── docgrok-controller       (1 replica)
├── docgrok-pipeline-worker  (1 replica, optional)
├── dse-qwen2               (0–N replicas, GPU)
├── clip                    (0–N replicas, GPU)
├── bge                     (0–N replicas, GPU)
└── bge-small               (0–N replicas, GPU)
```

### 5.5 Helm Charts

- **`helm/omnivec/`** — Main chart: deploys all OmniVec components, includes DocGrok as subchart
- **`helm/docgrok/`** — Standalone DocGrok chart (can be deployed independently)

---

## 6. Data Flows

### 6.1 CosmosDB Source → Inline Embedding

```
CosmosDB Source Container
    │ Change Feed (batches of 500)
    ▼
Change Feed Processor
    │ Extract content, compute content_hash
    │ Send to DocGrok /embed/batch
    │ PatchByPartitionBatch back to source
    ▼
Source Container (doc + /embedding, /embedded_at, /content_hash)
```

### 6.2 CosmosDB Source → Queue Mode

```
CosmosDB Source → Change Feed Processor → Create Job (metadata)
                                               │
Worker picks up → Download content → DocGrok → Write to Destination
```

### 6.3 Blob Source → Event-Driven

```
Blob uploaded → Event Grid → Service Bus → Worker → Job → Process
```

### 6.4 Blob Source → Chunked PDF

```
Azure Blob (PDF) → Worker → DocGrok Pipeline Worker (PaddleOCR)
    → Extracted text → Split into chunks → DocGrok /embed/batch
    → Write chunk documents (report-chunk-000, -001, ...)
    → Destination Vector Container
```

---

## 7. Scaling

| Component | Strategy | Trigger |
|-----------|----------|---------|
| API | Manual (2 replicas default) | API request load |
| Workers | HPA (1–10 replicas) | CPU > 70% |
| Changefeed | Fixed (15 replicas) | Matches CosmosDB partitions |
| GPU Models | Manual via UI/API | On-demand; scale to 0 saves GPU |

---

## 8. Monitoring

### Health Endpoints

| Component | Endpoint | Checks |
|-----------|----------|--------|
| Web | `/nginx-health` | nginx alive |
| API | `/health` | FastAPI + CosmosDB connectivity |
| DocGrok Router | `/health` | Router alive |
| DocGrok Models | `/health/models` | Per-model health status |

### Metrics

`GET /api/metrics` returns per-pipeline and daily aggregations of processed/failed counts, processing times, and throughput.

---

## 9. Deployment Hooks

### preprovision

1. Validates prerequisites (az, kubectl, helm — installs kubectl/helm if missing)
2. Checks Azure login
3. If RG exists → imports config from RG tags → skips prompts
4. If config already set via `azd env set` → skips prompts
5. Fresh deploy → interactive prompts for metadata store, blob source, node pools
6. Validates VM SKU availability in the target region
7. Stores all choices in azd env

### postprovision

1. Tries anonymous image import from shared registry
2. Falls back to token-based import if auth required
3. If no token → prompts → fallback to ACR source build
4. Gets AKS credentials
5. Creates namespaces and K8s secrets
6. Deploys via Helm (`helm upgrade --install`)
7. Auto rollout restart if images changed
8. Saves config as RG tags (enables cross-machine sync)
9. Prints deployment URL and admin token
