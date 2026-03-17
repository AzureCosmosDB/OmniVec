# OmniVec Production Architecture v2

## Overview

Production-grade architecture for 100M+ documents with SLA guarantees.
**Design principle:** Crash-resilient - any component can be killed/restarted at any time without data loss.

## Architecture Diagram

```
+-----------------------------------------------------------------------------------+
|                              OMNIVEC CONTROL PLANE                                |
|  +-------------+  +------------------+                                            |
|  | omnivec-api |  | omnivec-web      |                                            |
|  | (REST API)  |  | (UI)             |                                            |
|  +-------------+  +------------------+                                            |
+-----------------------------------------------------------------------------------+
           |                                    |
           v                                    v
+---------------------------+      +---------------------------+
|     BLOB CONTROLLER       |      |   COSMOSDB CONTROLLER     |
|  (manages ALL blob srcs)  |      | (manages ALL cosmos srcs) |
|  - Leader elected         |      | - Leader elected          |
|  - Creates/scales workers |      | - Creates/scales workers  |
|  - Tracks all progress    |      | - Tracks all progress     |
+------------+--------------+      +------------+--------------+
             |                                  |
    +--------+--------+                +--------+--------+
    |                 |                |                 |
    v                 v                v                 v
+--------+       +--------+       +--------+       +--------+
| Backfill        | Live           | Change          | Backfill
| Workers         | Workers        | Feed            | Workers
| (per-source)    | (per-source)   | (per-source)    | (per-source)
| src-A: 1-20     | src-A: 1-10    | src-C: 15       | src-C: 1-10
| src-B: 1-20     | src-B: 1-10    | src-D: 15       | src-D: 1-10
+--------+        +--------+       +--------+        +--------+
```

## Components

### 1. Control Plane

#### omnivec-api
- REST API for all operations
- CRUD for sources, pipelines, models
- Deployment management (create/scale/delete per-source deployments)
- Progress and metrics endpoints

#### omnivec-web
- Static UI served via nginx
- Proxies to omnivec-api

### 2. Controllers (Singleton with Leader Election)

#### Blob Controller (`omnivec-blob-controller`)
- **ONE instance** (with leader election for HA)
- Manages ALL blob sources
- Creates/scales/deletes per-source worker deployments
- Monitors progress across all blob sources
- Coordinates backfill → live transitions
- Crash-resilient: reads state from CosmosDB on startup

#### CosmosDB Controller (`omnivec-cosmosdb-controller`)
- **ONE instance** (with leader election for HA)
- Manages ALL CosmosDB sources
- Creates/scales/deletes per-source changefeed + backfill deployments
- Monitors partition progress and lag
- Coordinates backfill completion
- Crash-resilient: reads state from CosmosDB on startup

### 3. Per-Source Workers (Scalable)

#### Blob Backfill Worker (`blob-backfill-{source_id}`)
- **Scalable (1-N replicas per source)**
- Processes historical/existing blobs
- Paginated enumeration with checkpoint resume
- Stateless: claims work from job queue
- Auto-scales based on pending job count
- **Crash-resilient:** Job state in CosmosDB, checkpoint saved every N items

#### Blob Live Worker (`blob-live-{source_id}`)
- **Scalable (1-M replicas per source)**
- Watches Event Grid notifications via Storage Queue
- Processes only incremental changes (new/modified blobs)
- Lower latency than backfill (priority processing)
- Auto-scales based on queue depth
- **Crash-resilient:** Queue messages have visibility timeout, reprocessed if not completed

#### CosmosDB Changefeed Worker (`cosmos-cf-{source_id}`)
- **Scalable (matches physical partition count)**
- Processes live changes via Change Feed
- Lease-based coordination (no duplicates)
- Inline or queue mode processing
- **Crash-resilient:** Lease container tracks progress per partition

#### CosmosDB Backfill Worker (`cosmos-backfill-{source_id}`)
- **Scalable (1-N replicas per source)**
- Full scan of existing documents
- Paginated with continuation token checkpoints
- Scales to 0 after backfill complete
- **Crash-resilient:** Checkpoint saved after each page

