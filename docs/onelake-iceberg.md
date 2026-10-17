# OneLake Apache Iceberg

[README](../README.md) | [User guide](user-guide.md) | [Pipeline tuning](pipeline-tuning.md)

OmniVec reads existing OneLake Iceberg rows with a dedicated PyIceberg watcher
and can either send embeddings to a separate supported destination or write
them back to the same rows through a Fabric Spark Job Definition. It never
writes Iceberg metadata directly.

## Choose your path

| Goal | Required components |
|------|---------------------|
| Read OneLake into Cosmos DB Vector or Garnet | Iceberg watcher, queue workers, embedding model, destination access |
| Write embeddings into the original lakehouse rows | Above, plus write-back columns, staging access, and a Spark MERGE Job Definition |
| Search a OneLake write-back destination | A Garnet serving mirror; the lakehouse itself is not an OmniVec search index |
| Lower-latency change delivery | A separately configured long-running Spark Delta CDF job; the watcher remains the recovery path |

Grant catalog/data read, checkpoint write, and Service Bus permissions before
enabling the watcher. Write-back additionally requires staging/table write and
Fabric job execution permissions. Cross-tenant access requires the federated
identity setup below, not a client secret in source configuration.

Use `processing_mode: "queue"`. Same-row OneLake write-back also requires
`content_strategy: "truncate"` and the **same lakehouse and source table**;
it updates existing rows rather than inserting new ones.

## Configuration

The following JSON is the source **config object**, not the entire API request.
Use it under `config` with `name` and `type: "onelake-iceberg"` in
`POST /api/sources`, or save it as `onelake-source.json` and pass it to
`omnivec source create --name "Lakehouse documents" --type onelake-iceberg -f onelake-source.json`.

```json
{
  "catalog_uri": "https://onelake.table.fabric.microsoft.com/iceberg",
  "warehouse": "<workspaceId>/<lakehouseItemId>",
  "namespace": "dbo",
  "table": "documents",
  "content_fields": ["title", "body"],
  "id_field": "id",
  "poll_interval_seconds": 60,
  "batch_size": 200,
  "fabric_retry_interval_seconds": 900,
  "checkpoint_account_url": "https://onelake.dfs.fabric.microsoft.com",
  "checkpoint_file_system": "<workspaceId>",
  "checkpoint_path": ".omnivec/checkpoints",
  "change_data_feed": {
    "enabled": false,
    "checkpoint_path": "Files/omnivec/cdf-checkpoints/documents",
    "trigger_interval_seconds": 10,
    "recovery_poll_interval_seconds": 900,
    "max_files_per_trigger": 1000
  }
}
```

Same-tenant pipelines use the deployment workload identity. For cross-tenant
OneLake access, set the federated application on the individual pipeline source:

```json
{
  "source_id": "src-onelake",
  "onelake_identity": {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222"
  }
}
```

Cross-tenant pipelines use isolated credentials and checkpoint files. The
deployment identity continues to send Service Bus messages in the OmniVec
tenant. Supported destination paths include `cosmosdb-vector`, standalone
`garnet`, the same-lakehouse `onelake-iceberg` write-back destination below,
and benchmark-only `null`. A source registration alone does not start ingestion:
create a compatible pipeline and explicitly Run or Resume it.

The watcher hashes only `content_fields`; managed fields such as `embedding`,
`content_hash`, `pipeline_id`, `pipeline_generation`, and
`omnivec_writer_marker` are never hashed. Checkpoints are stored at
`<lakehouseItemId>/Files/<checkpoint_path>/<sourceId>.json` in OneLake and
contain source-ref/content-hash idempotency state per pipeline generation.
They also store a pipeline fingerprint, so adding a pipeline or changing its
model, generation, write-back mapping, or serving mirror forces a rescan even
when the Iceberg snapshot itself has not changed.
Configure the pipeline's OneLake destination `target_table` to this same
source table. Use the identifier visible to the attached Spark lakehouse
(commonly `documents`); namespace-qualified names such as `dbo.documents` are
also accepted when that Spark catalog exposes them.
When `change_data_feed.enabled` is true, the watcher performs the initial
prefill immediately and then scans at `recovery_poll_interval_seconds` as a
repair path. Keep it false for watcher-only operation. Enable it only when the
separate Delta CDF job below is configured and running; this flag does not
provision or start a Fabric stream.

