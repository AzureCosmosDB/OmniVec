# SharePoint Online source

[README](../README.md) | [User guide](user-guide.md) | [SharePoint-to-Foundry example](../examples/sharepoint-foundry/README.md)

OmniVec can ingest files from a SharePoint Online document library by polling
Microsoft Graph delta changes. The watcher publishes stable site, drive, and
item identifiers to Service Bus; the worker downloads each file with workload
identity and sends the bytes to DocGrok for extraction, chunking, and embedding.

## Before you start

1. Obtain Graph application consent and access to the intended site/library.
2. Prepare a Cosmos vector destination with matching embedding dimensions and
   a dedicated document partition key such as `/document_id`, **not `/id`**.
3. Deploy compatible SharePoint watcher, .NET worker, router, and extraction
   components with Service Bus access.
4. Register a folder-scoped source and a queue pipeline, then explicitly
   **Run or Resume** it.
5. Verify file ingestion, destination vectors, and search. Registration or
   connection-test success alone is not end-to-end proof.

The worker buffers files in memory, with a maximum raw file size of 50 MiB.
Start with a small sample and low file concurrency before increasing load.

## Permissions

Grant the OmniVec workload identity a Microsoft Graph application permission:

- `Sites.Selected` (recommended), followed by a read grant on each allowed site.
- `Files.Read.All` for tenant-wide document-library access.

Admin consent is required. No SharePoint client secret, certificate, or download
URL is stored in source configuration or placed on Service Bus.

