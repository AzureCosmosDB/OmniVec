# OmniVec Architecture

## Overview

OmniVec is a **Universal Vector Ingestion Platform** that watches source data stores
(primarily Azure CosmosDB containers), extracts text content, generates vector
embeddings via a dedicated embedding microservice (DocGrok), and writes the
embeddings back to the source documents (inline mode) or to a separate vector
store (queue mode).

The system is composed of three major subsystems:

1. **Control Plane API** -- FastAPI service managing Sources, Destinations,
   Pipelines, and Jobs.
2. **Change Feed Processor (CFP) Connector** -- .NET background service that
   watches CosmosDB source containers via Change Feed and drives the embedding
   pipeline.
3. **DocGrok** -- Embedding/transform microservice exposing `/embed/batch`.
   Runs native GPU models (bge-small, bge-large) and proxies external models
   (Azure OpenAI text-embedding-3-small/large).

```
                          +------------------+
                          |   OmniVec API    |
                          |  (FastAPI:8080)  |
                          +--------+---------+
                                   |
                    +--------------+--------------+
                    |                              |
           +-------v--------+           +---------v--------+
           | CosmosDB       |           | CosmosDB         |
           | "metadata"     |           | Source Containers |
           | (control plane)|           | (user data)      |
           +----------------+           +--------+---------+
                                                 |
                                         Change Feed (Latest Version)
                                                 |
                          +----------------------v-----------------------+
                          |       CFP Connector (.NET, 6 replicas)       |
                          |                                              |
                          |  SourceDiscoveryService (BackgroundService)  |
                          |       |                                      |
                          |  SourceWatcherManager                        |
                          |       |                                      |
                          |  SourceWatcher[0..N]                         |
                          |       |  (one ChangeFeedProcessor per source)|
                          |       |                                      |
                          |  LeaseContainerManager                       |
                          |       |  (leases-{source_id} containers)     |
                          +-------+--------------------------------------+
                                  |
                        +---------v----------+
                        |      DocGrok       |
                        |  /embed/batch      |
                        |  (GPU: bge-small)  |
                        +--------------------+
```

---

## Data Model

All control plane state lives in a single CosmosDB container called `metadata`
with partition key `/doc_type`. The four entity types are:

| Entity      | ID Prefix | doc_type      | Purpose                                    |
|-------------|-----------|---------------|--------------------------------------------|
| Source      | `src-`    | `source`      | Pointer to a CosmosDB container to watch   |
| Destination | `dst-`    | `destination` | Where to write vectors (if queue mode)     |
| Pipeline    | `pip-`    | `pipeline`    | Binds source(s) to model + destination     |
| Job         | `job-`    | `job`         | Single document processing unit (queue mode)|

A **Source** stores connection info (`endpoint`, `database`, `container`,
`content_field`) and is defined in the API. A **Pipeline** references one or more
sources and specifies:

- `docgrok_pipeline` -- the model ID (e.g. `mdl-native-bge-small`)
- `processing_mode` -- `"inline"` or `"queue"`
- `reset_at` -- ISO timestamp; when set, triggers a full replay of the source

Relevant source files:
- `/home/cdbmvs/omnivec/api/api.py` -- FastAPI routes
- `/home/cdbmvs/omnivec/api/models.py` -- Pydantic models
- `/home/cdbmvs/omnivec/api/store.py` -- CosmosDB persistence wrapper
- `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Models/Source.cs`
- `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Models/Pipeline.cs`

---

## Change Feed Processing Architecture

This is the heart of OmniVec's throughput story. The .NET CFP Connector uses the
Azure Cosmos DB Change Feed Processor library (Latest Version mode) to stream
document changes from source containers and process them in near-real-time.

### Component Hierarchy