---

## Crash Resilience & Checkpointing

### Design Principles

1. **No in-memory state that matters** - All critical state persisted to CosmosDB
2. **Checkpoint before acknowledge** - Always save checkpoint before marking work complete
3. **Idempotent operations** - Same operation can run multiple times safely
4. **Lease-based coordination** - Distributed locks prevent duplicate processing
5. **Visibility timeout** - Queue messages reappear if not completed

### Crash Scenarios & Recovery

| Scenario | Recovery | Data Loss |
|----------|----------|-----------|
| **Blob Controller dies** | New leader elected, reads state from CosmosDB | None |
| **CosmosDB Controller dies** | New leader elected, reads state from CosmosDB | None |
| **Backfill Worker dies mid-page** | Restart from last checkpoint (max ~100 items re-processed) | None |
| **Backfill Worker dies mid-job** | Job stays PROCESSING, reclaimed after timeout | None |
| **Live Worker dies mid-event** | Queue message reappears after visibility timeout | None |
| **Changefeed Worker dies** | Lease released, another replica takes over partition | None |
| **CosmosDB unavailable** | Retry with exponential backoff | None (delayed) |
| **Entire cluster restart** | All components resume from checkpoints | None |

### Checkpointing Guarantees

#### Blob Backfill Worker
```
For each page of 1000 blobs:
  1. Fetch page with continuation token
  2. For each blob in page:
     a. Check if job exists (dedup)
     b. Create job if new
     c. Every 100 blobs: save checkpoint
  3. After page complete: save checkpoint with next continuation token
  4. If crash between step 2c and 3: restart from last checkpoint
     - Max 100 blobs re-enumerated (idempotent - jobs already exist)
```

#### Blob Live Worker
```
For each queue message:
  1. Receive message (invisible for 5 minutes)
  2. Parse blob event
  3. Create/update job in CosmosDB
  4. Process job (embed, write to destination)
  5. Delete queue message (acknowledge)
  6. If crash before step 5: message reappears after 5 minutes
     - Reprocessing is idempotent (content hash check)
```

#### CosmosDB Changefeed Worker
```
For each change batch:
  1. Receive batch from Change Feed Processor
  2. Process documents (embed, write)
  3. CFP checkpoints automatically after batch handler returns
  4. If crash mid-batch: CFP replays from last checkpoint
     - Idempotent via content_hash (unchanged docs skipped)
```

#### CosmosDB Backfill Worker
```
For each query page:
  1. Query with continuation token
  2. For each document:
     a. Create job if not exists
     b. Every 100 docs: save checkpoint
  3. After page: save checkpoint with continuation
  4. If crash: restart from checkpoint
     - Max 100 docs re-scanned (idempotent)
```

### Distributed Coordination

#### Leader Election (Controllers)
```yaml
# Using Kubernetes Lease API
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: blob-controller-leader
  namespace: omnivec
spec:
  holderIdentity: blob-controller-pod-abc123
  leaseDurationSeconds: 15
  renewTime: "2026-03-07T12:00:00Z"
```

- Controller pods compete for lease
- Current leader renews every 10 seconds
- If leader dies, lease expires after 15 seconds
- New leader elected, reads full state from CosmosDB

#### Worker Coordination (Jobs)
```
Job claiming via etag (optimistic concurrency):
  1. Worker queries PENDING jobs for its source
  2. Attempts to update status to PROCESSING with etag
  3. If etag matches: job claimed successfully
  4. If etag conflict: another worker got it, skip
  5. No locks needed - CosmosDB etag is atomic
```

#### Stuck Job Recovery
```
Background task in controller:
  Every 60 seconds:
    1. Query jobs WHERE status = PROCESSING AND started_at < (now - timeout)
    2. Reset to PENDING (increment retry_count)
    3. If retry_count > max_retries: mark FAILED
```

### Checkpoint Document Schema

