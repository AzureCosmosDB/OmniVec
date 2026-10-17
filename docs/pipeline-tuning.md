# Pipeline tuning and delivery guarantees

[README](../README.md) | [User guide](user-guide.md) | [Architecture](architecture.md)

Use this reference after a pipeline works end to end. Start with a small,
representative workload and change one setting at a time. More replicas, larger
messages, or larger batches do not by themselves establish higher throughput.

## Choose the right control

| Control | What it changes | What it does not change |
|---------|-----------------|-------------------------|
| `embed_batch_size` | Documents in a model request, subject to model/token limits | Documents packed into a Service Bus message |
| `resource_policy.queue_documents_per_message` | Maximum compatible documents in one broker message | Model context window or total payload bytes |
| Worker concurrency and replicas | Available processing slots | Model quota, destination capacity, or source parallelism |
| `content_strategy` | One vector per input (`truncate`) or supported chunk processing | Which source/destination combinations are supported |
| Pipeline lanes | Opt-in broker separation by pipeline | Physical tenant isolation or independent model/destination capacity |

See the [compatibility table](user-guide.md#source-and-destination-compatibility)
before choosing a mode. Capacity settings apply to future work; queued messages
can retain older configuration snapshots.

## Embedding limits and resilience

Model registrations expose `max_batch_size`, `context_window_tokens`, and
`max_batch_tokens`. Pipeline batch settings may reduce, never increase, these
ceilings. Workers reserve 5% token headroom, re-estimate truncated inputs, and
enforce per-input and total-request budgets before batching.

Estimates are not exact tokenizer counts. For queued text batches, confirmed
backend context-limit errors trigger bounded batch splitting and, when
`TruncateOversized` is enabled, up to six progressively smaller singleton
retries. Other input errors do not trigger truncation. Unprocessable inputs are
explicitly dead-lettered; transient model outages are not treated as bad input.

Model changes refresh pipeline snapshots independently. Conflicting snapshots
are reported in `pending_pipeline_snapshots`; subsequent pipeline reads serve
current limits and retry persisting them. Already queued messages retain their
original snapshots, with router-side limits providing a defensive check.

### Worker budgets

Environment settings use the `Worker__` prefix:

| Setting | Default | Scope |
|---------|--------:|-------|
| `MaxLockRenewalMinutes` | 10 | Maximum scheduler-admission wait |
| `MaxProcessingMinutes` | 8 | Processing after admission, including retries |
| `DocGrokRequestTimeoutSeconds` | 330 | Per HTTP request; also capped by the smaller configured admission/processing budget |
| `ReceivePumpCount` | 4 | Independent receiver links per worker replica |
| `MaxConcurrentCalls` | 10 | Global text-processing concurrency |
| `BlobConcurrency` | 32 | Global Blob/SharePoint processing concurrency |
| `SharePointConcurrency` | 1 | Additional cap for memory-heavy buffered SharePoint files |
| `PrefetchCount` | 0 | Broker messages prefetched per receiver |

Locks are renewed during admission and processing. Total lock ownership is
bounded by the **sum** of admission and processing budgets; unfinished deliveries
are abandoned when a deadline expires. A request may be cancelled earlier by
the remaining processing budget. Retries do not reset that budget, and increasing
the HTTP timeout does not extend the DocGrok router's execution ceiling.

These are cooperative cancellation budgets, not forced thread termination.
Size memory before raising file-processing concurrency.

### Metrics and capacity estimates

Workers enqueue metrics into a bounded, nonblocking transport; telemetry I/O
does not run on the processing path. Background delivery uses stable
deduplication keys and bounded retries. The API asynchronously exports batch
events to Application Insights without writing metrics to Cosmos metadata.
Queue saturation and exhausted delivery failures are logged, not durably queued.
Historical totals use deduplicated batch events; fallback counters are local to
one API process since restart, not cluster-wide history.

Capacity estimates use the lower percentile of recent completed batches and
70% utilization, capped at the smaller of pipeline and deployed worker
concurrency. If worker configuration cannot be discovered, aggregate capacity
is unknown. Estimates describe observed contention and topology, not guaranteed
linear scaling or automatic replica control.

## Documents per queue message

In the console, open **Pipeline > Capacity > Documents per queue message**.
`resource_policy.queue_documents_per_message` accepts 1-1000 and defaults to 1.
For example, `100` packs up to 100 compatible documents into one broker message.
It is independent of `embed_batch_size`.

`Worker__MaxReceiveBatchSize` counts broker messages, not expanded documents.
Receiving one envelope does not reduce the model batch to one document. Model
requests still obey pipeline count, model count, and token limits.

The live `embed_batch_size` knob accepts 1-4096. Registered models may impose
smaller limits. Synthetic mock runs have no real model: their mock count cap
follows the pipeline knob without changing registered model metadata or token
limits. A 512-document request still needs 512 compatible documents available
and sufficient token budget.

### Packing limits

The Capacity panel also exposes:

| Resource-policy field | Default | Limit |
|-----------------------|--------:|-------|
| `queue_max_envelope_bytes` | 229376 (224 KiB) | Maximum 983040 (960 KiB) |
| `queue_send_batch_max_bytes` | 245760 (240 KiB) | Maximum 1048576 (1 MiB) |
| `queue_publish_batch_size` | 200 | 1-1000 |

The send-batch cap must reserve at least 16 KiB above the envelope cap. Defaults
remain compatible with 256 KiB brokers. For approximately 500 one-KiB documents,
an illustrative policy is:

```json
{
  "queue_documents_per_message": 500,
  "queue_max_envelope_bytes": 614400,
  "queue_send_batch_max_bytes": 655360,
  "queue_publish_batch_size": 500
}
```

The broker must support the chosen sizes. Serialized UTF-8 size, source-page
boundaries, and compatibility can reduce the actual count. This is a maximum,
not a minimum or a promise that each message contains exactly 500 documents.
Oversized individual documents retain the legacy single-message path and broker
size checks. Send failures are explicit and do not advance source checkpoints.

Queue-mode .NET ingestion, OneLake Iceberg scans, and synthetic runs honor these
settings. Python publishers share `omnivec_queue`; API and OneLake images include
it. Build the OneLake image from the repository root. Local Python invocations
also need that root on `PYTHONPATH`.

Publishers flush at source-page/publish-call boundaries. Synthetic pacing flushes
roughly every 100 ms rather than waiting indefinitely for a full batch. Synthetic
publishers refresh live policy within five seconds, including active runs.
Ingestion refreshes at its next control-plane poll. Once compatible code is
deployed, these policy changes need no image build or rollout; already queued
messages keep their original packing.

The separately managed Fabric Spark Delta CDF job also supports packing. Update
its pipeline plan: a static `--pipeline-config-base64` plan requires restarting
the job; `--pipeline-config-path` supports the hot-reload behavior described in
the [OneLake guide](onelake-iceberg.md). A microbatch checkpoints only after all
its sends succeed.

### Settlement and failure handling

Workers accept legacy messages and version-1 envelopes. Documents retain their
IDs, versions, model limits, and document-based telemetry. A parent delivery is
renewed and acknowledged once, after every document succeeds or has a confirmed
durable failure handoff.

A retryable failure retries the whole envelope, including successful documents.
Destination writes must therefore tolerate replay. Permanent failures are
republished individually with a dead-letter marker, then routed to the
subscription DLQ without re-embedding. Workers need Service Bus **Send and
Listen** permissions; the default deployment grants Data Owner.

Failed handoff or acknowledgment leaves the original delivery recoverable.
At-least-once replay can duplicate work, telemetry, and dead-letter handoffs.
Envelope batching is **not exactly-once delivery** and does not add revision
fencing to connectors that lack it.

### Rollout checklist

1. Deploy all envelope-aware workers before enabling counts above 1.
2. Keep prefetch at 0 and review receive-batch limits. For example, 512 prefetched
   envelopes of 100 documents represent 51,200 documents per receiver.
3. Retune autoscaling and backpressure thresholds: they observe broker-message
   counts, not document counts.
4. Reconcile backlog and DLQ behavior before increasing production traffic.
   Do not revert to old workers until queued envelopes have drained.

Pipeline admission bounds expanded envelopes; raw prefetch buffers are still
sized in messages. Packing reduces message and settlement counts, not payload
bytes, model computation, or destination-write work. It does not guarantee a
particular documents-per-second rate.

## Opt-in broker pipeline lanes: stage 1

**Disabled by default.** The Bicep `enablePipelineLanes=true` parameter only
provisions `embeddings-lanes-v1`, marked with topic UserMetadata
`omnivec-pipeline-lanes-v1`, with **no catch-all subscription**. The existing
`embeddings` topic, `worker` subscription, rules, and messages remain untouched.
The new topic uses ARM API `2026-01-01` for `userMetadata`; the namespace API is
unchanged.

This separates broker delivery within a shared namespace. Workers, models,
destinations, and infrastructure remain shared. It is not physical tenant
isolation, a complete resilience guarantee, or a throughput guarantee.

### Routing and ownership

Each lane is `p-` plus the first 32 lowercase hex characters of
SHA256(UTF8(exact pipeline ID)). The exact ID is the broker Subject (Label in
Python's correlation filter). A lane has one subject-only correlation rule,
created atomically with its subscription.

Metadata contains `routing_version=1`, `pipeline_id`, `resource_weight`,
`max_concurrency_per_worker`, `resource_priority`, and `workload_class`.
Publishers provision and validate before publishing; workers validate during
discovery. Unmarked/legacy topics, foreign/broad/default rules, forwarding,
sessions, and incompatible metadata fail closed. No automatic repair or deletion
occurs. Resource-policy changes require an explicit reconciled metadata migration.
Metadata is an ownership convention, not a security boundary against namespace
owners.

### Enablement and limits

Helm settings under `azure.serviceBus.pipelineLanes` are independent:
`workerEnabled`, `producersEnabled` (both false), and explicit `topicName`
(`embeddings-lanes-v1`). They cover .NET queue ingestion, the .NET worker, and
the OneLake Iceberg watcher. Service Bus must also be configured.

Environment-only equivalents:

| Component | Enabled | Topic |
|-----------|---------|-------|
| .NET worker | `Worker__PipelineLanesEnabled` | `Worker__PipelineLanesTopicName` |
| .NET ingestion | `ChangeFeed__PipelineLanesEnabled` | `ChangeFeed__PipelineLanesTopicName` |
| OneLake watcher | `ONELAKE_ICEBERG_PIPELINE_LANES_ENABLED` | `ONELAKE_ICEBERG_PIPELINE_LANES_TOPIC` |

Lane workers require fixed replicas. Disable `dotnetWorker.autoscaling.enabled`;
Helm rejects HPA and KEDA for stage 1. Do not point KEDA at the legacy subscription
or assume one fixed subscription on the new topic.

The shared fair scheduler reserves global and pipeline capacity before receive,
with zero prefetch, bounded batches, and one bounded receiver/pending receive
task per lane (maximum 128 lanes). Empty receives release capacity before a short
delay. A received body ID and Subject must match the admitted lane; mismatches
are explicitly dead-lettered as `InvalidLaneRouting`.

Administration validation is serialized and cached for at most 30 seconds.
Discovery failures are visible and stop lane workers. Receivers are disposed on
removal/shutdown. Shared capacity and idle-lane polling overhead still apply.

.NET publishers check backlog per pipeline and attempt healthy groups before
reporting blocked/failed groups. Any failed publication retains the source
checkpoint. Without a durable per-pipeline outbox, a shared source page/commit
can still stall subsequent healthy work; already sent groups can replay.

OneLake lane mode instead uses independent per-pipeline `--lanes-v1` checkpoint
plans, continues healthy plans after logging failed plans, and sets broker
MessageId/Subject. Activation **fails for CDF-enabled OneLake sources**: the
Fabric Spark CDF script is not lane-aware in stage 1.

### Migration and rollback

1. Provision and inspect the marked topic and exact lane rules/metadata.
2. Keep legacy workers to drain the old topic; start separate lane workers with
   fixed replicas. Do not replace all legacy workers at once.
3. Switch producers only after lane validation and worker readiness.
4. Reconcile source checkpoints, both backlogs, and DLQs. OneLake's new lane
   checkpoint scope starts with a replay. Remove legacy workers only afterward.

Do not delete subscriptions/messages or mutate existing catch-all rules as a
shortcut. Rollback also requires backlog reconciliation.

Local regression commands are:

```powershell
dotnet run --project tests\sharepoint\SharePoint.Regression.csproj --no-restore
python -m pytest tests\unit\test_pipeline_lanes.py tests\unit\test_onelake_iceberg_helpers.py -q
```

These use deterministic doubles, not live broker proof. Live two-pipeline
flood/outage/lock/settlement/migration checks remain necessary before activation.
Durable retry parking, persistent circuit breakers, and per-pipeline producer
outboxes are not provided by stage 1.

## Cosmos text chunking (queue mode)

For Cosmos field-content sources writing to a **separate Cosmos vector
container**, select `content_strategy: "chunk"`. Example pipeline fields:

```json
{
  "content_strategy": "chunk",
  "chunk_config": {
    "chunk_size": 1000,
    "chunk_overlap": 200,
    "chunk_unit": "chars",
    "store_text": true,
    "text_field": "text",
    "doc_id_pattern": "{source}-chunk-{chunk}"
  }
}
```

The .NET worker embeds overlapping chunks without first truncating the source.
`chars` counts Unicode codepoints and prefers paragraph/sentence boundaries.
`tokens` counts **whitespace words**, not model/BPE tokens. Oversized chunks
fail explicitly; reduce their size to fit model and worker limits.

This path rejects inline mode, URL content, attachments, mixed source types, and
non-Cosmos destinations. SharePoint and file extraction have their own contracts;
these restrictions describe Cosmos field-content chunking.

Every vector carries `chunk_index`, `chunk_count`, `source_id`, `source_ref`,
`pipeline_id`, and `chunk_source_partition`, even when optional metadata is
disabled. Only `store_text: true` persists chunk text at `text_field`; raw source
fields are not copied to every chunk. Chunk settings take precedence over
single-document `store_content`/`content_field`.

Templates support `source`, `source_ref`, `source_hash`, `chunk` (zero-padded),
`pipeline`, and `pipeline_hash`. They must include `{chunk}` and render valid
Cosmos IDs. A mandatory SHA-256 namespace prefix isolates the pipeline, source
registration, source partition, and document. The ID does not replace the original
`source_ref`. `/id` and supported single top-level non-metadata partition keys
are allowed.

By default, replacement chunks are written before obsolete chunks for that exact
source and pipeline are removed (`cleanup_order: "insert-first"`).
`cleanup_order: "delete-first"` removes the old set first and can leave a
temporary gap if subsequent work fails. Empty text removes the previous set. Failed embedding,
writing, or cleanup is retried or dead-lettered, not acknowledged as success.
Replacement across `/id` partitions is **not atomic or revision-fenced**:
overlapping/out-of-order updates can race. This differs from SharePoint's
revision-fenced replacement.

Create a new pipeline to change strategy, text-storage opt-in/field, or chunk ID
template. Size, overlap (including zero), unit, and cleanup order can change for future
processing; they do not automatically reprocess existing vectors.

The CLI exposes `--chunk-size`, `--chunk-overlap`, `--chunk-unit`, `--store-text`,
`--text-field`, and `--chunk-doc-id-pattern`. For chunk pipelines,
`--doc-id-pattern` also supplies the chunk template; the explicit chunk flag
takes precedence. Update payloads preserve omitted settings and explicit
zero/false values, but the API still enforces immutable identity/storage fields.

Local coverage uses `dotnet run --project tests\sharepoint -c Release`,
`python -m pytest tests\unit\test_cosmos_chunking.py tests\unit\test_pipeline_ui_flows.py -q`,
and `go test ./...` from `cli`. JavaScript tests use Node and DOM stubs, not a
browser. The opt-in [live probe](../scripts/e2e-cosmos-chunking.py) requires an
explicitly authorized deployment and named synthetic fixtures; it is not part
of the getting-started workflow.