```
SourceDiscoveryService (BackgroundService)
    |
    |  Polls API every 30s for active pipelines + CosmosDB sources
    |  Detects pipeline reset_at changes
    |
    v
SourceWatcherManager
    |
    |  Reconciles running watchers against desired state
    |  Handles generation-based resets (stop old, start new)
    |
    v
SourceWatcher[per source]
    |
    |  Wraps one ChangeFeedProcessor instance
    |  Handles the onChanges delegate (inline or queue mode)
    |
    v
LeaseContainerManager
    |
    |  Creates/manages lease containers in omnivec-cosmos
    |  One container per source: leases-{source_id}
    |
    v
CosmosDB Lease Container (leases-{source_id})
    |
    |  One lease document per physical partition of the source
    |  Stores continuation tokens (checkpoints)
    |
    v
ChangeFeedProcessor (Azure SDK)
    |
    Reads changes from source container partitions
```

### SourceDiscoveryService

**File:** `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Hosting/SourceDiscoveryService.cs`

A .NET `BackgroundService` that runs a reconciliation loop:

1. Waits 5 seconds on startup for the API to be ready.
2. Every `SourcePollIntervalSeconds` (default 30s), fetches all enabled CosmosDB
   sources and active pipelines from the OmniVec API.
3. Filters sources to only those referenced by at least one active pipeline.
4. Calls `DetectResets()` to compare each pipeline's `reset_at` against the
   last-known value. On first poll after startup, it captures the snapshot
   without triggering resets (avoids spurious replays on deployment rollout).
5. For any source affected by a reset, calls
   `SourceWatcherManager.ResetWatcherAsync()`.
6. Calls `SourceWatcherManager.ReconcileAsync()` to start/stop/update watchers.

### SourceWatcherManager

**File:** `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Services/SourceWatcherManager.cs`

Maintains a `ConcurrentDictionary<string, SourceWatcher>` of running watchers
keyed by source ID. Reconciliation logic:

- **New source:** Create and start a `SourceWatcher`.
- **Removed source:** Stop and dispose the watcher.
- **Generation change:** Stop the old watcher, start a new one with the new
  generation tag (see Generation-Based Reset below).
- **Existing, same generation:** Update pipeline references only.

### SourceWatcher

**File:** `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Services/SourceWatcher.cs`

Each `SourceWatcher` wraps exactly one `ChangeFeedProcessor` instance bound to a
single source CosmosDB container. On `StartAsync()`:

1. Creates a `CosmosClient` to the source container using `DefaultAzureCredential`.
2. Discovers the partition key path from container properties (e.g. `/source_id`).
3. Ensures the lease container exists via `LeaseContainerManager`.
4. Builds the `ChangeFeedProcessor` with:
   - `processorName` = `omnivec-cf-{sourceId}-gen{generation}`
   - `instanceName` = hostname of the pod
   - `maxItems` = 500 (configurable)
   - `pollInterval` = 5 seconds (configurable)
   - `startTime` = `DateTime.MinValue` (read from beginning on first run)
5. Starts the processor.

The `HandleChangesAsync` delegate is called by the CFP SDK for each batch of
changes on each partition.

### LeaseContainerManager

**File:** `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Services/LeaseContainerManager.cs`

Creates lease containers in the `omnivec` database on the OmniVec CosmosDB
account (not the source account). Container naming: `leases-{source_id}` with
partition key `/id`. Uses `CreateContainerIfNotExistsAsync` with a local
`ConcurrentDictionary` cache to avoid repeated creation calls.

Also provides `DeleteLeaseContainerAsync` for full resets (though the
generation-based approach has largely superseded explicit deletion).

### ChangeFeedOptions

**File:** `/home/cdbmvs/omnivec/connectors/ingestion/dotnet/Configuration/ChangeFeedOptions.cs`

| Setting                    | Default                                           | Purpose                            |
|----------------------------|---------------------------------------------------|------------------------------------|
| `OmniVecApiBaseUrl`        | `http://omnivec-api:8080`                         | API for source/pipeline discovery  |
| `OmniVecCosmosEndpoint`    | (configured)                                      | CosmosDB for lease containers      |
| `OmniVecDatabase`          | `omnivec`                                         | Database for leases                |
| `SourcePollIntervalSeconds`| `30`                                              | How often to poll API              |
| `InstanceName`             | `Environment.MachineName`                         | Pod hostname for lease ownership   |
| `MaxItemsPerBatch`         | `500`                                             | Max docs per CF batch              |
| `FeedPollIntervalSeconds`  | `5`                                               | CF poll interval                   |
| `MaxJobCreationRetries`    | `3`                                               | Retries for job creation           |
| `ErrorBackoffSeconds`      | `30`                                               | Backoff on discovery errors        |
| `DocGrokBaseUrl`           | `http://docgrok.docgrok.svc.cluster.local:80`     | DocGrok K8s service endpoint       |