```json
{
  "id": "cp-{source_id}-{worker_type}-{location_hash}",
  "doc_type": "checkpoint",
  "source_id": "src-001",
  "worker_type": "backfill",
  "location": "storage1.blob.core.windows.net/documents/legal",

  "state": {
    "continuation_token": "base64...",
    "last_item_processed": "legal/2025/03/contract.pdf",
    "items_processed": 3500000,
    "items_since_checkpoint": 45,
    "page_number": 3500
  },

  "stats": {
    "jobs_created": 3500000,
    "jobs_skipped_existing": 50000,
    "errors": 10
  },

  "worker": {
    "pod_name": "blob-backfill-src-001-abc123",
    "claimed_at": "2026-03-07T12:00:00Z"
  },

  "updated_at": "2026-03-07T12:05:00Z",
  "_etag": "\"00000000-0000-0000-0000-000000000000\""
}
```

### Idempotency Strategies

| Operation | Idempotency Key | Behavior on Duplicate |
|-----------|-----------------|----------------------|
| Create job | `job.id = hash(source_id, blob_path)` | Upsert - no duplicate |
| Process blob | `content_hash` | Skip if hash unchanged |
| Write embedding | `doc_id + embedding_dims` | Overwrite (safe) |
| Queue message | `message_id` | Exactly-once via delete |

---

## Source Configuration

### Blob Source (Multi-Account/Container)

```json
{
  "id": "src-blob-001",
  "name": "enterprise-documents",
  "type": "azure_blob",
  "config": {
    "locations": [
      {
        "account": "storage1.blob.core.windows.net",
        "container": "documents",
        "prefix": "legal/",
        "auth": "managed_identity"
      },
      {
        "account": "storage2.blob.core.windows.net",
        "container": "archives",
        "prefix": "",
        "auth": "managed_identity"
      }
    ],
    "file_patterns": ["*.pdf", "*.docx", "*.txt"],
    "exclude_patterns": ["*.tmp", "~$*"]
  },
  "backfill": {
    "enabled": true,
    "workers_min": 1,
    "workers_max": 20,
    "batch_size": 100,
    "checkpoint_interval": 50
  },
  "live": {
    "enabled": true,
    "workers_min": 1,
    "workers_max": 10,
    "event_grid_topic": "...",
    "storage_queue": "blob-events-src-001"
  },
  "processing": {
    "job_timeout_seconds": 300,
    "max_retries": 3,
    "retry_delay_seconds": 60
  }
}
```

### CosmosDB Source (Multi-Database/Container)

```json
{
  "id": "src-cosmos-001",
  "name": "product-catalog",
  "type": "cosmosdb",
  "config": {
    "locations": [
      {
        "account": "cosmos1.documents.azure.com",
        "database": "products",
        "container": "items",
        "partition_key": "/category"
      },
      {
        "account": "cosmos1.documents.azure.com",
        "database": "products",
        "container": "reviews",
        "partition_key": "/product_id"
      }
    ],
    "content_fields": ["description", "specifications", "review_text"],
    "id_field": "id"
  },
  "backfill": {
    "enabled": true,
    "workers_min": 1,
    "workers_max": 10,
    "page_size": 1000
  },
  "changefeed": {
    "enabled": true,
    "workers": 15,
    "processing_mode": "inline",
    "start_from": "beginning"
  },
  "processing": {
    "job_timeout_seconds": 120,
    "max_retries": 3
  }
}
```

---

## Progress Tracking

### Source Progress Document

