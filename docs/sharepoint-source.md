# SharePoint Online source

OmniVec can ingest files from a SharePoint Online document library by polling
Microsoft Graph delta changes. The watcher publishes stable site, drive, and
item identifiers to Service Bus; the worker downloads each file with workload
identity and sends the bytes to DocGrok for extraction, chunking, and embedding.

## Permissions

Grant the OmniVec workload identity a Microsoft Graph application permission:

- `Sites.Selected` (recommended), followed by a read grant on each allowed site.
- `Files.Read.All` for tenant-wide document-library access.

Admin consent is required. No SharePoint credential or download URL is stored in
source configuration or placed on Service Bus.

The SharePoint watcher also requires Service Bus and the .NET embedding worker.
The watcher stores its Graph delta link and item-to-path mappings in the source's
Cosmos lease container so progress and delete handling survive pod restarts.
Attaching a new active pipeline resets that source cursor and replays the library
so the new pipeline receives existing files.

## Source configuration

```json
{
  "name": "Corporate policies",
  "type": "sharepoint",
  "config": {
    "site_id": "contoso.sharepoint.com,site-guid,web-guid",
    "drive_id": "drive-guid",
    "folder_path": "Policies",
    "file_types": ["pdf", "docx", "txt", "md", "html"],
    "poll_interval_seconds": 60,
    "max_file_size_bytes": 52428800,
    "auth_type": "managed-identity"
  }
}
```

Use Microsoft Graph Explorer or the Graph API to resolve the site and drive IDs:

```text
GET /v1.0/sites/{hostname}:/sites/{site-path}
GET /v1.0/sites/{site-id}/drives
```

Enable the dedicated watcher in Helm:

```yaml
sharepointWatcher:
  enabled: true
```

For an `azd` installation, persist the setting instead of applying a one-off
Helm override that the next deployment would replace:

```powershell
azd env set OMNIVEC_SHAREPOINT_ENABLED true
azd env set OMNIVEC_BUILD true
azd up
```

For this feature branch, build from source as shown or supply matching images
for all changed services. Enabling the watcher alone does not update older
shared registry images to include the SharePoint protocol.

`azure.serviceBus.namespace` must be configured or Helm rendering fails. Keep
`dotnetWorker.enabled: true` so published SharePoint references are downloaded
and processed.

The default and maximum file size is **50 MiB of raw file bytes**. The router
allows **70 MiB of HTTP request body**, including base64 expansion (~66.7 MiB
for a 50 MiB file) and JSON overhead. Both the router's HTTP limit and Axum's
extractor limit are configured. Outbound API JSON does not HTML-escape base64
characters such as `+`, which would otherwise exceed that budget. The worker enforces the configured raw-byte
limit while downloading, even without a Content-Length header.

This is a bounded, **in-memory**, not streaming, implementation. Download buffers,
base64 strings, serialized JSON, extracted text, and vectors can coexist. A
50 MiB file can require several hundred MiB per concurrent operation. Reduce
`Worker__BlobConcurrency` and `Worker__MaxConcurrentCalls`, lower the source size
limit, and size worker/router/pipeline-worker memory accordingly. Compressed
documents can expand far beyond their input size.

## Destination and synchronization contract

SharePoint supports **queue mode with a Cosmos DB vector destination**. Use a
dedicated document partition key such as `/document_id` (a nested dedicated
path is also supported), **not `/id`**, a vector field, or metadata fields.
Probe the destination before creating the pipeline. All chunks and the
synchronization manifest for one item/pipeline must share a logical partition
so Cosmos transactions can fence concurrent workers. Hierarchical partition
keys are not supported.

- The partition/document identity is `sp-` plus SHA-256 of the JSON array
  `[source_id, site_id, drive_id, item_id, pipeline_id]`. It is Cosmos-safe and
  independent of filenames. Chunk IDs append the durable revision and index.
  Renames update display metadata, not identity; different sources and pipelines
  cannot overwrite each other's chunks.
- `source_id`, `pipeline_id`, `source_ref`, and `_omnivec_sync` are mandatory
  synchronization fields for SharePoint, independent of optional metadata
  selections. Content/vector/partition fields cannot overwrite these fields.
  `_omnivec_sync.kind` distinguishes `chunk` from `state`; the latter has no
  vector. Retrieval/count queries should select `chunk` records or require the
  vector field to be defined.
