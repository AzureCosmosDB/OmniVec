# OmniVec user guide

[README](../README.md) | [CLI guide](cli-guide.md) | [Search API](search-api.md)

This guide covers the OmniVec web interface and the source/destination
configuration shared with its REST API. Access the console at
`<your-omnivec-origin>/ui`. Use HTTPS for credentials on untrusted networks.
The desktop console groups sources and vector stores
under **Connections**; the classic console exposes separate **Sources** and
**Destinations** pages. Labels below use the classic names where they differ.

For deployment and a complete small example, start with the
[README quick start](../README.md#quick-start).

## First-pipeline checklist

1. Register and test an embedding model.
2. Register a source; confirm permissions and that its watcher is deployed.
3. Register a destination with matching vector dimensions and the required index.
4. Create a compatible pipeline and choose the content to embed.
5. **Run or Resume it:** creation always leaves it paused.
6. Verify destination writes and retrieval, not just health or job counters.

The key distinction is **source -> pipeline -> destination**. Sources describe
where data comes from; pipelines select content, model, and processing policy;
destinations describe where results go.

## Navigation

Use these areas of the console. Exact labels and available actions depend on
the console version, enabled capabilities, and your role.

| Area | Purpose |
|------|---------|
| **Connections** / **Sources**, **Destinations** | Register and test data connections |
| **Pipelines** | Configure, activate, monitor, and pause ingestion |
| **Models** / **DocGrok Health** | Register models and inspect model connectivity |
| **Vector Search** | Query supported destinations |
| **Metrics**, health, and deployment views | Inspect throughput, errors, dependencies, and workload readiness |
| **Diagnostics** / **Recovery Agent** | Read-only evidence and approval-gated repairs |
| **Jobs** (classic) | Inspect metadata job records, not a complete Service Bus delivery ledger |
| **MCP servers** / **Foundry agents** | Prepare and approve supported cloud deployment plans |

Use the **theme toggle** (sun/moon icon) to switch between light and dark mode.

Resource labels keep as much of the name's prefix as fits, an ellipsis in the
middle, and the last five characters when space is limited. Hover over a label
to see its full name. This only changes the display; saved names and IDs are
unchanged.

In the classic console, filter the Pipelines list by name or ID to focus on a
demo or investigation without deleting other pipelines. Live progress follows
confirmed backend document counts; it does not extrapolate unprocessed
documents. A percentage is unavailable when the source total is unknown.

---

## 1. Sources

A source registers a connection and its scan settings. Configure pipeline
content fields and extraction strategy separately; SharePoint file filters and
OneLake scan content fields are also part of the source configuration.

### Supported sources

| Source | API type | Connection and scan settings | Prerequisites |
|--------|----------|------------------------------|---------------|
| Azure Blob Storage | `azure-blob` | `account_url`, `container`, optional `prefix` | Blob ingestor and read access; initial enumeration/live polling, with optional Event Grid delivery |
| Cosmos DB for NoSQL | `cosmosdb` | `endpoint`, `database`, `container` | Cosmos data access and changefeed processor; initial backfill followed by change feed |
| PostgreSQL | `postgresql` | `host`, `port` (5432), `database`, `table`, `user`, `password`; optional `schema_name` and `primary_key` | Database connectivity/read access; polls recognized tracking columns (`updated_at`, `modified_at`, `last_modified`, `_ts`), otherwise scans the table |
| SQL Server / Azure SQL | `mssql` | `host`, `port` (1433), `database`, `table`; optional `schema_name`, `primary_key`, `user`, `password` | CDC-enabled database/table and changefeed processor; the watcher attempts to enable table CDC if needed and requires permission |
| Databricks Delta Lake | `databricks` | `workspace_url`, `http_path`, `catalog`, `schema`, `table`, optional `content_column`/`id_column` | Delta CDF enabled on the table, SQL Warehouse access, and dedicated Databricks watcher |
| SharePoint Online | `sharepoint` | `site_id`, `drive_id`, optional `folder_path`, `file_types`, `poll_interval_seconds`, `max_file_size_bytes` | Microsoft Graph application access, dedicated SharePoint watcher, Service Bus, and .NET worker |
| Fabric OneLake Iceberg | `onelake-iceberg` | `warehouse` (`<workspace-id>/<lakehouse-id>`), `namespace`, `table`, `content_fields`, `id_field` | OneLake catalog/data read and checkpoint write access, dedicated PyIceberg watcher, Service Bus, and .NET worker |
| Synthetic (benchmark only) | `synthetic` | `benchmark_only: true`, `document_count`, `message_size_bytes`, `docs_per_second`, optional mock embedding | Explicit generation request, active queue pipeline, API Service Bus publishing configuration, and worker; see [benchmarks](#synthetic-source-and-null-destination-benchmarks) |

S3 (`s3`) and standalone HTTP (`http`) sources are declared API types but are
**not implemented**; creation is rejected. A content field containing a URL is
not the same as a standalone HTTP source.

### Creating a Source

1. Open **Connections > Sources > Add source** (classic: **Sources > + New Source**).
2. Enter a **name** and select an available source type.
3. Fill in the connection and scan settings from the table above.
4. Check the permissions guidance and run **Test Connection** where offered.
5. Click **Create**.

The desktop picker currently provides Cosmos DB, Azure Blob, PostgreSQL,
SharePoint, and OneLake guided forms. Available options also depend on
`GET /api/capabilities`. Use `POST /api/sources` with `name`, `type`, and
`config` for implemented types not exposed by the picker, such as Databricks,
SQL Server, and synthetic sources. The synthetic form is not currently
advertised by the capabilities response.

### SharePoint and OneLake setup

For SharePoint, grant Graph `Sites.Selected` with a site-specific read grant
(recommended), or `Files.Read.All`, with admin consent. Enable
`OMNIVEC_SHAREPOINT_ENABLED=true` for `azd`, or `sharepointWatcher.enabled`
in Helm. The default and maximum file size is 50 MiB; size worker memory and
concurrency for in-memory document processing. SharePoint requires a Cosmos DB
vector destination with a dedicated document partition key such as
`/document_id`, **not `/id`**. See the
[SharePoint guide](sharepoint-source.md) for identity, synchronization, and
upgrade requirements.

For OneLake, enable `OMNIVEC_ONELAKE_ICEBERG_ENABLED=true` for `azd`, or
`onelakeIcebergWatcher.enabled` in Helm, after granting catalog/data read,
checkpoint write, and Service Bus permissions. The watcher performs Iceberg
scans; optional low-latency Delta CDF requires a separately configured,
long-running Fabric Spark job. Turning on the watcher alone does not deploy
that stream. See the [OneLake guide](onelake-iceberg.md).

SharePoint and OneLake can use a pipeline-scoped cross-tenant identity:
`sharepoint_identity` or `onelake_identity` on that pipeline's source entry.
These require federated application setup and service-specific grants; do not
put application secrets in source configuration.

### Databricks setup

Enable Delta Change Data Feed on the source table and grant access to the SQL
Warehouse and table. Enable `databricksWatcher.enabled` in the deployment's Helm
values. The dedicated watcher polls Delta CDF through the SQL Warehouse,
including insert/update events and delete tombstones. Authentication can use
the workload identity (`auth_type: "managed-identity"`) or a PAT resolved from
`pat_secret_ref` in Key Vault. Register the source through the REST API if the
console does not offer a Databricks form.

### Source Detail Page

The detail page shows:
- Connection configuration (read-only after a pipeline references this source)
- List of pipelines using this source
- Connection test results

> **Note:** Sources are **locked** (connection config becomes read-only) once a pipeline references them. Delete the pipeline first to unlock.

---

## 2. Destinations

A destination is where vector embeddings are stored.

### Supported destinations

| Destination | API type | Main configuration | OmniVec search |
|-------------|----------|--------------------|----------------|
| Cosmos DB Vector | `cosmosdb-vector` | `endpoint`, `database`, `container`; existing vector embedding policy and vector index | Vector; full-text/hybrid when the relevant Cosmos policies are configured |
| PostgreSQL + pgvector | `pgvector` | `host`, `port`, `database`, `table`, `user`, `password`, `vector_column`, `vector_dimensions`, `index_type` (`hnsw` or `ivfflat`) | Vector |
| SQL Server / Azure SQL | `mssql` | `host`/`server`, `database`, `table`, authentication, optional `id_column`, `vector_column`, `content_column` | Not supported by OmniVec Search; the worker writes serialized embedding arrays and metadata |
| Garnet Vector Sets (preview) | `garnet` | `endpoint`, `vector_set`, `distance_metric`, `quantization`, TLS/authentication | Vector |
| OneLake same-row write-back | `onelake-iceberg` | `workspace_id`, `lakehouse_item_id`, `spark_job_definition_item_id`, `staging_file_system`, `target_table`, optional serving `mirror` | Through a Garnet mirror only; not directly against the lakehouse |
| Null sink (benchmark only) | `null` | `benchmark_only: true`, optional `max_docs_per_second` and `rate_key` | None; embeddings are discarded |

### Creating a Destination

1. Open **Connections > Vector stores > Add vector store** (classic: **Destinations > + New Destination**).
2. Enter a **name** and select an available destination type.
3. Fill in the configuration from the table above. Match vector dimensions to the embedding model.
4. Click **Test Connection** where offered. Results are connector-specific: Cosmos returns vector policies; pgvector discovers vector columns/indexes; Garnet checks connectivity and Vector Set commands.
5. Click **Create**.

The desktop wizard offers Cosmos DB Vector, pgvector, Garnet, and OneLake.
Use `POST /api/destinations` with `name`, `type`, and `config` for SQL Server
or the benchmark-only null sink when they are absent from the UI.

### Garnet Vector Sets

Use a Garnet server started with `--enable-vector-set-preview`; a generic
Redis-compatible endpoint is not sufficient. Azure Managed Redis uses Redis
Enterprise, not Garnet, and is not a substitute for this connector.

Set `endpoint` to the reachable host and port and choose a `vector_set`
(default `omnivec-vectors`). Supported distance metrics are `COSINE`, `L2`,
`IP`, and `XCOSINE_NORMALIZED`; quantization options are `NOQUANT`, `Q8`,
and `BIN`. TLS defaults to enabled. For ACL authentication, supply `username`
and `password_secret_ref: "kv://<vault>/<secret>"`, and grant the workload
identity access to that secret. Enable `use_entra_auth` only if the endpoint
explicitly implements the Azure Redis Entra token flow; it defaults to false.
OmniVec uses RESP2 for the preview Vector Set commands.

Garnet can be a standalone destination or a OneLake serving mirror. Graph
parameters `m`/`ef` and query parameters `search_ef`/`filter_ef` tune index/search
behavior; these do not provide a throughput guarantee.

### OneLake write-back and serving mirrors

OneLake destinations are **not general-purpose append targets**: they update
existing rows from a OneLake source in the **same lakehouse and source table**.
Use `processing_mode: "queue"` and `content_strategy: "truncate"`.
The worker durably stages embeddings and submits a Fabric Spark Job Definition
to MERGE the vectors and managed metadata into those rows. It never inserts
missing source rows or writes Iceberg metadata directly.

Create the required write-back columns, upload the provided Spark merge job,
and grant staging write and Fabric job execution permissions as described in
the [OneLake guide](onelake-iceberg.md). Tune `merge_batch_size` (default 5000),
`merge_flush_interval_seconds` (30), and `max_concurrent_merges` (1) to control
merge submission. Queue processing and lakehouse merge completion are separate
stages; accepting an embedding into staging is not proof of a completed MERGE.

An optional `mirror` with `type: "garnet"` serves vectors for low-latency search.
The desktop mirror shortcut assumes TLS and Entra authentication; use the API
configuration shown in the OneLake guide for self-hosted Garnet ACL settings.
Mirror errors fail processing unless `mirror.best_effort: true` is explicitly
chosen. The legacy `redis` string mirror is not vector-searchable.

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
   - For file sources, configure the applicable **file type filters** (e.g., `pdf`, `txt`, `docx`); SharePoint also has source-level file filters.
4. **Select embedding model** — choose a registered model or an existing DocGrok transform. A transform name is not automatically tied to the last model you registered.
5. **Select destination** — choose where vectors are written.
6. **Select vector index path** — dropdown populated from the destination's vector policy (e.g., `/embedding`).
7. Configure **processing mode**: `queue` for a separate destination; `inline` only for a supported source and destination identifying the same physical container/table.
8. Configure **content strategy**: `truncate` (one vector per doc) or `chunk` (split into chunks).
9. Enable **Process Existing** if the initial scan should include existing documents.
10. Click **Create**, inspect the saved configuration, then **Run** or **Resume**.

Model output dimensions must match the selected vector path/column. `truncate`
can shorten oversized input; it does not preserve all text from an arbitrarily
long document. For a separate Cosmos destination, choose `store_content: true`
if you need persisted text previews. Chunk pipelines use `chunk_config.store_text`
instead. Content storage can copy sensitive source text into another system, so
make that choice deliberately.

### Source and destination compatibility

| Pipeline | Required mode and constraints |
|----------|-------------------------------|
| Cosmos DB in-place | `inline`; source and destination must identify the same physical Cosmos container |
| Supported SQL/PostgreSQL in-place | `inline`; source and destination must identify the same physical table |
| Cross-store pipelines | `queue`; require Service Bus, a running producer/watcher, and .NET workers |
| SharePoint to Cosmos DB Vector | `queue`; dedicated document partition key such as `/document_id`, no hierarchical partition keys; other destination types are rejected |
| OneLake to OneLake write-back | `queue` and `truncate`; same lakehouse and source table; Spark job and write-back columns required |
| OneLake to a separate vector store | `queue`; for example, Cosmos DB Vector or standalone Garnet, without lakehouse write-back |
| Synthetic benchmark | Explicit generation into an active `queue` pipeline; choose a real destination or the discard-only `null` sink |

Do not assume that selecting `chunk` is valid for every source/destination
pair. For example, OneLake same-row write-back rejects chunking. See the
[Cosmos field-content chunking contract](pipeline-tuning.md#cosmos-text-chunking-queue-mode)
and the [SharePoint synchronization contract](sharepoint-source.md#destination-and-synchronization-contract)
for their supported paths.

### Pipeline Detail Page

- **Status badge:** Active, Paused, Error
- **Stats:** Documents processed, failed, completion percentage
- **Source list** with content field configuration (read-only)
- **Action buttons:** Pause, Resume, Run, Reset, Delete

### Pipeline Lifecycle

```text
Create -> PAUSED -> Run or Resume -> ACTIVE
          ^                          |
          +---------- Pause ---------+
```

| Action | Effect | Important boundary |
|--------|--------|--------------------|
| **Run** | Activate continuous processing; recheck a disabled destination where supported | Not a single-document or one-time batch run |
| **Resume** | Activate a paused pipeline | Does not guarantee a full replay |
| **Pause** | Stop new source work after watcher reconciliation | Already queued/in-flight work may still write |
| **Reset** | Remove job records, clean supported chunk outputs, change generation, and request replay | Restores the previous active/paused state on success; cleanup failure leaves it paused |
| **Delete** | Remove pipeline configuration | Does not purge all destination data or cancel queued work |

`process_existing` is an initial-backfill setting, not an activation switch.
Reset and full source sync can repeat embedding and destination writes. Quiesce
producers/workers and account for queued messages before destructive recovery.

### Sharing sources and destinations

Queue pipelines can fan out from a source to separate destinations, subject to
connector compatibility. When sharing a destination, use pipeline-scoped document
identities; the default is `{source_hash}-{pipeline}`. The API rejects unsafe
shared-destination identity patterns unless overwrite is explicitly selected.
See [destination identity](architecture.md#destination-identity).

**Inline processing has a single active owner per physical source**, even if
pipelines choose different vector fields or use different source registrations.
Separate embedding paths do not bypass that rule. Use separate queue destinations
for independent models or content strategies.

### Locked settings after creation

The API keeps sources, destination, vector path, document-ID template, partition
template, and chunk-ID template immutable. Create a new pipeline for those
changes. Cosmos pipelines also keep content strategy and chunk text-storage
opt-in/field immutable.

Supported Cosmos chunk size, overlap, and unit changes affect future processing,
not existing vectors. Processing-mode changes have a dedicated API and must
still satisfy same-store, connector, and single-owner constraints; mode is not
an unrestricted throughput switch. The UI may expose fewer edit controls than
the API. See [pipeline tuning](pipeline-tuning.md) for live capacity settings.

---

## 4. Jobs

The Jobs page displays control-plane job records used by supported/legacy job
paths. Current Service Bus queue ingestion and inline processing do **not**
necessarily create one job record for every document. An empty Jobs page does
not prove a pipeline is idle or broken.

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

### Retries and dead letters

For queue pipelines, workers retry transient operations and use Service Bus
redelivery/dead-lettering. Inspect queue backlog, DLQ reasons, worker logs, and
destination progress. **Retry job is not a Service Bus DLQ redrive.**
Repair the dependency or bad input before replaying work.

Inline pipelines have no worker-queue DLQ; failed processing retains source
progress for retry and requires log/diagnostic inspection. The
[delivery reference](pipeline-tuning.md#settlement-and-failure-handling) explains
envelope retries and at-least-once behavior.

---

## 5. Vector Search Playground

Test your vector indexes with natural language queries.

1. Navigate to **Vector Search**.
2. Select one or more **destination indexes** (checkbox dropdown).
3. Enter a **natural language query**.
4. Click **Search**.

For vector search, the API resolves the destination's pipeline/model configuration
and embeds the query before searching. Results show:
- Score/rank (not a calibrated probability of relevance)
- Source metadata
- Content preview
- Index badge (when searching multiple indexes)

Searchable stores are Cosmos DB Vector, pgvector, standalone Garnet, and
OneLake destinations with a Garnet mirror. SQL Server destinations, OneLake
without a Garnet mirror, legacy Redis string mirrors, and null sinks are not
searchable through OmniVec. Full-text/hybrid search requires the corresponding
Cosmos configuration; pgvector and standalone Garnet provide vector search,
not full-text indexes.

If several pipelines use one destination, scope the request to the intended
pipeline where supported; do not assume every vector was produced by one model.
The API supports source/pipeline filters and full-text/hybrid options. Inspect
warnings and per-index errors even when the request succeeds. See the
[Search API guide](search-api.md) for the distinction between
`/api/playground/search` and standalone `/search`.

---

## 6. Models and DocGrok health

Register/test external models and inspect native model status. Check the selected
model's dimensions, input modality, batch/context limits, and endpoint access.
External Azure OpenAI deployments need no GPU pool; self-hosted models need
appropriate compute and memory. Model connectivity alone does not verify a
source-to-destination pipeline.

GPU utilization and other runtime measurements may be unavailable. A stopped
model or scaled-to-zero deployment is different from an unregistered model.

---

## 7. Deployments

### OmniVec Deployments

Scale and manage OmniVec components:
- `omnivec-api` — API server
- `omnivec-controller` — Control-plane bookkeeping
- `omnivec-dotnet-worker` — Queue document processing
- Source watcher deployments — Change feed, polling, or event ingestion

### DocGrok Deployments

Scale GPU models and pipeline workers:
- Native models (BGE, CLIP, DSE-Qwen2) — scale to 0 stops model pods, not necessarily the billable GPU nodes
- Pipeline worker (PaddleOCR) — scale for PDF processing throughput

Each deployment card shows:
- Name, image tag, status badge
- Ready/desired replica count
- Pod table (name, status, restarts, age)
- Action buttons: Scale +/−, Restart, Pause/Resume

Use the deployment names returned by your installation rather than assuming
every optional watcher/model exists. Manual scaling can be overridden by an
enabled autoscaler. More workers help only if source parallelism, model quota,
destination capacity, and memory allow it.

### Metrics and diagnostics

Live throughput and latency describe recent observations; historical ranges
describe a selected time window. Unknown measurements and incomplete coverage
are not zeros. For chunked sources, distinguish source documents, chunks,
embedding requests, and broker messages.

Use **Diagnostics** for read-only system/pipeline evidence without a chat model.
**Recovery Agent** requires a chat model for investigation and explicit approval
for supported changes. MCP/Foundry provisioning is a separate administrator
workflow, disabled by default. See [agent behavior](agent.md) and
[cloud deployment prerequisites](cloud-deployments.md).

---

## 8. Processing Modes

### Blob Storage → New Vector Documents

Source is Azure Blob Storage. Pipeline creates new documents in the destination for each processed blob.

### CosmosDB → Patch-in-Place (Inline)

Source and destination are the **same** CosmosDB container. The embedding is patched directly into the source document — no separate vector document created.

### CosmosDB → Separate Destination (Queue)

Source is CosmosDB, destination is a **different** container. New vector documents are upserted to the destination.

### Content Change Detection

Change detection is connector-specific. Cosmos inline processing uses content
hash and pipeline/reset metadata to avoid embedding its own unchanged patch
events. Other sources use change feeds, timestamps, versions, or Graph delta
state. Do not assume every connector detects deletions or offers the same
ordering/revision guarantees. Queue delivery is at-least-once.

---

## 9. Authentication

There are two separate access layers:

| Layer | Purpose |
|-------|---------|
| Console/API authentication | Admin/API token or configured Entra sign-in and application roles |
| Connector/model identity | Permission for workload processes to read sources, write destinations, access secrets, and call models |

An OmniVec role does not grant Azure, database, Graph, or Fabric permissions.
See [Entra sign-in setup](deployment.md#microsoft-entra-sign-in). Do not put tokens
or database passwords in source control or shared screenshots.

OmniVec uses **Azure Managed Identity / Workload Identity** for supported Azure
data-plane connections. Authentication is connector-specific, not universally
keyless: PostgreSQL can require database credentials, SQL Server supports
database credentials or Entra authentication, Databricks supports workload
identity or a Key Vault-backed PAT, and Garnet can use ACL authentication.

**Required RBAC for CosmosDB sources/destinations:**
1. `Cosmos DB Built-in Data Contributor` (SQL RBAC) — data operations
2. Account-level read/discovery permissions where required by the configured checks; use the scoped permissions report rather than assuming data-plane RBAC covers ARM operations

**Required RBAC for Blob Storage sources:**
- `Storage Blob Data Reader` (reading blobs)

**Additional connector requirements:** Graph application consent and site
grants for SharePoint; OneLake read/checkpoint write and Fabric job execution/
staging write permissions for OneLake write-back; SQL Warehouse/table access
for Databricks; and Key Vault secret read permission when a connector uses
secret references. Queue producers need Service Bus Send, and envelope-aware
workers need both Send and Listen for durable failure handoff. A successful
model connection does not prove source or destination permissions.

---

## 10. Common Tasks

### Embed CosmosDB documents in-place

1. Create a **source** pointing to your CosmosDB container
2. Create a **destination** pointing to the **same** container (must have vector embedding policy)
3. Create an **inline** pipeline with `process_existing: true` and the correct content fields
4. Run or Resume it; verify embeddings on the original documents

Only one active inline pipeline may own that physical source.

### Embed blob storage documents

1. Create a **source** pointing to your blob container
2. Create a **destination** (CosmosDB with vector index)
3. Create a **queue** pipeline with supported file filters and a compatible extraction/model route
4. Run or Resume it; verify enumeration, destination writes, and search

Polling detects creates/updates. Event Grid requires a subscription for the source
account, delivery permissions, and an enabled consumer. Polling alone does not
detect deletions. Check event-provisioning results rather than assuming pipeline
creation established live event delivery.

### Scale for faster processing

Use Metrics/Diagnostics to identify the bottleneck first. Review model quotas,
worker concurrency/memory, source parallelism, and destination write capacity.
Increase the relevant component only after measuring those limits. For queue
packing and the fixed-replica requirement of optional broker lanes, see
[pipeline tuning](pipeline-tuning.md).

### Synthetic source and null destination benchmarks

Use these **only for controlled benchmarks**. A synthetic source generates
documents rather than reading customer data. A null destination discards all
embeddings: completion means the worker reached the sink, not that vectors
were stored or made searchable.

Register each through the REST API:

```json
{
  "name": "Synthetic queue benchmark",
  "type": "synthetic",
  "config": {
    "benchmark_only": true,
    "document_count": 10000,
    "message_size_bytes": 1024,
    "docs_per_second": 100,
    "content_pattern": "deterministic",
    "mock_embedding": false
  }
}
```

Send this body to `POST /api/sources`. For a discard-only destination, send:

```json
{
  "name": "Benchmark discard sink",
  "type": "null",
  "config": {
    "benchmark_only": true,
    "max_docs_per_second": 0,
    "rate_key": "queue-benchmark"
  }
}
```

Send the second body to `POST /api/destinations`. Create an active queue-mode
pipeline referencing the returned IDs and an embedding model. The API requires
`SYNTHETIC_SERVICEBUS_NAMESPACE` (wired from `azure.serviceBus.namespace` by
Helm). As an operator or administrator, start a run with
`POST /api/sources/{source_id}/generate` and body
`{"pipeline_id": "<pipeline-id>"}`; inspect publication status with
`GET /api/sources/{source_id}/generate/{run_id}`. Publication completion is
not worker completion; check pipeline processing, errors, and dead letters.

`docs_per_second: 0` publishes as fast as Service Bus accepts messages; it is
not a guaranteed processing rate. `message_size_bytes` controls the serialized
single-document body, while pipeline Capacity settings control queue envelope
packing. A positive null-sink `max_docs_per_second` simulates a bottleneck per
worker process and `rate_key`, not a cluster-wide quota.

Keep `mock_embedding: false` to exercise the actual model. Mock runs require
both source `mock_embedding: true` and worker opt-in
`dotnetWorker.enableBenchmarkMockEmbedding: true` in Helm (environment:
`Worker__EnableBenchmarkMockEmbedding`). Choose `mock_embedding_dimensions`
to match a real destination if using one. Synthetic/mock/null results do not
establish production model throughput, storage durability, or search quality.

---

## 11. API Quick Reference

These are the main control-plane routes. Authenticate against the OmniVec
origin; the standalone search service has a separate token scope.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST` | `/api/sources` | List / Create sources |
| `GET/PUT/DELETE` | `/api/sources/{id}` | Read / Update / Delete a source registration |
| `POST` | `/api/sources/{id}/test` | Test connection |
| `POST` | `/api/sources/{id}/sync` | Request asynchronous source synchronization |
| `GET` | `/api/source-syncs/{operation_id}` | Inspect synchronization readiness |
| `POST` | `/api/sources/{id}/generate` | Start synthetic benchmark publication (operator/admin) |
| `GET` | `/api/sources/{id}/generate/{run_id}` | Read synthetic publication status |
| `GET/POST` | `/api/destinations` | List / Create destinations |
| `POST` | `/api/destinations/{id}/test` | Test a saved destination |
| `GET/POST` | `/api/pipelines` | List / Create pipelines |
| `GET/PUT/DELETE` | `/api/pipelines/{id}` | Read / Update / Delete configuration |
| `POST` | `/api/pipelines/{id}/run` | Activate continuous processing |
| `POST` | `/api/pipelines/{id}/pause` | Pause new source work after reconciliation |
| `POST` | `/api/pipelines/{id}/resume` | Activate a paused pipeline |
| `POST` | `/api/pipelines/{id}/reset` | Request reset/replay; destructive cleanup may occur |
| `PATCH` | `/api/pipelines/{id}/resource-policy` | Update supported capacity policy |
| `GET` | `/api/jobs` | List jobs (`?pipeline_id=`, `?status=`) |
| `POST` | `/api/jobs/{id}/retry` or `/api/jobs/{id}/cancel` | Metadata job management, not DLQ redrive |
| `GET/POST` | `/api/models` | List / Register models |
| `POST` | `/api/playground/search` | Search by registered destination IDs |
| `GET` | `/api/operations/deployments` | Kubernetes deployment inventory |
| `GET` | `/api/health/checks` | Dependency/component health evidence |
| `GET` | `/api/metrics/live` | Live measurements and coverage |
| `GET` | `/api/metrics/timeseries` | Historical measurements |
| `GET` | `/health` | API liveness, not an end-to-end health check |