```json
{
  "id": "progress-{source_id}",
  "doc_type": "source_progress",
  "source_id": "src-blob-001",
  "source_name": "enterprise-documents",

  "backfill": {
    "status": "in_progress",
    "started_at": "2026-03-07T10:00:00Z",
    "locations": {
      "storage1/documents/legal": {
        "total_blobs_estimated": 5000000,
        "blobs_enumerated": 3500000,
        "jobs_created": 3500000,
        "jobs_completed": 3200000,
        "jobs_failed": 100,
        "jobs_pending": 299900,
        "continuation_token": "...",
        "last_blob": "legal/2025/contract-123.pdf",
        "percent_complete": 64,
        "estimated_completion": "2026-03-08T02:00:00Z"
      },
      "storage2/archives": {
        "total_blobs_estimated": 10000000,
        "blobs_enumerated": 1000000,
        "percent_complete": 10
      }
    },
    "totals": {
      "blobs_enumerated": 4500000,
      "jobs_completed": 3200000,
      "jobs_pending": 1299900,
      "percent_complete": 32
    }
  },

  "live": {
    "status": "running",
    "events_received": 15000,
    "events_processed": 14500,
    "events_pending": 500,
    "lag_seconds": 12,
    "throughput_per_sec": 25
  },

  "workers": {
    "backfill": {
      "desired": 10,
      "ready": 10,
      "processing": 8
    },
    "live": {
      "desired": 3,
      "ready": 3,
      "processing": 2
    }
  },

  "health": {
    "status": "healthy",
    "last_check": "2026-03-07T12:00:00Z",
    "issues": []
  },

  "updated_at": "2026-03-07T12:00:05Z"
}
```

### Pipeline Progress Document

```json
{
  "id": "progress-{pipeline_id}",
  "doc_type": "pipeline_progress",
  "pipeline_id": "pip-001",
  "pipeline_name": "document-embeddings",

  "sources": {
    "src-blob-001": {
      "backfill_percent": 32,
      "live_lag_seconds": 12,
      "jobs_pending": 1299900,
      "jobs_completed_today": 500000,
      "throughput_per_sec": 150
    },
    "src-cosmos-001": {
      "backfill_percent": 100,
      "changefeed_lag_seconds": 2,
      "docs_processed_today": 50000
    }
  },

  "totals": {
    "documents_indexed": 85000000,
    "documents_pending": 15000000,
    "estimated_completion": "2026-03-10T00:00:00Z",
    "overall_percent": 85
  },

  "sla": {
    "target_lag_seconds": 60,
    "current_lag_seconds": 12,
    "sla_met": true,
    "uptime_percent_30d": 99.95
  }
}
```

---

## Kubernetes Deployments

### Controllers (Singleton with Leader Election)

```yaml
# Blob Controller - ONE for all blob sources
apiVersion: apps/v1
kind: Deployment
metadata:
  name: omnivec-blob-controller
  namespace: omnivec
spec:
  replicas: 2  # 2 for HA - only 1 active (leader)
  selector:
    matchLabels:
      app: omnivec-blob-controller
  template:
    metadata:
      labels:
        app: omnivec-blob-controller
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: omnivec-api
      containers:
        - name: controller
          image: omnivecregistry.azurecr.io/omnivec-api:v1
          command: ["python", "-m", "blob_controller"]
          env:
            - name: LEASE_NAME
              value: "blob-controller-leader"
            - name: LEASE_NAMESPACE
              value: "omnivec"
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "500m"
---
# CosmosDB Controller - ONE for all cosmosdb sources
apiVersion: apps/v1
kind: Deployment
metadata:
  name: omnivec-cosmosdb-controller
  namespace: omnivec
spec:
  replicas: 2  # 2 for HA - only 1 active (leader)
  selector:
    matchLabels:
      app: omnivec-cosmosdb-controller
  template:
    metadata:
      labels:
        app: omnivec-cosmosdb-controller
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: omnivec-api
      containers:
        - name: controller
          image: omnivecregistry.azurecr.io/omnivec-api:v1
          command: ["python", "-m", "cosmosdb_controller"]
          env:
            - name: LEASE_NAME
              value: "cosmosdb-controller-leader"
            - name: LEASE_NAMESPACE
              value: "omnivec"
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "500m"
```

### Per-Source Workers (Created Dynamically by Controllers)