For write-back, use this destination **config object** with
`type: "onelake-iceberg"` in `POST /api/destinations`, or a CLI `--file`.
Remove `mirror` entirely if no serving mirror is required.

```json
{
  "workspace_id": "<workspaceId>",
  "lakehouse_item_id": "<lakehouseItemId>",
  "spark_job_definition_item_id": "<sparkJobDefinitionItemId>",
  "staging_account_url": "https://onelake.dfs.fabric.microsoft.com",
  "staging_file_system": "<workspaceId>",
  "staging_path": "<lakehouseItemId>/Files/omnivec/staging",
  "target_table": "documents",
  "merge_batch_size": 5000,
  "merge_flush_interval_seconds": 30,
  "max_concurrent_merges": 1,
  "writeback_columns": {
    "id_field": "id",
    "embedding_field": "embedding",
    "content_hash_field": "content_hash",
    "pipeline_id_field": "pipeline_id",
    "pipeline_generation_field": "pipeline_generation",
    "model_field": "embedding_model",
    "source_id_field": "source_id",
    "source_ref_field": "source_ref",
    "writer_marker_field": "omnivec_writer_marker",
    "run_id_field": "omnivec_run_id",
    "embedded_at_field": "embedded_at"
  },
  "mirror": {
    "type": "garnet",
    "destination_id": "optional-serving-destination-id",
    "best_effort": false,
    "config": {
      "endpoint": "<garnet-host>:6380",
      "use_entra_auth": false,
      "tls": true,
      "username": "omnivec",
      "password_secret_ref": "kv://<vault>/<secret>",
      "vector_set": "omnivec-vectors",
      "distance_metric": "COSINE",
      "quantization": "NOQUANT",
      "m": 16,
      "ef": 200
    }
  }
}
```

The merge controls are user-configurable in the OneLake destination form.
`merge_batch_size` submits a full batch, while
`merge_flush_interval_seconds` bounds latency for a partially filled batch.
`max_concurrent_merges` limits active Fabric Spark MERGE submissions for the
destination. The defaults favor sustained ingestion without launching one
Spark job per embedding microbatch.

Staged JSONL names are deterministic from the target table and
pipeline/source/ref/content hashes. The worker first persists each embedding
microbatch under the destination staging path. A distributed OneLake
coordinator combines those durable files and submits a Fabric job when the
configured row threshold or flush interval is reached. Accepted source
messages therefore do not require one Fabric job per embedding call.
The Spark job refuses to insert a missing source row: it updates only the
configured `id_field` in the existing target/source table. Each update carries
the monotonic Iceberg snapshot sequence in `omnivec_run_id`, so a late older
Spark job cannot overwrite or resurrect data after a newer update or delete.
Rows deleted from the source or changed to empty configured content clear the
managed write-back columns and are removed from the serving mirror. When configured, the
Garnet mirror stores vectors in a native DiskANN-backed Vector Set using
`VADD`, JSON attributes, and deterministic element IDs derived from
`pipelineId`, `sourceId`, and `sourceRef`. The Garnet server must be started
with `--enable-vector-set-preview`. The client uses RESP2 because `VSIM` does
not yet support RESP3. TLS defaults on. Self-hosted Garnet can use an ACL
username with a Key Vault-backed `password_secret_ref`; anonymous access is
also possible when the deployment permits it. `use_entra_auth` is only for
endpoints that explicitly implement the Azure Redis Entra token flow. Azure
Managed Redis runs Redis Enterprise rather than Garnet and is not a substitute
for a Garnet server with Vector Sets enabled. Mirror failures fail the Service
Bus batch unless `mirror.best_effort` is true.
The legacy `redis` mirror type remains available for existing string-mirror
configurations, but it is not vector-searchable. New serving mirrors should
use Garnet.