---

## Two Processing Modes

### Inline Mode (Primary, High-Throughput)

This is the production path. The CFP connector processes documents end-to-end
without touching the job queue:

```
Source Container
    |
    | Change Feed (batches of up to 500 docs)
    v
SourceWatcher.HandleChangesAsync()
    |
    | Phase 1: Extract content, compute content_hash, filter eligible docs
    | Phase 2: Sub-batch (100 native / 50 external) to DocGrok /embed/batch
    | Phase 3: PatchByPartitionBatch back to source container
    v
Source Container (now has /embedding, /embedded_at, /content_hash, etc.)
```

Key characteristics:
- **Zero queue overhead** -- no Job creation, no Worker polling.
- **Batch embedding** -- sends up to 100 texts per DocGrok call (50 for
  external models to respect rate limits).
- **TransactionalBatch patching** -- groups docs by partition key, patches
  each group in a single TransactionalBatch (max 100 ops per batch), reducing
  N individual round-trips to P partition-grouped requests.
- **Global concurrency throttle** -- `SemaphoreSlim(3000)` prevents overwhelming
  CosmosDB when multiple partitions are being processed concurrently across all
  watchers on a single instance.

### Queue Mode

The original path, still available for non-CosmosDB destinations or external
processing:

```
Source Container
    |
    | Change Feed
    v
SourceWatcher.HandleChangesAsync()
    |
    | Creates PENDING jobs via POST /api/jobs/bulk (batches of 50)
    v
OmniVec API --> metadata container (Job docs)
    |
    v
Worker (Python) polls for PENDING jobs --> processes --> writes to destination
```

---

## Scaling Model

### How CFP Distributes Work

The CosmosDB Change Feed Processor uses a **lease-based partition assignment**
model. Here is how it scales:

```
Source container: 17 physical partitions (P0..P16)
CFP replicas: 6 pods (N=6)

Lease container: leases-{source_id}
  One lease document per partition (17 lease docs)
  Each lease is "owned" by exactly one instance at a time

Distribution (approximate):
  Pod-0 (instance="cfp-pod-abc"):  P0,  P1,  P2   (3 partitions)
  Pod-1 (instance="cfp-pod-def"):  P3,  P4,  P5   (3 partitions)
  Pod-2 (instance="cfp-pod-ghi"):  P6,  P7,  P8   (3 partitions)
  Pod-3 (instance="cfp-pod-jkl"):  P9,  P10, P11  (3 partitions)
  Pod-4 (instance="cfp-pod-mno"):  P12, P13, P14  (3 partitions)
  Pod-5 (instance="cfp-pod-pqr"):  P15, P16       (2 partitions)
```

Key properties:

1. **Each partition is owned by exactly one instance at a time.** The CFP SDK
   writes an `Owner` field in the lease document using etag-based optimistic
   concurrency. If an instance crashes, its leases expire and are stolen by
   surviving instances.

2. **Automatic rebalancing.** When a new instance starts (or one dies), the CFP
   SDK redistributes leases so that each instance owns roughly
   `ceil(P / N)` partitions.

3. **Instance identity.** Each pod uses its hostname as `InstanceName`
   (from `Environment.MachineName`). This is unique per Kubernetes pod and
   stable for the pod's lifetime.

4. **All instances run identical code.** The `SourceDiscoveryService` on every
   replica creates the same set of `SourceWatcher` instances (one per source).
   The CFP SDK handles the coordination -- no application-level leader election
   is needed.

### Scaling Up

To increase throughput, scale the CFP Deployment replica count. The CFP SDK
automatically rebalances leases:

```
kubectl scale deployment omnivec-cfp --replicas=12 -n omnivec
```