```yaml
# blob-backfill-{source_id} - Created by Blob Controller
apiVersion: apps/v1
kind: Deployment
metadata:
  name: blob-backfill-src-001
  namespace: omnivec
  labels:
    app: blob-backfill
    source: src-001
    managed-by: omnivec-blob-controller
spec:
  replicas: 1
  selector:
    matchLabels:
      app: blob-backfill
      source: src-001
  template:
    metadata:
      labels:
        app: blob-backfill
        source: src-001
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: omnivec-api
      containers:
        - name: worker
          image: omnivecregistry.azurecr.io/omnivec-api:v1
          command: ["python", "-m", "blob_backfill_worker"]
          env:
            - name: SOURCE_ID
              value: "src-001"
            - name: CHECKPOINT_INTERVAL
              value: "100"
            - name: BATCH_SIZE
              value: "50"
          resources:
            requests:
              memory: "1Gi"
              cpu: "500m"
            limits:
              memory: "2Gi"
              cpu: "2"
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: blob-backfill-src-001
  namespace: omnivec
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: blob-backfill-src-001
  minReplicas: 1
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
# blob-live-{source_id} - Created by Blob Controller
apiVersion: apps/v1
kind: Deployment
metadata:
  name: blob-live-src-001
  namespace: omnivec
  labels:
    app: blob-live
    source: src-001
    managed-by: omnivec-blob-controller
spec:
  replicas: 1
  selector:
    matchLabels:
      app: blob-live
      source: src-001
  template:
    metadata:
      labels:
        app: blob-live
        source: src-001
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: omnivec-api
      containers:
        - name: worker
          image: omnivecregistry.azurecr.io/omnivec-api:v1
          command: ["python", "-m", "blob_live_worker"]
          env:
            - name: SOURCE_ID
              value: "src-001"
            - name: QUEUE_NAME
              value: "blob-events-src-001"
            - name: VISIBILITY_TIMEOUT
              value: "300"
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: blob-live-src-001
  namespace: omnivec
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: blob-live-src-001
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### Per-Source CosmosDB Workers (Created Dynamically)

```yaml
# cosmos-cf-{source_id} - Changefeed processors
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cosmos-cf-src-002
  namespace: omnivec
  labels:
    app: cosmos-cf
    source: src-002
    managed-by: omnivec-cosmosdb-controller
spec:
  replicas: 15  # Match physical partition count
  selector:
    matchLabels:
      app: cosmos-cf
      source: src-002
  template:
    metadata:
      labels:
        app: cosmos-cf
        source: src-002
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: omnivec-api
      containers:
        - name: changefeed
          image: omnivecregistry.azurecr.io/omnivec-changefeed:v1
          env:
            - name: SOURCE_ID
              value: "src-002"
            - name: PROCESSING_MODE
              value: "inline"
---
# cosmos-backfill-{source_id}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cosmos-backfill-src-002
  namespace: omnivec
  labels:
    app: cosmos-backfill
    source: src-002
    managed-by: omnivec-cosmosdb-controller
spec:
  replicas: 1  # Scaled to 0 after backfill complete
  selector:
    matchLabels:
      app: cosmos-backfill
      source: src-002
  template:
    spec:
      containers:
        - name: worker
          image: omnivecregistry.azurecr.io/omnivec-api:v1
          command: ["python", "-m", "cosmosdb_backfill_worker"]
          env:
            - name: SOURCE_ID
              value: "src-002"
            - name: PAGE_SIZE
              value: "1000"
            - name: CHECKPOINT_INTERVAL
              value: "100"
