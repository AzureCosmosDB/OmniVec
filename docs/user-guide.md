# OmniVec User Guide (Web UI)

This guide covers the OmniVec web interface. Access it at `http://<omnivec-url>/ui`.

---

## Navigation

The sidebar provides access to all sections:

| Section | Description |
|---------|-------------|
| **Dashboard** | Pipeline status, job stats, throughput metrics |
| **Sources** | Manage data source connections |
| **Destinations** | Manage vector store targets |
| **Pipelines** | Create and manage processing pipelines |
| **Jobs** | Monitor individual document processing |
| **Vector Search** | Multi-index search playground |
| **DocGrok Health** | Model status and endpoint health |
| **DocGrok Deployments** | Scale models and pipeline workers |
| **OmniVec Health** | Component health and connectivity |
| **OmniVec Deployments** | Scale API, workers, changefeed |

Use the **theme toggle** (sun/moon icon) to switch between light and dark mode.

---

## 1. Sources

A source is a connection to a data store. Sources store **connection info only** — content extraction (what fields to embed, file types to process) is configured on the pipeline.

### Creating a Source

1. Navigate to **Sources** and click **+ New Source**.
2. Enter a **name** and select the **source type** (Azure Blob, CosmosDB, PostgreSQL, MSSQL).
3. Fill in the connection config:
   - **Azure Blob:** `account_url`, `container`, optional `prefix`
   - **CosmosDB:** `endpoint`, `database`, `container`
   - **PostgreSQL:** `host`, `port`, `database`, `table`
   - **MSSQL:** `host`, `port`, `database`, `table`
4. Click **Test Connection** to verify connectivity before saving.
5. Click **Create**.

### Source Detail Page

The detail page shows:
- Connection configuration (read-only after a pipeline references this source)
- List of pipelines using this source
- Connection test results

> **Note:** Sources are **locked** (connection config becomes read-only) once a pipeline references them. Delete the pipeline first to unlock.

---

## 2. Destinations

A destination is where vector embeddings are stored.

### Creating a Destination

1. Navigate to **Destinations** and click **+ New Destination**.
2. Enter a **name** and select the type (CosmosDB Vector, pgvector, MSSQL).
3. Fill in connection config:
   - **CosmosDB Vector:** `endpoint`, `database`, `container`
   - **pgvector:** `host`, `port`, `database`, `table`, `vector_column`, `dimensions`
   - **MSSQL:** `host`, `port`, `database`, `table`
4. Click **Test Connection** — this probes the container and returns the **vector indexing policy** (available embedding paths with dimensions, distance function, and index type).
5. Click **Create**.

### Destination Detail Page

Shows connection config, vector policy details, and pipelines writing to this destination.

> **Note:** Like sources, destinations are **locked** once a pipeline references them.

---

## 3. Pipelines

A pipeline connects one or more sources to a destination through an embedding model.

### Creating a Pipeline

1. Navigate to **Pipelines** and click **+ New Pipeline**.
2. Enter a **name** and optional **description**.
3. **Add sources:**
   - Select a source from the dropdown.
   - Configure **content fields** (which document fields to embed, e.g., `content`, `title`).
   - Set **content mode**: `field` (direct value), `blob_url`, `http_url`.
   - For blob/S3 sources, configure **file type filters** (e.g., `pdf`, `txt`, `docx`).
4. **Select embedding model** — choose a DocGrok pipeline (`text-azure`, `pdf-vision`, etc.).
5. **Select destination** — choose where vectors are written.
6. **Select vector index path** — dropdown populated from the destination's vector policy (e.g., `/embedding`).
7. Configure **processing mode**: `queue` (standard) or `inline` (high-throughput for CosmosDB sources).
8. Configure **content strategy**: `truncate` (one vector per doc) or `chunk` (split into chunks).
9. Toggle **Process Existing** to backfill existing documents on creation.
10. Click **Create**.

### Pipeline Detail Page

- **Status badge:** Active, Paused, Error
- **Stats:** Documents processed, failed, completion percentage
- **Source list** with content field configuration (read-only)
- **Action buttons:** Pause, Resume, Run, Reset, Delete

### Pipeline Lifecycle

```
Created (process_existing=true)  →  ACTIVE  →  Processing...
Created (process_existing=false) →  PAUSED  →  (waiting)

ACTIVE  ──pause──→  PAUSED  ──resume──→  ACTIVE
Any     ──reset──→  Reprocess all documents from the beginning
```

### Important: One pipeline per source + destination + embedding path

If you create two pipelines with the **same source, same destination, and same embedding path**, the second pipeline may not process documents. This is because the changefeed processor uses lease-based ownership — only one pipeline's changefeed can process a given source container at a time.

**If you need multiple embedding models on the same data:**
- Use different **embedding policy paths** in the destination (e.g., `/embedding_small` and `/embedding_large`)
- Each pipeline selects a different embedding path, so they write to different vector fields
- Both pipelines can share the same source and destination containers

**If you need the same model but different content strategies:**
- Create separate destination containers (one per strategy)
- Each pipeline targets a different destination

### Locked settings after creation