Upper bound: replica count should not exceed the number of physical partitions
in the source container. Beyond that, extra replicas sit idle (no partitions to
claim). In practice, 17 partitions / 6 replicas is a good ratio given that each
replica can saturate 2-3 partitions of embedding work.

---

## Throughput Analysis

### Pipeline: CF Batch --> DocGrok Embed --> Patch Back

For a single partition processing a batch:

```
Step                          Time       Notes
----                          ----       -----
CF read (500 docs)            ~50ms      SDK reads from partition
Content extraction + hash     ~5ms       CPU-bound, SHA256
DocGrok /embed/batch          ~2-3s      bge-small on A100, 100-text sub-batches
TransactionalBatch patch      ~0.5-1s    Groups by PK, max 100 ops/batch
                              --------
Total per batch               ~3-5s
```

### Aggregate Throughput

With 17 partitions processed in parallel across 6 instances:

```
500 docs/batch * 17 partitions / 3.5s avg = ~2,400 docs/sec (peak)
500 docs/batch * 17 partitions / 5.0s avg = ~1,700 docs/sec (sustained)
```

Observed range: **1,350 - 2,400 docs/sec** on CF replay (full re-embed) with
bge-small (384 dimensions) on 4 GPU replicas of DocGrok.

### Bottlenecks

1. **Embedding latency** -- The dominant cost. External models (Azure OpenAI)
   are significantly slower and subject to rate limits; sub-batch size is
   reduced to 50 for external models.
2. **CosmosDB RU budget** -- Each Patch operation costs ~10 RU. At 2,000
   docs/sec that is 20,000 RU/s for writes alone. The source container must
   be provisioned appropriately (or autoscaled).
3. **429 backpressure** -- TransactionalBatch does NOT get automatic SDK retries
   for 429 responses. The code handles this explicitly with exponential backoff
   (500ms * 2^attempt). The `SemaphoreSlim(3000)` global throttle prevents
   runaway concurrency.

---

## Generation-Based Reset

This is the mechanism that allows users to trigger a full re-embed of all
documents in a source container, without downtime and without coordination
between replicas.

### The Problem

CosmosDB Change Feed (Latest Version mode) only delivers changes from the last
checkpoint forward. Once a document has been processed and its lease checkpoint
advanced, the CFP will never see it again -- unless you start over.

### The Solution

Each `SourceWatcher` uses a **generation-tagged processor name**:

```
processorName = "omnivec-cf-{sourceId}-gen{generation}"

where generation = SHA256(reset_at)[:8]   (e.g. "a3f7c2e1")
      or "0" if no reset_at has been set
```

When a user clicks "Reset" in the UI:

```
1. UI calls PATCH /api/pipelines/{id} with reset_at = now()
2. SourceDiscoveryService polls API, detects reset_at changed
3. SourceWatcherManager stops old watcher (gen "0")
4. SourceWatcherManager starts new watcher (gen "a3f7c2e1")
5. New processorName = "omnivec-cf-src-xxx-gena3f7c2e1"
6. CFP SDK sees no lease docs for this processorName
7. CFP creates fresh lease docs, starts reading from beginning
8. All documents flow through HandleChangesAsync again
```

### Why This Works Across N Replicas

Every replica independently:
1. Polls the API and sees the new `reset_at`.
2. Computes the same generation hash (deterministic: `SHA256(reset_at)[:8]`).
3. Stops its old watcher and starts a new one with the new processor name.
4. The new CFP instance on each replica shares the same lease container.
5. The CFP SDK distributes the fresh leases across all N replicas.

No inter-replica communication is needed. Each replica converges independently
on the same processor name within one poll cycle (30 seconds).

### Old Lease Cleanup

Old lease documents (for the previous generation) are simply abandoned in the
`leases-{source_id}` container. They are harmless -- no CFP instance will ever
claim them again because the processor name has changed. They can be cleaned up
asynchronously if desired, but in practice the cost of a few hundred small
documents in a serverless container is negligible.

### Feedback Loop Prevention

Patching embeddings back to the source container produces new Change Feed events
(this is inherent to Latest Version mode -- there is no way to suppress it).
Without protection, this would cause an infinite loop: embed --> patch --> CF
event --> embed --> patch --> ...