```

---

## Checkpointing Strategy

### Blob Backfill Checkpoint

```json
{
  "id": "checkpoint-backfill-{source_id}-{location_hash}",
  "doc_type": "checkpoint",
  "source_id": "src-001",
  "location": "storage1/documents/legal",
  "checkpoint_type": "blob_backfill",

  "enumeration": {
    "continuation_token": "base64...",
    "last_blob_name": "legal/2025/03/contract-456.pdf",
    "blobs_enumerated": 3500000,
    "pages_processed": 3500
  },

  "processing": {
    "last_job_id": "job-abc123",
    "jobs_created": 3500000,
    "jobs_completed": 3200000
  },

  "worker_id": "blob-backfill-src-001-abc123",
  "updated_at": "2026-03-07T12:00:00Z",
  "version": 42  # Optimistic concurrency
}
```

### Blob Live Checkpoint

```json
{
  "id": "checkpoint-live-{source_id}",
  "doc_type": "checkpoint",
  "source_id": "src-001",
  "checkpoint_type": "blob_live",

  "queue": {
    "last_message_id": "msg-xyz789",
    "messages_processed": 15000,
    "last_processed_at": "2026-03-07T12:00:00Z"
  },

  "updated_at": "2026-03-07T12:00:00Z"
}
```

### CosmosDB Changefeed Checkpoint

Managed by CosmosDB SDK via lease container:
- `leases-{source_id}` container
- One lease document per partition
- Automatic failover on pod death

### CosmosDB Backfill Checkpoint

```json
{
  "id": "checkpoint-backfill-{source_id}-{container_hash}",
  "doc_type": "checkpoint",
  "source_id": "src-002",
  "location": "cosmos1/products/items",
  "checkpoint_type": "cosmos_backfill",

  "query": {
    "continuation_token": "base64...",
    "partition_key_ranges_completed": ["0", "1", "2"],
    "current_partition_range": "3",
    "docs_scanned": 5000000
  },

  "updated_at": "2026-03-07T12:00:00Z"
}
```

---

## SLA Monitoring

### Metrics Exposed (Prometheus)

```
# Backfill progress
omnivec_backfill_percent{source="src-001", pipeline="pip-001"} 32
omnivec_backfill_jobs_pending{source="src-001"} 1299900
omnivec_backfill_throughput_per_sec{source="src-001"} 150

# Live lag
omnivec_live_lag_seconds{source="src-001"} 12
omnivec_live_events_pending{source="src-001"} 500

# Job processing
omnivec_jobs_completed_total{source="src-001", status="success"} 3200000
omnivec_jobs_completed_total{source="src-001", status="failed"} 100
omnivec_job_processing_seconds{source="src-001", quantile="0.99"} 2.5

# Worker health
omnivec_workers_ready{source="src-001", type="backfill"} 10
omnivec_workers_ready{source="src-001", type="live"} 3

# SLA
omnivec_sla_target_lag_seconds{pipeline="pip-001"} 60
omnivec_sla_current_lag_seconds{pipeline="pip-001"} 12
omnivec_sla_compliance{pipeline="pip-001"} 1  # 1 = met, 0 = breached
```

### Alerting Rules

```yaml
groups:
  - name: omnivec-sla
    rules:
      - alert: OmniVecLagBreached
        expr: omnivec_live_lag_seconds > omnivec_sla_target_lag_seconds
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "SLA breached for {{ $labels.source }}"

      - alert: OmniVecBackfillStalled
        expr: rate(omnivec_backfill_jobs_pending[10m]) >= 0
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "Backfill not making progress for {{ $labels.source }}"

      - alert: OmniVecWorkersDegraded
        expr: omnivec_workers_ready < omnivec_workers_desired * 0.5
        for: 5m
        labels:
          severity: warning
```

---

## API Endpoints

### Source Management

```
POST   /api/sources                    # Create source
GET    /api/sources                    # List sources
GET    /api/sources/{id}               # Get source details
PUT    /api/sources/{id}               # Update source config
DELETE /api/sources/{id}               # Delete source

POST   /api/sources/{id}/start         # Start processing (creates deployments)
POST   /api/sources/{id}/stop          # Stop processing (scales to 0)
POST   /api/sources/{id}/pause         # Pause (keeps state, stops workers)
POST   /api/sources/{id}/resume        # Resume from checkpoint
```

### Progress & Metrics

```
GET    /api/sources/{id}/progress      # Get source progress
GET    /api/pipelines/{id}/progress    # Get pipeline progress
GET    /api/progress/global            # Get system-wide progress

