# OmniVec architecture

[README](../README.md) | [User guide](user-guide.md) | [Deployment](deployment.md)

OmniVec separates **configuration**, **ingestion**, **embedding**, and
**retrieval**. It runs on AKS, but customer data, models, and destination stores
can live outside the OmniVec resource group. Connector access must be configured
separately from deploying the application.

## System overview

```text
Browser / CLI / application
              |
        web proxy / API -------- metadata, model and pipeline configuration
              |                                  |
              |                         source watchers
              |                          /           \
              |                  queue mode         inline mode
              |                      |                   |
              |                 Service Bus              |
              |                      |                   |
              |                .NET workers              |
              |                      \                   /
              |                         DocGrok
              |                    extract / embed
              |                           |
              |                     destinations
              |
       search resolver -> search service -> destination indexes
                               |
                         query embedding
                          via DocGrok
```

This is a logical flow, not a claim that all connectors support both modes.
See the [compatibility matrix](user-guide.md#source-and-destination-compatibility).

## Components

| Component | Responsibility |
|-----------|----------------|
| `omnivec-web` | Static HTML/JavaScript console and nginx reverse proxy |
| `omnivec-api` | Python FastAPI control plane: configuration, validation, auth, lifecycle, metrics, and proxies |
| `omnivec-controller` | Control-plane bookkeeping and metadata job/stat maintenance |
| `omnivec-changefeed` | .NET Cosmos change feed and supported SQL/PostgreSQL watchers |
| `omnivec-blob-ingestor` | Blob enumeration/polling and optional Event Grid consumption |
| Dedicated Databricks/SharePoint watchers | Connector-specific ingestion using the .NET ingestion image |
| `omnivec-onelake-iceberg-watcher` | Python/PyIceberg scans and OneLake checkpoint management |
| `omnivec-dotnet-worker` | Service Bus consumption, content processing, model requests, destination writes, and settlement |
| `docgrok` router | Embedding dispatch to registered external/native models and document transforms |
| DocGrok pipeline worker / model workloads | Document extraction/OCR and model execution as configured |
| `omnivec-search` | Multi-index retrieval with per-index embedding policies |
| `omnivec-agent` | Read-only diagnostics and bounded approval-gated recovery |

The current queue processing path uses .NET workers, **not Python workers polling
Cosmos job records**. Actual deployment names, optional components, replica counts,
and resource requests come from [Helm values](../helm/omnivec/values.yaml).

The web service exposes `/ui`, static assets, `/api/*`, and API `/health`.
`/nginx-health` checks nginx itself. There is no direct `/docgrok` web-proxy route;
the control plane exposes supported DocGrok operations under `/api/docgrok/*`.

## Ingestion paths

### Queue mode

1. A source watcher reads a page, feed batch, or event for active pipelines.
2. It resolves the pipeline's model, identity, content, destination, and resource
   policy into work messages.
3. It publishes to Service Bus. Source progress advances only after required
   publications succeed; this means **queued**, not **stored in the destination**.
4. Workers receive deliveries, obtain processing capacity, extract content, and
   request embeddings through DocGrok.
5. Workers write results and settle the delivery, or retry/hand off a durable
   failure according to the worker/broker policy.

The normal broker path uses the `embeddings` topic and `worker` subscription.
Ordinary pipelines share the worker pool. Optional version-1 envelopes pack
multiple compatible documents into one message. Optional pipeline lanes change
broker routing but do not isolate model or destination capacity.

Both features, including rollout and rollback restrictions, are documented in
[pipeline tuning](pipeline-tuning.md).

### Inline mode

Supported Cosmos/SQL/PostgreSQL same-store pipelines embed and update the
original container/table without sending work through the embedding queue.
Source and destination must refer to the same physical store.

Only one active inline owner is allowed per physical source, even when two
pipelines select different vector fields or register the same store under
different source IDs. Discovery checks are not a transactional cancellation
fence: already-running work can finish before reconciliation. Use queue-mode
fan-out to separate destinations for independent pipelines.

Cosmos patch-feedback detection uses content hash, pipeline identity, and reset
metadata. An existing embedding timestamp alone does not mean changed content
can be skipped.

### Connector-specific paths

| Source/path | Change tracking and important boundary |
|-------------|----------------------------------------|
| Cosmos DB | Native partitioned change feed; lease ownership controls processing |
| Blob | Initial enumeration and polling for creates/updates; Event Grid delivery is required for delete events |
| PostgreSQL | Recognized non-null tracking timestamp plus primary-key bookmark; otherwise repeated full scans |
| SQL Server | Initial scan followed by CDC; an expired CDC position requires replay |
| Databricks | Version-pinned snapshot followed by complete Delta CDF commits |
| SharePoint | Graph delta, durable outbox, stable item identity, and revision-fenced Cosmos synchronization |
| OneLake | Iceberg scans; optional separate long-running Spark CDF publisher for live changes |
| OneLake write-back | Worker stages embeddings durably; a separate Fabric Spark MERGE updates existing source rows asynchronously |
| Synthetic | Explicit benchmark publication, not a continuously watched customer store |

SharePoint requires queue mode and a Cosmos vector container with a dedicated
document partition key, such as `/document_id`. OneLake write-back is restricted
to the same lakehouse/source table and `truncate`; it is not an append sink.
See the [SharePoint](sharepoint-source.md) and [OneLake](onelake-iceberg.md)
contracts for their additional permissions and ordering behavior.

## State, checkpoints, and replay

Control-plane records use the Cosmos `metadata` container, partitioned by
`/doc_type`. Sources, destinations, pipelines, models, auth records, and other
control entities are distinct from customer vector data.

The ingestion service uses preprovisioned shared containers:

| State | Container / location | Scope |
|-------|----------------------|-------|
| Cosmos feed checkpoints and polling-source ownership | `source-leases`, partition key `/id` | Namespaced processor/source/generation ownership |
| Durable Blob state and SharePoint synchronization state/outboxes | `source-state`, partition key `/scopeId` | Source/pipeline scope |
| OneLake scan checkpoints | Configured OneLake checkpoint files | Source/pipeline configuration and identity scope |
| Fabric CDF streaming progress | Configured Spark checkpoint location | Streaming query/microbatch |

SQL, PostgreSQL, and Databricks watcher cursors remain process-local; restart
can replay source data. Do not generalize SharePoint's durable revision/outbox
protocol to every connector.

Delivery is **at least once**. A failed publication can replay a source page;
a failed envelope can replay documents already written; an uncertain settlement
can redeliver work. Deterministic IDs help avoid duplicate items but do not make
every multi-document update atomic or revision-fenced.

Metadata Jobs-page records are not the complete queue ledger. Observe source
progress, broker backlog/DLQ, worker errors, and actual destination writes.
OneLake staging acceptance is not proof that Spark MERGE has completed.

## Destination identity

Queue pipelines distinguish three identities:

| Field | Purpose / default |
|-------|-------------------|
| `doc_id_pattern` | Destination item ID; default `{source_hash}-{pipeline}` |
| `chunk_config.doc_id_pattern` | Human-readable chunk suffix; Cosmos chunk processing adds a deterministic isolation prefix |
| `partition_key_pattern` | Destination logical partition; compatibility default `{source_partition}` |

When a destination is shared, the API rejects item templates without
`{pipeline}` or `{pipeline_hash}` unless `collision_policy: "overwrite"` is
explicitly selected. Overwrite is a deliberate collision/data-replacement risk,
not an isolation mechanism.

Partition templates may use supported source, pipeline, destination, and model
identifiers/hashes. The worker renders partition identity consistently for
writing and tombstone deletion. Older messages without a partition template
retain `{source_partition}` behavior.

Use `POST /api/pipelines/identity-preview` to inspect identities before saving.
Document, partition, and chunk-ID templates are immutable after creation.
Changing them requires a new pipeline and a scoped migration plan; do not delete
records and then attempt an unsupported in-place template update.

Before cleanup or migration, quiesce producers/workers and reconcile queued
deliveries. Verify ownership and actual partition keys of every affected record.
For Cosmos chunk variables, required namespace prefixes, replacement behavior,
and immutable text-storage settings, see the
[chunking contract](pipeline-tuning.md#cosmos-text-chunking-queue-mode).

## Models and DocGrok

An **OmniVec pipeline** connects sources to a destination. A **DocGrok transform**
defines processing stages such as file extraction, OCR, chunking, and embedding.
A registered embedding model may also be selected directly.

The Rust router dispatches to external providers such as Azure OpenAI or
self-hosted model workloads such as BGE, CLIP, and DSE-Qwen2. Compute, memory,
modality, and output dimensions depend on the chosen model; there is no universal
GPU size or dimension count. External models do not require an OmniVec GPU pool.

Model `max_batch_size`, `context_window_tokens`, and `max_batch_tokens` bound
requests. Pipeline settings can lower those limits, not raise them. The
[tuning guide](pipeline-tuning.md#embedding-limits-and-resilience) covers estimates,
token headroom, retries, and snapshot refresh behavior.

## Retrieval

Most clients use `POST /api/playground/search` on the control-plane origin.
The API resolves registered destination IDs into search specifications and calls
the internal search service using `SEARCH_INTERNAL_TOKEN`.

The standalone `POST /search` endpoint instead accepts self-describing index
specifications. Its Helm service is **ClusterIP by default**, not a second public
load balancer. Expose it only through a deliberately configured, authenticated,
TLS-protected access path.

Supported stores are Cosmos DB, pgvector, and Garnet. A OneLake destination is
searchable through a configured Garnet mirror, not by querying the lakehouse
directly. SQL Server and null sinks are not search backends. Full-text search is
Cosmos-only; hybrid retrieval combines vector and full-text rankings.

Per-index failures can produce a partial successful response. Inspect errors,
warnings, and result provenance rather than treating HTTP 200 as complete
retrieval. See [Search API](search-api.md).

## Identity and access

| Layer | Responsibility |
|-------|----------------|
| User/API credential | Console/API role and operation authorization |
| AKS workload identity | Azure data/management access granted to the workload |
| Connector-specific credentials | Database credentials, Graph/Fabric federation, Garnet ACL, or supported secret references |
| Search token | Standalone search scope, separate from control-plane credentials |
| Cloud deployment identity | Explicitly allowed Azure scopes and administrator-approved provisioning |

Workload identity reduces the need for static Azure keys; it does not make all
connectors keyless. Credentials and configured secret references still require
access controls. Metadata, stored content, vectors, staging, and telemetry have
different retention and deletion requirements.

SharePoint/OneLake cross-tenant identity selectors are pipeline-scoped and require
federated application setup plus service grants. Signing in to OmniVec does not
grant those permissions.

The [Recovery Agent](agent.md) and [MCP/Foundry deployment](cloud-deployments.md)
have separate bounded approval contracts. Readiness, a successful grant plan,
or a model-generated answer does not prove data processing or semantic correctness.

## Scaling and operations

The worker pool uses fixed replicas by default, with optional CPU HPA or
Service Bus KEDA scaling. KEDA must be installed before enabling it:

```yaml
dotnetWorker:
  autoscaling:
    enabled: true
    type: keda
    minReplicas: 2
    maxReplicas: 10
    messageCountPerReplica: 50
```

This observes the normal `embeddings/worker` backlog. Packing documents into
envelopes changes the meaning of that message count. Stage-1 pipeline lanes
reject both HPA and KEDA and require fixed replicas. Do not mix their configurations.

Scale according to the bottleneck: source partitions/polling, worker memory,
model quota/latency, destination throughput, or Spark capacity. More replicas
cannot overcome an unrelated quota or single-writer restriction.

| Signal | What it establishes |
|--------|---------------------|
| `/health` on the API | API liveness |
| `/api/health/checks` | Dependency/component observations |
| `/ready` on search | DocGrok reachability from search |
| Live/historical metrics | Observed throughput, latency, failures, and coverage within their respective windows |
| Destination growth plus expected retrieval | Evidence of processing and retrieval, within the checked scope |

New pipelines start paused. Pause/delete/reset are not worker-cancellation
fences. Use the [lifecycle guide](user-guide.md#pipeline-lifecycle) before recovery
actions and the [deployment guide](deployment.md) for infrastructure/image changes.
