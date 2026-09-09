# Connector processing and recovery

## Worker budgets and queue ownership

The .NET worker renews locks for every received message, including messages
waiting for a concurrency slot. It stops renewing settled messages. Prefetch
is disabled because prefetched message locks age before processing can renew
them. Renewal failure cancels the batch; unfinished deliveries are abandoned
or become available again when their broker locks expire.

Configuration uses the `Worker__` environment-variable prefix:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `MaxProcessingMinutes` | 8 | Cooperative received-batch processing budget, including retries and slot waits |
| `MaxLockRenewalMinutes` | 10 | Upper limit on lock renewal; effective batch budget is the smaller of these two settings |
| `DocGrokRequestTimeoutSeconds` | 330 | Timeout of each DocGrok HTTP request, capped by the effective received-batch budget |
| `MaxConcurrentCalls` | 10 | Receive loops and global text-request concurrency |
| `BlobConcurrency` | 32 | Global concurrency for Blob/SharePoint document processing, not multiplied by receive loops |
| `SharePointConcurrency` | 1 | Additional cap on memory-heavy buffered SharePoint files; raise only after sizing worker memory |

Increase both processing/renewal budgets, and the HTTP timeout if needed, for
legitimately long document extraction. These are cancellation budgets, not
forced thread termination. The SDKs and destination operations must honor
cancellation. A hung native dependency still needs process-level recovery.
The shared DocGrok client defaults to 330 seconds: the router's 300-second
execution ceiling plus 30 seconds of transport grace. This also applies to text
requests. `Worker__DocGrokRequestTimeoutSeconds` remains explicitly configurable;
nonpositive timeouts/budgets are rejected. A request cannot exceed the configured
effective batch budget, and the batch's remaining lifetime can cancel it earlier.
Retries do not reset that lifetime. Increasing the connector timeout does not
extend the router's execution ceiling.

Different pipelines in a received batch progress independently. A document
request waiting for the global concurrency gate does not consume a text slot.
Deliberately idle workers continue heartbeating. Configured workers become
ready after a successful receive, including an empty receive—not merely after
constructing a Service Bus client.

## Failure handling

DocGrok transport failures, timeouts, HTTP 408/429 and 5xx are retried at most
four times per request. Backoff is bounded, including server `Retry-After`.
Responses are disposed on every attempt. Exhaustion returns control to queue
delivery retry; it does not bisect/truncate/dead-letter an entire batch as if
an unavailable model were bad input.

Only input-specific 400/413/415/422 errors qualify for input isolation.
Unsupported writers/deletes are explicit failures, not successful no-ops.
SQL/Postgres destinations propagate exhausted/permanent write errors; Cosmos
destination retries are bounded and do not repeatedly retry permanent errors.
Incomplete or empty embeddings are errors unless the processor explicitly
reports an intentional skip.

Service Bus's configured maximum delivery count remains the final retry/DLQ
policy. Monitor delivery count, DLQ reasons, worker error logs and destination
lag. Repair the dependency/configuration before redriving DLQ messages.
Confirmed explicit worker dead-lettering logs the reason and reports
`processed=0, failed=1` through existing inline metrics with a message-ID
deduplication key. Metrics delivery is best-effort. Broker-automatic
dead-lettering after delivery exhaustion is not observed by this reporting
path; monitor the Service Bus DLQ directly. The legacy `/jobs` metadata retry
endpoint is not a Service Bus DLQ redrive operation.
Exhausted HTTP timeouts are retryable failures with error logs, not shutdown
cancellation or indexed success. They are abandoned for redelivery rather than
counted as terminal DLQ failures. The local mixed-delivery regression exercises
permanent failure, timeout, failure telemetry and continued healthy-message
progress through the real worker receive loop.
Inline pipelines have no queue DLQ: errors retain source checkpoints and are
logged for operator action. These connectors do not add a new API error-state
contract.

## Source checkpoints and backpressure

- All queue publications check backpressure. Failure to read subscription
  runtime properties is **not** treated as an empty queue. Provision the
  identity's required Service Bus management access; failure preserves source
  progress instead of flooding a blocked queue.