Once a pipeline is created, the following settings become **read-only**:
- **Source** and **Destination** — cannot be changed
- **Content Strategy** (truncate/chunk) — changing would invalidate existing vectors
- **Chunk Configuration** (size, overlap, doc ID pattern) — must be consistent with existing chunks
- **Processing Mode** (inline/queue) — tied to changefeed lease setup

---

## 4. Jobs

Jobs are individual document processing units. They are created automatically when a pipeline detects new or changed documents.

### Jobs Page

- **Table view** with columns: ID, Pipeline, Source Ref, Status, Error, Created
- **Filters:** Pipeline dropdown, status dropdown (pending, processing, completed, failed)
- **Actions:** Retry (failed jobs), Cancel (pending jobs)

### Job Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Waiting for a worker |
| `processing` | Worker is actively processing |
| `completed` | Successfully embedded and stored |
| `failed` | Processing error (check error field) |
| `cancelled` | Manually cancelled |

### Automatic Retries

The controller monitors job health every 10 seconds:
- Jobs stuck in `processing` for more than 10 minutes → marked `failed`
- Failed jobs are automatically retried (up to 3 times)

---

## 5. Vector Search Playground

Test your vector indexes with natural language queries.

1. Navigate to **Vector Search**.
2. Select one or more **destination indexes** (checkbox dropdown).
3. Enter a **natural language query**.
4. Click **Search**.

The query is embedded using the same model as the pipeline, then searched against selected indexes. Results show:
- Similarity score (percentage)
- Source metadata
- Content preview
- Index badge (when searching multiple indexes)

---

## 6. DocGrok Health

View the status of all registered embedding models:
- Model name, type, dimensions
- Health status (running, stopped, error)
- GPU utilization
- Endpoint health

---

## 7. Deployments

### OmniVec Deployments

Scale and manage OmniVec components:
- `omnivec-api` — API server
- `omnivec-controller` — Source monitoring, job creation
- `omnivec-worker` — Document processing (scale up for faster throughput)

### DocGrok Deployments

Scale GPU models and pipeline workers:
- Native models (BGE, CLIP, DSE-Qwen2) — scale to 0 saves GPU resources
- Pipeline worker (PaddleOCR) — scale for PDF processing throughput

Each deployment card shows:
- Name, image tag, status badge
- Ready/desired replica count
- Pod table (name, status, restarts, age)
- Action buttons: Scale +/−, Restart, Pause/Resume

The page auto-refreshes every 10 seconds.

---

## 8. Processing Modes

### Blob Storage → New Vector Documents

Source is Azure Blob Storage. Pipeline creates new documents in the destination for each processed blob.

### CosmosDB → Patch-in-Place (Inline)

Source and destination are the **same** CosmosDB container. The embedding is patched directly into the source document — no separate vector document created.

### CosmosDB → Separate Destination (Queue)

Source is CosmosDB, destination is a **different** container. New vector documents are upserted to the destination.

### Content Change Detection

After initial embedding, OmniVec tracks content changes using SHA256:
- New document → embed and store hash
- Content changed (hash mismatch) → re-embed
- Content unchanged (hash match) → skip
- Non-content field changed → skip

---

## 9. Authentication

OmniVec uses **Azure Managed Identity** (DefaultAzureCredential) for all Azure service connections. No keys or connection strings needed.

**Required RBAC for CosmosDB sources/destinations:**
1. `Cosmos DB Built-in Data Contributor` (SQL RBAC) — data operations
2. `Cosmos DB Account Reader Role` (ARM RBAC) — SDK initialization

**Required RBAC for Blob Storage sources:**
- `Storage Blob Data Reader` (reading blobs)

---

## 10. Common Tasks

### Embed CosmosDB documents in-place

1. Create a **source** pointing to your CosmosDB container
2. Create a **destination** pointing to the **same** container (must have vector embedding policy)
3. Create a **pipeline** with `process_existing: true` and `content_fields` set to your text field
4. The changefeed processor detects documents and embeds them in-place

### Embed blob storage documents

1. Create a **source** pointing to your blob container
2. Create a **destination** (CosmosDB with vector index)
3. Create a **pipeline** with `file_types` set to your file extensions
4. Existing blobs are enumerated and processed; new uploads are detected via Event Grid

### Scale for faster processing

Navigate to **OmniVec Deployments** → `omnivec-worker` → increase replicas.

---

## 11. API Quick Reference

All UI operations are also available via the REST API:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST` | `/api/sources` | List / Create sources |
| `POST` | `/api/sources/{id}/test` | Test connection |
| `GET/POST` | `/api/destinations` | List / Create destinations |
| `GET/POST` | `/api/pipelines` | List / Create pipelines |
| `POST` | `/api/pipelines/{id}/pause\|resume\|run\|reset` | Lifecycle |
| `GET` | `/api/jobs` | List jobs (`?pipeline_id=`, `?status=`) |
| `POST` | `/api/jobs/{id}/retry\|cancel` | Job management |
| `GET/POST` | `/api/models` | List / Register models |
| `POST` | `/api/search` | Vector similarity search |
| `GET` | `/api/deployments` | K8s deployment management |
| `GET` | `/health` | System health |