### Verify each stage

After creating the pipeline, explicitly Run or Resume it. Verify source scans
and worker progress, then inspect staging and Spark job status, then confirm
the target rows contain the expected vectors. Finally query the Garnet mirror
if configured. A successful queue acknowledgment or staging write does **not**
prove that MERGE finished or that the serving mirror contains every expected row.

## Fabric Spark Job Definition

Upload [onelake_iceberg_merge.py](../connectors/fabric_spark/onelake_iceberg_merge.py) as the Spark Job
Definition entry point. The worker invokes the Spark Job Definition API with
`executionData.commandLineArguments` and binds `defaultLakehouseId` to the
source lakehouse:

```text
--staging-path <executionData.stagingPath>
--target-table <executionData.targetTable>
--run-id <executionData.runId>
--writer-marker <executionData.writerMarker>
--writeback-columns-base64 <base64-encoded writeback column JSON>
```

The execution request is sent to:

```text
POST /v1/workspaces/{workspace_id}/sparkJobDefinitions/{spark_job_definition_item_id}/jobs/sparkjob/instances
```

The Spark Job Definition should use the checked-in Python file as its default
executable. Alternatively set `spark_executable_file` to its OneLake `abfss://`
path so the worker overrides the executable for each run.

Add the write-back columns to the existing source Iceberg table (adjust
embedding dimensions if your Spark runtime requires a typed fixed
representation):

```sql
ALTER TABLE dbo.documents ADD COLUMNS (
  embedding ARRAY<FLOAT>,
  content_hash STRING,
  pipeline_id STRING,
  pipeline_generation STRING,
  embedding_model STRING,
  source_id STRING,
  source_ref STRING,
  omnivec_writer_marker STRING,
  omnivec_run_id STRING,
  embedded_at TIMESTAMP
);
```

The Spark MERGE key is the configured source row `id_field`; it only updates
rows when the stored content hash, model, pipeline ID, or pipeline generation
differs. The watcher hashes only configured source text columns and skips rows
whose stored OmniVec hash/model/generation match, preventing circular
write-back loops. Grant the workload identity OneLake read/write access
(including the source table, checkpoint, and staging paths), Service Bus
Send/Receive, Fabric job execution permission, Key Vault secret access when
ACL authentication is configured, and network access to Garnet. Enable the
watcher only after assigning these permissions:

```powershell
azd env set OMNIVEC_ONELAKE_ICEBERG_ENABLED true
azd env set OMNIVEC_BUILD true
azd up
```

## Streaming inserts, updates, and deletes with Delta CDF

Fabric Lakehouse tables are physically Delta tables even when OneLake exposes
them through the Iceberg REST catalog. Use Delta Change Data Feed for the live
path and keep the Iceberg watcher for prefill and recovery.

Enable CDF before starting the stream:

```sql
ALTER TABLE dbo.documents SET TBLPROPERTIES (
  'delta.enableChangeDataFeed' = 'true'
);
```

Upload these files to a long-running Fabric Spark Job Definition:

- [onelake_delta_cdf_stream.py](../connectors/fabric_spark/onelake_delta_cdf_stream.py)
- [onelake_delta_cdf_helpers.py](../connectors/fabric_spark/onelake_delta_cdf_helpers.py)
- [omnivec_queue/__init__.py](../omnivec_queue/__init__.py), uploaded as the Python dependency
  `omnivec_queue.py` (the pure envelope codec does not require the Azure SDK).