- Blob continuation/version markers advance only after all pipeline targets
  publish successfully. Live poll watermarks use the scan start, not its end,
  so concurrent modifications are not missed.
- PostgreSQL timestamp/PK bookmarks commit after successful processing;
  comparison and ordering use the same text PK ordering. Tables without a
  non-nullable tracking timestamp are paginated through complete repeat scans
  rather than reading only the first page forever. Nullable tracking columns
  are excluded during schema detection, so rows with null timestamps cannot
  block the initial page or disappear from incremental polling.
- SQL initial scans preserve native key types and include negative/zero keys.
  Failed pages do not advance bookmarks. A CDC retention gap triggers a full
  scan instead of indefinitely retrying an expired LSN.
- Databricks starts with a version-pinned snapshot, then processes complete
  commits rather than dropping rows past `LIMIT 1000`. Snapshots/commits use
  `EXTERNAL_LINKS` JSON results, avoiding the 25 MiB total INLINE result limit.
  External chunks are streamed into bounded publication pages; workspace
  authorization is never sent to presigned download URLs. Only after all chunks
  and row counts are verified and every page is published does the version
  advance. Failed downloads/publication replay the pinned query on retry
  (duplicates are possible). External-result access and outbound storage
  connectivity must be enabled; expired links/errors fail without checkpointing.
  Individual rows and messages still need to fit memory and Service Bus limits.
- Every polling source (Blob, SQL, PostgreSQL, Databricks, SharePoint) uses
  ownership leases, including explicit resets. Cosmos CFP keeps its native
  partition leases. Missing destinations and construction failures do not
  silently checkpoint data or abort discovery of unrelated sources.
- Cosmos patch-feedback deduplication requires matching **content hash,
  pipeline and reset timestamp**. Existing `embedded_at` alone must not suppress
  subsequent content changes.

SQL/PostgreSQL/Blob/Databricks cursors are still in-memory: process restart
replays source data. Delivery is at-least-once, not exactly-once. SharePoint has
its separate durable outbox/revision protocol. These changes do not generalize
SharePoint's identity/chunk replacement/version fencing to other sources.
Inline source metadata has a single owner, regardless of vector field.
Discovery refuses conflicting inline writers against the same normalized
Cosmos endpoint/database/container or SQL/PostgreSQL server/port/database/schema/table,
including different source registrations and same-pipeline source aliases.
It stops affected watchers, retains checkpoints and pending resets, and logs
the refusal without connection credentials. Queue work sharing an affected source
also waits; unrelated sources continue. Pause competing pipelines to resolve
existing conflicts. Invalid inline target configuration fails closed.
This is a periodic discovery safeguard, not an atomic distributed ownership fence:
already-running operations may finish before reconciliation, and distinct DNS/server
aliases for one physical target are not resolved. Changing source connection
configuration may require restarting its watcher; destination configuration
refreshes during discovery.

Pipeline pause/delete/reset is not yet an authoritative worker cancellation
fence. Queued or in-flight work can still write after a lifecycle change.
Pause removes a pipeline from new source discovery/enqueue after reconciliation;
already queued or in-flight work may complete. Workers do not discard paused
deliveries or compare incompatible source-specific `PipelineGeneration` formats.
Resume does not guarantee a full replay, so discarding paused deliveries would
lose work. No new queue pause/reset protocol or internal API is introduced here.
SharePoint's content revision fencing does not solve control-plane lifecycle
ordering. Destructive resets require quiescing producers/workers and explicitly
handling queued deliveries until consistent lifecycle tokens and safe
pause/resume replay semantics are enforced end to end.

## Local regression command

```powershell
dotnet run --project tests\sharepoint\SharePoint.Regression.csproj --no-restore --verbosity quiet
```

The project references both production .NET projects and adds no test packages.
It exercises HTTP retries, lock renewal/budgets, pipeline fairness, failed
publication/checkpoints, destination acknowledgment, and the retained SharePoint
regressions using local doubles. It does not replace deployed Azure, SQL/PG,
Databricks, or load/memory integration testing.