OmniVec uses a two-layer defense:

**Layer 1: content_hash dedup.** Each document gets a `content_hash` (SHA256 of
the content field) when embedded. On the next CF event, the handler computes the
hash of the incoming content and compares it to the stored hash. If they match
and the document is not subject to a reset, it is skipped.

**Layer 2: reset_at vs embedded_at timestamp comparison.** When a reset is
active (`reset_at` is set):
- Documents where `embedded_at < reset_at` need reprocessing (they were embedded
  before the reset was requested).
- Documents where `embedded_at > reset_at` are feedback events from the current
  reset cycle -- skip them.

Together these two checks ensure every document is processed **exactly once per
reset generation**.

```
Document lifecycle during reset:

  1. Doc has: content_hash="abc", embedded_at="2025-01-01T00:00:00Z"
  2. User resets pipeline: reset_at="2025-06-15T12:00:00Z"
  3. CF delivers doc to handler
     - content_hash matches ("abc" == "abc")
     - BUT embedded_at (Jan 1) < reset_at (Jun 15) --> needs reprocessing
  4. Handler embeds doc, patches:
     - embedded_at = "2025-06-15T12:01:00Z" (now)
     - content_hash = "abc" (unchanged)
  5. Patch triggers new CF event
  6. CF delivers doc again
     - content_hash matches ("abc" == "abc")
     - embedded_at (Jun 15 12:01) > reset_at (Jun 15 12:00) --> skip
  7. Done. No infinite loop.
```

---

## DocGrok Integration

DocGrok is the embedding microservice that OmniVec delegates text-to-vector
conversion to.

### Model Types

| Model ID Pattern       | Type     | Example                    | Hardware           |
|------------------------|----------|----------------------------|--------------------|
| `mdl-native-*`         | Native   | `mdl-native-bge-small`     | GPU (A100)         |
| `mdl-ext-*`            | External | `mdl-ext-fb8c70b0`         | Azure OpenAI API   |

### Batch Embedding

The CFP connector calls `POST /embed/batch` with:

```json
{
  "model_id": "mdl-native-bge-small",
  "texts": ["doc1 content", "doc2 content", "..."]
}
```

Sub-batch sizes:
- **Native models:** 100 texts per request (GPU can handle the batch size
  efficiently).
- **External models:** 50 texts per request (to stay within Azure OpenAI rate
  limits).

Response:
```json
{
  "outputs": [[0.123, -0.456, 0.789], [0.789, 0.012, -0.345]]
}
```

### Retry Logic

DocGrok calls include 429 retry with exponential backoff (up to 5 attempts).
The `RetryAfter` header from the response is respected if present.

---

## Inline Patch Strategy

### TransactionalBatch Grouping

After embedding, documents must be patched back to the source container. Rather
than issuing N individual Patch operations, the connector groups documents by
their partition key value and uses `TransactionalBatch`:

```
500 docs with 17 distinct partition keys
    |
    | GroupBy(doc => doc.pkValue)
    v
17 groups (avg ~29 docs each)
    |
    | Each group -> TransactionalBatch (max 100 ops per batch)
    v
17 TransactionalBatch requests (instead of 500 individual patches)

~29x reduction in network round-trips
```

Each patch operation sets six fields on the source document:

```csharp
PatchOperation.Set("/embedding", floats),        // The embedding vector
PatchOperation.Set("/embedded_at", now),          // ISO timestamp
PatchOperation.Set("/embedding_dims", dims),      // e.g. 384
PatchOperation.Set("/pipeline_id", pipelineId),   // Which pipeline produced this
PatchOperation.Set("/pipeline_name", name),       // Human-readable name
PatchOperation.Set("/content_hash", hash),        // SHA256 of content field
```

### Concurrency Control

A `static SemaphoreSlim(3000, 3000)` shared across all `SourceWatcher` instances
within a process throttles the maximum number of concurrent patch operations.
This prevents a burst of completions from overwhelming the CosmosDB account's
RU budget.