GET    /api/sources/{id}/lag           # Get real-time lag
GET    /api/sources/{id}/throughput    # Get processing rate
GET    /api/sources/{id}/eta           # Get estimated completion time
```

### Worker Management

```
GET    /api/sources/{id}/workers                    # List workers
POST   /api/sources/{id}/workers/backfill/scale     # Scale backfill workers
POST   /api/sources/{id}/workers/live/scale         # Scale live workers
POST   /api/sources/{id}/workers/restart            # Rolling restart
```

### Checkpoints

```
GET    /api/sources/{id}/checkpoints   # List checkpoints
POST   /api/sources/{id}/checkpoints/reset          # Reset to beginning
POST   /api/sources/{id}/checkpoints/set            # Set specific checkpoint
```

---

## Implementation Files

### New Files to Create

```
api/
  blob_controller.py          # Single controller for ALL blob sources
  blob_backfill_worker.py     # Per-source backfill worker
  blob_live_worker.py         # Per-source live event worker
  cosmosdb_controller.py      # Single controller for ALL CosmosDB sources
  cosmosdb_backfill_worker.py # Per-source CosmosDB backfill worker
  checkpoint_manager.py       # Checkpoint CRUD with atomic updates
  progress_tracker.py         # Progress aggregation and reporting
  leader_election.py          # Kubernetes Lease-based leader election
  deployment_manager.py       # Create/scale/delete per-source deployments
```

### Modified Files

```
api/
  api.py                      # Add progress, checkpoint, controller endpoints
  models.py                   # Update Source model for multi-location config
  store.py                    # Add batch operations, distributed locks
  controller.py               # Deprecate - split into blob/cosmosdb controllers

connectors/ingestion/dotnet/
  Services/SourceWatcher.cs   # Update for per-source deployment model
```

### Delete/Deprecate

```
api/
  blob_enumerator.py          # Replaced by blob_backfill_worker.py
  source_worker.py            # Replaced by blob_backfill_worker.py + blob_live_worker.py
  controller.py               # Replaced by blob_controller.py + cosmosdb_controller.py
```

---

## Migration Path

### Phase 1: Multi-Location Support
1. Update Source model to support `locations[]` array
2. Update blob_enumerator to iterate multiple locations
3. Add location-level checkpointing

### Phase 2: Backfill/Live Split
1. Create blob_backfill_worker.py
2. Create blob_live_worker.py with Event Grid integration
3. Create blob_controller.py to manage both

### Phase 3: Per-Source Deployments
1. Update API to create per-source K8s deployments
2. Implement blob_controller deployment management
3. Add progress tracking per source

### Phase 4: CosmosDB Enhancements
1. Create cosmos_controller.py
2. Create cosmos_backfill_worker.py
3. Update changefeed processor for per-source deployment

### Phase 5: SLA & Monitoring
1. Add Prometheus metrics
2. Implement progress aggregation
3. Add alerting rules
4. Build SLA dashboard

---

## Capacity Planning

### For 100M Documents

| Component | Replicas | Memory | CPU | Notes |
|-----------|----------|--------|-----|-------|
| omnivec-api | 3 | 1Gi | 1 | Load balanced |
| omnivec-master | 1 | 512Mi | 0.5 | Leader elected |
| blob-ctrl-* | 1/source | 256Mi | 0.25 | Lightweight |
| blob-backfill-* | 1-20/source | 2Gi | 2 | Auto-scaled |
| blob-live-* | 1-10/source | 1Gi | 1 | Auto-scaled |
| cosmos-ctrl-* | 1/source | 256Mi | 0.25 | Lightweight |
| cosmos-cf-* | 15/source | 512Mi | 0.5 | Match partitions |
| cosmos-backfill-* | 1-10/source | 2Gi | 2 | Temporary |

### Estimated Throughput

- **Backfill:** 500K-1M docs/hour per source (with 10 workers)
- **Live:** 10K-50K events/hour per source (depends on rate)
- **Total capacity:** 100M docs in 4-8 days (10 sources, 10 workers each)
