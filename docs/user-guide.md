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

### Synthetic payload benchmarks (opt-in)

Enable `api.mockBenchmark.enabled` in the Helm chart to expose the console's
**Mock** source, **Mock** sink, and **Mock embedding** model. The chart deploys
a dedicated single-replica sink so its acceptance budget is shared by all producers.
The destination uses its own HTTP server rather than the control-plane API;
authentication, payload bounds, and the configured acceptance rate apply.
This feature is disabled by default and intended for test environments.
The chart deploys separate .NET `omnivec-mock-source` and
`omnivec-mock-destination` images and a Rust `docgrok-mock-embedding` model.
The API handles component registration, configuration and run receipt reads
only; it does not generate documents or receive embedding batches.
Set `api.adminToken` to authenticate component HTTP requests.
The Rust DocGrok router forwards requests to that service; mock vector
generation does not run in the router. The model receives registered dimensions
and latency with each request and does not read metadata storage.
Legacy unregistered `mock-embedding`/`mock-1536` router pipeline aliases are
retired with an explicit error; use a registered model service instead.

In the console, create a Mock source with document count, exact document size in
bytes, generation rate, and batch size. Register a Mock embedding model with its
embedding dimension and optional simulated latency. Create a Mock sink with the
same dimension, acceptance rate, and burst capacity, then connect them in a normal
registered pipeline using inline processing and whole-document content.

Source and sink rates of `0` mean unlimited. A rate-limited sink rejects excess
batches with HTTP 429 and Retry-After; the runner retries for at most 30 minutes.
The source batch size must fit the sink's burst capacity when limiting is enabled.
Each binary vector batch is limited to 4 MiB of FP32 values.

The source's **Embedding response transport** defaults to `json`, retaining the
normal DocGrok response path. Opt into `fp32` to receive the same full vectors in
binary directly from the registered mock model. Both modes transmit every value
over HTTP and validate finite FP32 values and dimensions at the runner and sink.
The Rust router also supports FP32 responses for registered OpenAI-compatible
embedding models. Select a real model such as Harrier to benchmark inference
with synthetic input and a discard-only sink; transform recipes are not supported.
Run receipts set `real_embeddings` to distinguish real inference from synthetic
model output. Neither path persists vectors in the mock destination.

Vectors travel over HTTP from the model through the Rust router as JSON or
FP32, then to the sink as full
little-endian FP32 payloads. The sink validates dimensions, every value, and a
SHA256 receipt before discarding the vectors. There is no GPU inference or
searchable vector persistence. Run receipts and checkpoints are persisted in
metadata storage; sink replay receipts are in memory and can expire or be lost on
restart, so this does not provide exactly-once persistent vector writes.

The pipeline overview shows accepted documents, measured documents/second, and
an authenticated full run receipt. `payload_bytes` counts source-to-sink binary
bodies, including a 16-byte header per batch; it excludes model/router traffic
and HTTP overhead. Measurement excludes initial metadata setup but includes
generation, embedding HTTP calls, payload validation, throttling, and checkpoint
writes. Pause/resume retains progress; reset starts a new generation.
The receipt also includes batch count, stage mean/max milliseconds, cumulative
stage seconds and component implementation names. Model and sink HTTP timings include
service time and network wait; sink validation is a subset of sink HTTP time.
Cumulative stage time overlaps across concurrent batches and should not be
added to elapsed time.
The synthetic sink coalesces metadata reads with a configurable cache interval:
`api.mockBenchmark.configRefreshSeconds` in Helm or
`OMNIVEC_MOCK_CONFIG_REFRESH_SECONDS` in the API environment (default 300 seconds,
finite and at least 1). Each destination and replica expires independently at a
random 80-100% of that interval, avoiding synchronized refreshes. Disabling a
destination is observed after at most the configured interval plus metadata read time;
failed refreshes return an error rather than accepting with stale configuration.
Mock source/sink configuration and model dimensions/latency are locked while a
pipeline references them. Create new components or remove referencing pipelines
before changing those settings.