The semaphore is acquired before the TransactionalBatch call and released in
a `finally` block.

### 429 Handling for TransactionalBatch

The Azure Cosmos DB SDK's automatic 429 retry does **not** apply to
TransactionalBatch responses. The code implements explicit retry:

```
Attempt 1: Execute TransactionalBatch
  If 429 -> delay 1s, retry
Attempt 2: Execute TransactionalBatch
  If 429 -> delay 2s, retry
Attempt 3: Execute TransactionalBatch
  If 429 -> delay 4s, retry
...up to MaxPatchRetries (5)
```

The delay uses exponential backoff: `500ms * 2^attempt`. Additionally, the SDK's
`CosmosClientOptions` are configured with:
- `MaxRetryAttemptsOnRateLimitedRequests = 9`
- `MaxRetryWaitTimeOnRateLimitedRequests = 30s`

These apply to individual point operations (the fallback `PatchDocumentAsync`
method) but not to TransactionalBatch.

---

## Error Handling and Reliability

### Checkpoint Semantics

The CFP SDK checkpoints (advances the continuation token in the lease document)
**only after the `HandleChangesAsync` delegate returns successfully**. If the
handler throws an exception, the batch is NOT checkpointed and will be retried
on the next poll cycle.

The inline processing path deliberately re-throws exceptions after logging:

```csharp
catch (Exception ex)
{
    _logger.LogError(ex, "Inline processing failed ...");
    // Re-throw so CFP does NOT checkpoint -- batch will be retried
    throw;
}
```

This provides at-least-once delivery semantics: a batch may be processed more
than once on transient failures, but the content_hash dedup ensures idempotency.

### Watcher Lifecycle

If a `SourceWatcher` fails to start (e.g., source container is unreachable),
the error is logged and the watcher is disposed. On the next reconciliation
cycle (30 seconds later), `SourceDiscoveryService` will attempt to start it
again.

### Error Notification

The CFP SDK's `WithErrorNotification` callback logs partition-level errors
without stopping the processor. The SDK continues processing other partitions.

---

## Infrastructure

### AKS Cluster

- GPU node pool: `Standard_NC24ads_A100_v4` for DocGrok (native model inference)
- System node pool: Standard VMs for API, CFP connector, controller, worker
- Service account: `omnivec-api` with workload identity for Azure RBAC

### CosmosDB

| Account         | Type        | Purpose                                  |
|-----------------|-------------|------------------------------------------|
| `omnivec-cosmos`| Serverless  | `metadata` container + `leases-*` containers |
| (user accounts) | Provisioned | Source containers being watched           |

The lease containers live in `omnivec-cosmos` (the OmniVec control plane
account), not in the user's source account. This avoids requiring write access
to user accounts for lease management.

### Container Registry

- ACR: `<internal-acr>`
- Build command: `az acr build --registry <internal-acr> --image omnivec-api:vXX --file api/Dockerfile .`
- No Docker daemon on the build VM; all builds go through ACR.

### Helm Chart

Deployment is managed via Helm at `/home/cdbmvs/omnivec/helm/omnivec/`. The
chart includes:
- `omnivec-api` -- FastAPI control plane (1 replica)
- `omnivec-cfp` -- Change Feed Processor connector (6 replicas)
- `omnivec-controller` -- Pipeline controller (1 replica)
- `omnivec-worker` -- Job processor for queue mode (1-N replicas)

---

## Summary of Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Inline mode over queue mode | Eliminates job creation overhead; 10-50x higher throughput |
| Generation-based reset over lease deletion | No inter-replica coordination needed; deterministic from API state |
| TransactionalBatch over individual patches | ~30x fewer network round-trips per CF batch |
| SemaphoreSlim(3000) global throttle | Prevents RU exhaustion without complex rate limiting |
| Lease containers in omnivec-cosmos | Avoids needing write access to customer source accounts |
| content_hash + embedded_at for dedup | Prevents infinite feedback loop from CF events caused by patches |
| Sub-batch size 100/50 (native/external) | Balances GPU utilization against API rate limits |
| SHA256[:8] generation tag | Short, deterministic, collision-resistant identifier for processor names |