The job reads `insert`, `update_postimage`, and `delete` records. Updates made
only by OmniVec are discarded when the stored content hash, model, pipeline
ID, and generation match. An update that clears all configured source content
is emitted as a delete.

Create a Key Vault secret containing a Service Bus namespace connection string
with **Send** permission only. Grant the Fabric job identity permission to read
that secret. Fabric NotebookUtils doesn't expose a Service Bus token audience,
so the connection string is retrieved from Key Vault at runtime and is never
placed in Spark arguments or source control.

Start the job after the first watcher prefill completes:

```text
--target-table dbo.documents
--checkpoint-location abfss://<workspaceId>@onelake.dfs.fabric.microsoft.com/<lakehouseId>/Files/omnivec/cdf-checkpoints/documents
--service-bus-namespace <namespace>.servicebus.windows.net
--service-bus-topic embeddings
--service-bus-key-vault-url https://<vault>.vault.azure.net/
--service-bus-connection-secret-name omnivec-servicebus-send
--pipeline-config-base64 <base64-encoded JSON array>
--trigger-interval-seconds 10
--max-files-per-trigger 1000
```

For a new stream/checkpoint, omitting `--starting-version` lets Delta CDF emit
the current snapshot as inserts, then future changes. It does **not** mean
"future changes only" and may overlap the watcher's prefill. Set a starting
version only after choosing a deliberate boundary within retained CDF history;
do not create a gap between prefill and streaming.

Spark's checkpoint provides microbatch recovery. Deterministic message IDs and
source/pipeline versions support replay handling, but delivery remains at least
once. Service Bus duplicate detection, if configured, can reduce duplicate
publication within its detection window; it is not an exactly-once guarantee.

The decoded `pipeline-config-base64` value is an array with one entry per active
pipeline. This is a **plan shape**, not a ready-to-run configuration: replace all
sample IDs/model/generation values and populate `destination_config` with the
complete resolved destination configuration shown earlier.

```json
[
  {
    "pipeline_id": "pip-documents",
    "pipeline_name": "Documents",
    "docgrok_pipeline": "text-embedding-model",
    "pipeline_generation": "1",
    "pipeline_revision": 1,
    "resource_policy": {"queue_documents_per_message": 100},
    "source_id": "src-onelake",
    "id_field": "id",
    "content_fields": ["title", "body"],
    "destination_id": "dst-onelake",
    "destination_type": "onelake-iceberg",
    "destination_config": {},
    "writeback_columns": {
      "content_hash_field": "content_hash",
      "pipeline_id_field": "pipeline_id",
      "pipeline_generation_field": "pipeline_generation",
      "model_field": "embedding_model"
    }
  }
]
```

This job remains running and consumes Fabric capacity while active. If
continuous Spark capacity isn't desired, retain the polling watcher or route
upstream database CDC through Fabric Eventstream before landing the table.

Stage-1 [broker pipeline lanes](pipeline-tuning.md#opt-in-broker-pipeline-lanes-stage-1)
are not compatible with CDF-enabled OneLake sources. Do not enable both.

`resource_policy.queue_documents_per_message` is optional (default 1, maximum
1000). Copy it from the API pipeline policy into each manually supplied plan;
running Spark jobs do not hot-reload API policy changes. For live changes
without restarting the stream, supply `--pipeline-config-path` instead of
`--pipeline-config-base64`: the job reads that JSON file at each microbatch
boundary. Replace the file atomically to avoid partial JSON reads.
Plans also accept `queue_max_envelope_bytes` (default 229376, max 983040),
`queue_send_batch_max_bytes` (default 245760, max 1048576), and
`queue_publish_batch_size` (default 200, max 1000). Reserve at least 16384
bytes between envelope and send-batch caps and confirm the broker supports
the chosen sizes. Messages flush at microbatch and configured byte/count
boundaries, retaining each document's identity and source version.
Deploy envelope-aware workers first. Changing transport packing does not
change model-request token limits or Structured Streaming checkpoint semantics.