For aggregate scale-out benchmarks, `api.mockBenchmark.shardCount` (1-32,
default 1) deploys separate single-replica receivers and additional runners.
Select a source's **Runner shard** and destination's **Receiver shard** in the
advanced portal fields. Each destination belongs to exactly one receiver, so
its acceptance budget and receipt cache are not multiplied by replica count.
Each pipeline still has one leased runner; scale-out uses multiple registered
pipelines. Report aggregate throughput over the earliest start to latest finish,
not the sum of independently measured rates. It is distinct from single-pipeline
throughput. Multi-node timestamps assume synchronized cluster clocks.

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
- **Chunk document ID pattern and text-storage fields** — cannot be changed in place.
- **Processing Mode** (inline/queue) — tied to changefeed lease setup

For Cosmos text chunking, **size, overlap, unit and replacement order** can be edited for newly queued work. In-flight messages retain their original configuration.

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

The queue writer directly upserts both new and existing vector documents; it does not attempt a patch first. An upsert replaces the destination document with the fields emitted by the pipeline, including configured source fields and metadata. Unrelated destination-only fields are not retained. Inline mode still patches the existing source document.

Upserts are grouped by partition in transactions of at most 100 operations and a conservative 1.5 MB serialized payload budget. Larger individual documents use a single-item upsert instead of a transaction; the Cosmos 2 MiB item limit still applies. Payloads are planned before writes or delete-first cleanup begins.

For Cosmos field-content chunking, choose `chunk_config.cleanup_order` in the pipeline settings:

| Value | Behavior on replacement |
|---|---|
| `insert-first` (default) | Persist every new chunk before deleting obsolete chunks. Failed writes skip cleanup; partial writes can temporarily leave mixed old/new chunks. |
| `delete-first` | Delete all previous chunks for that source document/pipeline/partition, then upsert the new chunks. Failed cleanup skips insertion; failed insertion can leave a search gap. |

Neither mode is an atomic replacement across batches or partitions. Errors propagate for retry, and messages are completed only after both stages succeed. Ordinary unchunked records use direct upsert without a cleanup query. A chunked document that shrinks to one or zero chunks still requires cleanup. Chunk lookup remains query-based, including `/id` destinations; manifests and distributed fencing are not implemented by this setting. Concurrent revisions are not given atomic ordering guarantees.

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
| `GET` | `/ready` | Metadata storage readiness (503 when unavailable) |
# Garnet HASH sources (inline)

A native `garnet` source polls a named HASH containing JSON documents. Configure
`endpoint`, `hash_key`, TLS/authentication, `poll_interval_seconds`, `scan_page_size`
and `batch_size` (at most 50). Each HASH field is a stable source reference. Each
value must contain `id`, a positive Int64 `version`, and string `content`.
Increase the version for every change, including `{"id":"doc","version":2,
"content":"","deleted":true}` tombstones. Hard `HDEL` is not a deletion feed.

This source supports a single-source, `inline`, `truncate` pipeline to a separate
Garnet vector set on the same endpoint. It does not use Service Bus. Real model
calls and the normal version-fenced destination writer are used; source HASH
values are never overwritten. Source checkpoint hashes use the reserved
`__omnivec:source-checkpoints:` prefix and advance only after successful writes.
Unchanged records are skipped; changing a record without increasing its version
is logged as an error. Pipeline edits and resets advance the processing revision.
Empty content removes the corresponding vector, as does an explicit tombstone.

Polling provides eventual convergence, not a transactional change feed or a
point-in-time snapshot. One polling lease owner handles each source, with bounded
batch concurrency from `resource_policy.max_concurrency_per_worker` (capped at
64). Logs separate read, model, writer/checkpoint, and metrics callback time.
Memory-only Garnet deployment is not durable storage.

## Large inline Cosmos documents

Inline Cosmos processing retains the source document and writes one embedding
back to it. In truncate mode, the model may embed only its token-limit portion;
use chunk processing to embed all text. Inline model requests are bounded to
50 texts and 2 MiB of serialized JSON, including JSON escaping. Large documents
are split into smaller request batches without truncating their stored text.
A single text that exceeds the request limit fails explicitly and requires
chunk processing. Model output and required writes must complete before the
source change-feed checkpoint advances.