- A persisted page outbox captures targets, deterministic event IDs, and a
  monotonically increasing revision **before publication**. References and the
  next Graph checkpoint advance only after all messages have been sent.
  A send or checkpoint failure replays the same outbox. Cosmos ETags prevent
  concurrent watchers from advancing the same checkpoint independently.
- Initial enumeration, newly attached pipelines, and expired delta tokens
  trigger a full scan. On completion, persisted items not seen in that scan
  receive tombstones, including deletes missed while a delta token was invalid.
  The outbox is bounded by Cosmos's 2 MiB document limit: an unusually large
  Graph page or large fan-out/configuration fails without advancing the cursor.
  Pipeline resets also force a full scan while preserving the outbox/revision
  history; they do not delete the SharePoint lease container.
- The destination first claims a revision, stages all new chunks, then removes
  obsolete chunks and marks the revision complete. Every write/delete batch
  conditionally updates the same manifest ETag. A newer revision fences older
  workers, including workers whose queue locks expired. Completed duplicates
  are idempotent; incomplete duplicates resume safely. Delete/empty results
  retain a high-water tombstone so delayed upserts cannot resurrect content.
  The manifest sets `ttl: -1` so a container-level TTL cannot remove its
  high-water mark. Do not manually delete manifests while queued events remain.
- This is **at-least-once**, not exactly-once delivery or an atomic whole-document
  swap. Readers can see old and staged new chunks together during replacement.
  A failed attempt retains old vectors and may leave staged chunks until retry.
  Monitor abandoned/dead-letter messages; replay is required to finish cleanup.
  Batches reserve room under Cosmos's 2 MiB/100-operation limits; a single
  serialized chunk larger than 1.5 MB is rejected without deleting old vectors.
- Downloads verify Graph's queued eTag before and after reading content.
  Superseded/missing versions are logged as skipped and leave the last good
  vectors intact until the watcher's newer update/delete is processed.
- Missing/malformed processor `chunks`, empty vectors, missing embeddings, and
  dimension failures are errors and cause message retry. A zero-byte file or
  an explicit processor `skipped: true` with a reason is an intentional empty
  chunk set and removes previous vectors. Empty documents are not embedded as
  artificial placeholder text.

Cosmos-source and Blob chunk upserts keep their existing writer path. The
shared legacy delete path now also scopes deletion to `pipeline_id` and
propagates unsuccessful delete batches instead of acknowledging them.

## Upgrade from path-based SharePoint documents

Do not mix old workers/messages with the revised protocol. Stop the old watcher,
drain or explicitly retire its queued SharePoint messages, and deploy the
watcher, worker, router, and pipeline-worker together. Revision-less legacy
messages fail closed rather than writing unsafe path IDs.

**Prefer a new destination container** using `/document_id`, then reindex and
switch readers after verification. The watcher detects the old checkpoint
schema and performs a full scan. Keep its lease container and destination
manifests: clearing only the lease state resets revision ordering and is unsafe.
For a deliberate reset, use a new source ID and destination together.

Existing path-keyed records are **not automatically purged**: old metadata may
be missing, collided across pipelines, or refer to another source. If reusing a
compatible container, inventory/export legacy rows first and delete only rows
whose ownership and partition keys are verified, scoped by source and pipeline.
Never perform an unscoped path delete. This manual migration is necessary to
avoid either duplicate legacy search results or deletion of unrelated data.
Changing a source's site/drive identity likewise requires a new source ID and
an explicit migration rather than reusing its existing checkpoint.

## Local regression validation

No live Azure resources are needed:

```powershell
dotnet run --project tests\sharepoint\SharePoint.Regression.csproj
python -m pytest tests\unit\test_sharepoint_processing.py tests\unit\test_models.py tests\unit\test_openapi_snapshots.py -q
cargo test --manifest-path docgrok\router\Cargo.toml request_body
```

The dependency-free .NET console exercises production identity, watcher,
publisher, worker, and chunk-replacement logic with local HTTP/queue/Cosmos
transaction doubles. It does not substitute for a deployed Azure end-to-end
test, Cosmos transaction/consistency verification, or a production memory test.
