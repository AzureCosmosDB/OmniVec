# Blob ingestion: Event Grid push (replacing live-poll)

## Why
- Current `BlobSourceWatcher` does prefill scan + infinite live-polling on `LastModified`.
- Polling does not scale to huge containers; misses deletes and renames; expensive in LIST calls.
- Replace ongoing polling with Event Grid -> Service Bus -> in-cluster consumer.
- Keep prefill (one-time) - Event Grid does not backfill.

## Target architecture

```
Storage Account
   |- Event Grid system topic
         |- Subscription (per source) -> Service Bus queue "blob-events"
                |- omnivec-blob-ingestor / BlobEventConsumer
                      |- BlobCreated / BlobModified -> publish EmbeddingMessage to "embeddings" topic
                      `- BlobDeleted / BlobRenamed  -> publish DeleteMessage -> worker -> Cosmos DeleteItem
```

## Phase A - infra + provisioning (~2h)
1. **bicep**: `infra/modules/servicebus.bicep` - add `blob-events` queue (alongside `jobs`).
   - `maxDeliveryCount: 10`, `lockDuration: PT5M`, DLQ on expiration.
2. **bicep**: assign **EventGrid Data Sender** role on `blob-events` queue to the Event Grid identity.
3. **bicep / helm**: assign **EventGrid Contributor** + **Storage Account Contributor** on each source storage account to omnivec MI - per-source provisioning at runtime. Document the RBAC requirement.
4. **api/api.py**:
   - Modify `create_eventgrid_subscription`: replace `endpointType: "WebHook"` with `endpointType: "ServiceBusQueue"`, target the `blob-events` queue (env `OMNIVEC_BLOB_EVENT_QUEUE_RESOURCE_ID`).
   - Add auto-call from `create_pipeline` when source.type == azure-blob AND processing_mode == queue AND not already configured. Race-safe: provision EG **first**, then prefill - consumer dedupes by url+etag.
   - Add `POST /api/triggers/eventgrid/bulk_provision` to backfill existing pipelines on wintest.
5. **Manual setup on wintest** (until next bicep deploy):
   ```pwsh
   az servicebus queue create --namespace-name omnivec-sb-<cluster-suffix> -g rg-omnivec-wintest-3 --name blob-events --max-delivery-count 10
   ```
6. **Smoke test**: hit `/api/triggers/eventgrid/create` against an existing source, verify subscription in portal points to SB queue, upload blob, see message land in queue.

## Phase B - consumer + delete propagation (~3h)
7. **connectors/ingestion/dotnet/Services/BlobEventConsumer.cs** (new):
   - SB receiver loop on `blob-events` using `ServiceBusProcessor`.
   - Parse Event Grid envelope (both EG schema and CloudEvents schema).
   - For each event:
     - `Microsoft.Storage.BlobCreated` / `BlobModified`:
       - Resolve source by matching account+container+prefix against active sources.
       - For each active pipeline using that source, publish `EmbeddingMessage` to `embeddings` topic.
       - Dedupe key: `url + etag` (in-memory LRU per source).
     - `Microsoft.Storage.BlobDeleted` / `BlobRenamed`:
       - Publish new `DeleteMessage` to worker.
   - Backpressure: respect existing publisher flag.
   - DLQ on parse failure / non-recoverable errors.

8. **connectors/ingestion/dotnet/Services/SourceWatcherManager.cs**:
   - When `BLOB_USE_EVENTGRID=true` (default false initially), skip Phase 2 polling in `BlobSourceWatcher` - keep prefill only.
   - Start a single `BlobEventConsumer` per process (not per source).

9. **connectors/worker/dotnet/Models/EmbeddingMessage.cs** + writers:
   - Add `MessageType: "embed" | "delete"`.
   - On `delete`: Cosmos `DeleteItemAsync(id, partitionKey)` matching `source_id + source_ref`. SQL: `DELETE FROM <table> WHERE source_id=@s AND source_ref=@r`.
   - Scope delete by source_id to avoid clobbering other pipelines' docs.

10. **helm/omnivec/templates/blob-ingestor-deployment.yaml**:
    - Add `BlobSource__UseEventGrid=true`, `BlobSource__EventGridQueueName=blob-events`.

11. **Smoke + flip**:
    - Roll out with `UseEventGrid=true`, watch consumer logs.
    - Upload, modify, delete blobs on wintest source - verify embed + delete propagate end-to-end.
    - Confirm dot-net poll loop is quiet for that source.

## Risks / decisions to confirm before starting
- RBAC: probe storage account perms first; fall back gracefully and surface "needs RBAC" in pipeline status.
- Filter granularity: EG sub per source with `subjectBeginsWith=/blobServices/default/containers/<c>/blobs/<prefix>`. Two sources on same container+prefix double-fan-out (acceptable, dedup at destination).
- Backfill: existing pipelines on wintest need a one-time `bulk_provision` call.
- Polling fallback: keep behind a flag for initial bake-in; remove after 1 week stable.

## Out of scope
- Cross-tenant storage accounts (require cred from another tenant) - document, allow opt-out.
- Non-Azure blob backends - keep polling for S3/GCS if they appear.
- UI changes - the existing "Create Event Grid Subscription" button can stay; just becomes redundant when auto-provision is on.

## Files expected to change

Phase A:
- `infra/modules/servicebus.bicep` - add `blob-events` queue
- `api/api.py` - swap EG endpoint; auto-call from `create_pipeline`; bulk_provision endpoint
- `helm/omnivec/templates/blob-ingestor-deployment.yaml` - pass `BlobSource__EventGridQueueName`

Phase B:
- `connectors/ingestion/dotnet/Services/BlobEventConsumer.cs` (new)
- `connectors/ingestion/dotnet/Services/SourceWatcherManager.cs`
- `connectors/ingestion/dotnet/Services/BlobSourceWatcher.cs` (Phase 2 conditional)
- `connectors/worker/dotnet/Models/EmbeddingMessage.cs`
- `connectors/worker/dotnet/Destinations/CosmosDbDestinationWriter.cs`
- `connectors/worker/dotnet/Destinations/PostgresDestinationWriter.cs`
- `connectors/worker/dotnet/Destinations/MsSqlDestinationWriter.cs`
- `connectors/worker/dotnet/Services/EmbeddingWorkerService.cs`
- `helm/omnivec/templates/blob-ingestor-deployment.yaml`

## Estimated effort
- Phase A: ~2h
- Phase B: ~3h
- Total: ~5h, one PR.

## Wintest context for the new session
- AKS context: `omnivec-aks-<cluster-suffix>`
- Resource group: `rg-omnivec-wintest-3`
- SB namespace: `omnivec-sb-<cluster-suffix>.servicebus.windows.net` (existing queue `jobs`, topic `embeddings`, subscription `worker`)
- ACR: `omnivecacr<cluster-suffix>.azurecr.io` (env) + `omnivecregistry.azurecr.io` (CI build output)
- MI client id: `<MI_CLIENT_ID>`
- Existing pipeline: `pip-29511bf3` (paused), `pip-4705eaa3` (paused, ui-test)
- Test source: `src-b74b772f` (blob), dest: `dst-1e35d37d` (Cosmos), model: `mdl-ext-de45f285`