These are **ingestion workload permissions**, not permission to expose every
indexed file to every user. For user-facing retrieval, configure the separate
[protected Cosmos MCP endpoint](../mcp_servers/cosmos/README.md#protected-sharepoint-deployment).
It uses each user's delegated Graph access, not the ingestion identity or an
ACL snapshot.

For security review, see the detailed
[SharePoint permission-enforcement design](security/sharepoint-acl-enforcement.md).
It traces provenance through ingestion and Cosmos, delegated MCP authorization,
agent-visible output, deployment prerequisites, and the live verification gates.

The SharePoint watcher also requires Service Bus and the .NET embedding worker.
Pipelines with an explicit `sharepoint_identity` get isolated watchers, ownership
leases, Graph credentials, delta cursors, revision sequences, and item-to-path
mappings, even when that identity uses the deployment's tenant. Pipelines that
omit `sharepoint_identity` retain the legacy shared source cursor so upgrades
preserve existing outboxes, revision high-water marks, and delete reconciliation
state.

## Source configuration

Submit this body to `POST /api/sources`, replacing the site/drive IDs. In the
CLI, pass only the nested `config` object to `--file`, with `--name` and `--type`
as separate flags.

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

| Field | Default | Limits and behavior |
|---|---|---|
| `site_id` | required | 1-512 characters, trimmed. |
| `drive_id` | required | 1-256 characters, trimmed. |
| `folder_path` | `""` (library root) | Up to 1024 characters; surrounding `/` are removed. |
| `file_types` | `txt`, `json`, `pdf`, `docx`, `md`, `csv`, `html`, `xml` | Up to 50 extensions, normalized to lowercase without a leading dot. An empty list allows every extension. |
| `poll_interval_seconds` | `60` | 10-86400. |
| `max_file_size_bytes` | `52428800` (50 MiB) | Greater than 0 and at most 50 MiB. |
| `auth_type` | `managed-identity` | The only accepted value. |

The connection test that runs on registration is advisory: a failure adds a
warning to the response but still creates the source.

After a pipeline uses the source, treat its configuration as fixed. The API
accepts later edits, and the watcher restarts, but it keeps its persisted delta
cursor and does not rescan. A changed `folder_path` therefore has no effect until
a pipeline reset forces a full scan; changed `file_types` or
`max_file_size_bytes` apply only to items Graph reports as changed afterward.
Never edit `site_id` or `drive_id` in place; see
[Upgrade from path-based SharePoint documents](#upgrade-from-path-based-sharepoint-documents).

Use Microsoft Graph Explorer or the Graph API to resolve the site and drive IDs:

```text
GET /v1.0/sites/{hostname}:/sites/{site-path}
GET /v1.0/sites/{site-id}/drives
```

## Pipeline-scoped identity

Same-tenant pipelines can omit `sharepoint_identity` and use the deployment's
normal managed identity. For cross-tenant access, configure the SharePoint app
registration on the individual pipeline source:

```json
{
  "name": "Policy search",
  "sources": [{
    "source_id": "src-sharepoint",
    "sharepoint_identity": {
      "tenant_id": "11111111-1111-1111-1111-111111111111",
      "client_id": "22222222-2222-2222-2222-222222222222"
    }
  }],
  "docgrok_pipeline": "mdl-example",
  "destination_id": "dst-vectors",
  "vector_index_path": "embedding",
  "processing_mode": "queue"
}
```

The IDs are non-secret identity selectors stored with the pipeline and copied
to that pipeline's Service Bus messages. The watcher and worker exchange the
projected Kubernetes service-account token for Microsoft Graph tokens using the
selected tenant/app. No tenant/client ID is configured in Helm, `azd`, Bicep, or
deployment hooks.

The app registration must have Microsoft Graph `Sites.Selected` application
permission, admin consent, a site-specific `read` grant, and a federated
credential matching the AKS OIDC issuer and
`system:serviceaccount:omnivec:omnivec-api`.

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

Use compatible images for all components implementing the SharePoint protocol.
If the selected published images do not include that support, build from source
as shown. Enabling the watcher alone does not update older shared images.

With `sharepointWatcher.enabled: true`, Helm rendering fails unless
`azure.serviceBus.namespace` is configured and `dotnetWorker.enabled` is `true`;
the .NET worker downloads and processes the published SharePoint references.

The default and maximum file size is **50 MiB of raw file bytes**. The router
allows **70 MiB of HTTP request body**, including base64 expansion (~66.7 MiB
for a 50 MiB file) and JSON overhead. Both the router's HTTP limit and Axum's
extractor limit are configured. Outbound API JSON does not HTML-escape base64
characters such as `+`, which would otherwise exceed that budget. The worker enforces the configured raw-byte
limit while downloading, even without a Content-Length header.

The watcher applies the same limit earlier, using the size Graph reports. It
does not publish files that exceed `max_file_size_bytes`, do not match
`file_types`, or are not files (for example folders), and logs a warning for
oversized files. If such an item was previously indexed, the watcher publishes a
delete instead, so its existing vectors are removed. For example, a file that
grows past the limit, or is renamed to an excluded extension, disappears from
the destination.

This is a bounded, **in-memory**, not streaming, implementation. Download buffers,
base64 strings, serialized JSON, extracted text, and vectors can coexist. A
50 MiB file can require several hundred MiB per concurrent operation.
`Worker__SharePointConcurrency` (default 1) caps SharePoint files per worker
replica, inside the broader `Worker__BlobConcurrency` file-processing limit
(default 32). The Helm chart does not expose either setting; override them as
environment variables on the .NET worker deployment if needed. Other messages
handled by the same worker, bounded by `Worker__MaxConcurrentCalls`
(`dotnetWorker.maxConcurrentCalls`), share its memory. Lower the source size
limit as needed, and size worker/router/pipeline-worker memory accordingly. Compressed documents can expand far beyond their input size.

## Destination and synchronization contract

SharePoint supports **queue mode with a Cosmos DB vector destination**. Use a
dedicated document partition key such as `/document_id` (a nested dedicated
path is also supported), **not `/id`**, a vector field, or metadata fields.
Probe the destination before creating the pipeline. All chunks and the
synchronization manifest for one item/pipeline must share a logical partition
so Cosmos transactions can fence concurrent workers. Hierarchical partition
keys are not supported.

Pipeline creation validates the destination's stored `partition_key_path`. The
destination probe fills that value only when it is absent and records only the
first path of a hierarchical key, so the API cannot detect an explicitly
configured mismatch or a hierarchical container. The worker reads the actual
container definition on every write and validates that instead: it rejects
hierarchical keys and an actual partition key that is `/id`, a synchronization
field, or the vector field. Those messages are abandoned and retried rather than
written.

- The partition/document identity is `sp-` plus SHA-256 of the JSON array
  `[source_id, site_id, drive_id, item_id, pipeline_id]`. It is Cosmos-safe and
  independent of filenames. Chunk IDs append the durable revision and index.
  Renames update display metadata, not identity; different sources and pipelines
  cannot overwrite each other's chunks. Moves within the watched folder keep the
  same Graph item ID and are handled like renames. Items Graph reports as
  deleted, and items that stop matching the source filters, are removed.
- `source_ref` is display metadata built from the file name and the delta
  item's `parentReference.path`. Microsoft Graph documents that delta responses
  do not include that path, and that renaming a folder does not return its
  descendants, so `source_ref` can contain only the file name and is not
  refreshed by folder renames. Identity, deletes, and retrieval authorization
  use the stable site, drive, and item IDs instead.
- `source_id`, `pipeline_id`, `source_ref`, `_omnivec_sync`, and
  `_omnivec_provenance` are mandatory
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
  Pipelines on one source that omit `sharepoint_identity` share a single watcher
  and cursor, and it publishes every change to all of them. Attaching or
  resetting any one of these pipelines therefore rescans the source and
  re-downloads and re-embeds eligible files for **all pipelines sharing that
  watcher**. Pipelines with an explicit `sharepoint_identity` have their own
  watcher and rescan independently, regardless of whether the identity uses the
  deployment's tenant.
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
  A staging failure retains old vectors and may leave staged chunks until retry.
  Once all new chunks are staged, cleanup can remove some or all old chunks
  before a later cleanup batch or the final completion write fails. There is no
  rollback; protected retrieval withholds results while the manifest is incomplete.
  Monitor abandoned/dead-letter messages; replay is required to finish cleanup.
  Batches reserve room under Cosmos's 2 MiB/100-operation limits; a single
  serialized chunk larger than 1.5 MB is rejected without deleting old vectors.
- Downloads verify Graph's queued eTag before and after reading content.
  Superseded/missing versions are logged as superseded, and the message is
  completed. The last good vectors stay intact until the watcher's newer
  update/delete is processed.
  The watcher resolves the resource tenant from the drive root's
  `sharepointIds.tenantId`, verifies it against an explicitly selected tenant,
  and pins it in the checkpoint. The worker checks the tenant on file metadata
  before and after download. A configured application tenant is not a substitute
  for the actual SharePoint resource tenant.
- Missing/malformed processor `chunks`, empty vectors, missing embeddings, and
  dimension failures are errors and cause message retry. Permanent input
  failures are dead-lettered immediately as `PermanentProcessingFailure`, without
  retry: a download that exceeds the configured size limit, and processor HTTP
  400/413/415/422 responses. Replay them after correcting the cause. A zero-byte file or
  an explicit processor `skipped: true` with a reason is an intentional empty
  chunk set and removes previous vectors. Empty documents are not embedded as
  artificial placeholder text.

Cosmos-source and Blob chunk upserts keep their existing writer path. The
shared legacy delete path now also scopes deletion to `pipeline_id` and
propagates unsuccessful delete batches instead of acknowledging them.

Every chunk and synchronization manifest contains this reserved provenance
block, independent of optional metadata selection:

```json
{
  "_omnivec_provenance": {
    "schema_version": 1,
    "provider": "sharepoint",
    "resource_tenant_id": "11111111-1111-1111-1111-111111111111",
    "site_id": "contoso.sharepoint.com,site-guid,web-guid",
    "drive_id": "drive-id",
    "item_id": "item-id",
    "source_etag": "<exact Graph eTag of the embedded file>"
  }
}
```

These are stable source identifiers and the embedded version, not a flattened
permission list. They contain no user tokens or preauthenticated download URLs.
The writer validates their binding to the source/pipeline document identity.
Destination field settings cannot overwrite either reserved block.

## Permission-aware retrieval

Enable protected retrieval explicitly for dedicated, trusted-writer SharePoint
containers. The MCP server validates an Entra user token for its own API and
uses OBO to check Graph metadata **and content-read access** as that user. It
returns text/citations only from the exact indexed eTag and a completed current
Cosmos revision, hiding stale, incomplete, missing-provenance and inaccessible
rows. Both listing and vector search apply this gate. Graph outages and consent
challenges are explicit failures rather than apparently successful empty results.

The first supported caller is Foundry custom per-user OAuth in the same tenant.
Cross-tenant ingestion still works, but retrieval of another resource tenant's
rows is denied. Shared Function keys, project/agent managed identity, and native
Functions MCP tool triggers do not provide a user context for protected reads.
Generic/admin search remains privileged and must not be exposed as an alternate
path to the same private data.

Follow the [MCP identity, supported-file limits and client setup](../mcp_servers/cosmos/README.md).
This feature is not a general Purview/IRM/no-download enforcement engine. It
does not retroactively erase previously returned data or Foundry history.

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
an explicit migration rather than reusing its existing checkpoint. The API does
not enforce this: it accepts in-place edits, and the watcher keeps the source's
persisted delta cursor.

### Upgrade from revision-based records without provenance

This also requires a coordinated watcher/worker upgrade. Stop the old watcher
and drain or explicitly retire already-published legacy envelopes before
starting the new worker: upserts without a resource tenant or source eTag, and
deletes without a resource tenant, fail
closed. The upgraded watcher can enrich its own persisted, unpublished outbox
with the verified resource tenant without changing event IDs.

Checkpoint schema 3 forces a full scan of earlier checkpoints, preserving
revision high-water marks and reconciliation state. Keep both the lease state
and destination manifests. The scan rewrites chunks with mandatory provenance;
do not simply copy source IDs onto old vectors or reset revisions. Protected
retrieval hides legacy chunks until the new version is fully committed.
Enable the protected Function App only with its complete Entra/OBO configuration
and verify the backfill and per-user access before switching callers.

## Local regression validation

No live Azure resources are needed:

```powershell
dotnet run --project tests\sharepoint\SharePoint.Regression.csproj
python -m pytest tests\unit\test_sharepoint_processing.py tests\unit\test_models.py tests\unit\test_openapi_snapshots.py -q
python -m pytest tests\unit\test_sharepoint_mcp.py tests\unit\test_cosmos_mcp.py tests\unit\test_foundry_cosmos_mcp_agent.py -q
cargo test --manifest-path docgrok\router\Cargo.toml request_body
```

The dependency-free .NET console exercises production identity, watcher,
publisher, worker, and chunk-replacement logic with local HTTP/queue/Cosmos
transaction doubles. It does not substitute for a deployed Azure end-to-end
test, Cosmos transaction/consistency verification, or a production memory test.
The MCP tests use local signed tokens and Graph/Cosmos doubles; they do not
grant consent, configure a Foundry connection, or exercise a real tenant.
