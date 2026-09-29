# OneLake Apache Iceberg

OmniVec reads existing OneLake Iceberg rows with a dedicated PyIceberg watcher
and writes vectors and OmniVec metadata back to those same rows through a
Fabric Spark Job Definition. It never writes Iceberg metadata directly.

## Configuration

Create a source with `type: "onelake-iceberg"`:

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
    "enabled": true,
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
tenant. OneLake sources can target either a `cosmosdb-vector` destination or
the same-lakehouse `onelake-iceberg` write-back destination described below.

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
repair path. The Delta CDF job below handles low-latency changes.

Create a destination with `type: "onelake-iceberg"`:

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

## Fabric Spark Job Definition

Upload `connectors/fabric_spark/onelake_iceberg_merge.py` as the Spark Job
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

- `connectors/fabric_spark/onelake_delta_cdf_stream.py`
- `connectors/fabric_spark/onelake_delta_cdf_helpers.py`

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

Omit `--starting-version` for the normal prefill-to-live handoff; Structured
Streaming starts with changes arriving after the stream starts. Set it only
when intentionally replaying retained CDF history. Spark's checkpoint provides
microbatch recovery. Messages have deterministic IDs, and downstream writes
use source and pipeline versions, so retried microbatches are safe. Enabling
duplicate detection on the Service Bus topic also avoids redundant embedding
calls.

The decoded `pipeline-config-base64` value is an array with one entry per
active pipeline:

```json
[
  {
    "pipeline_id": "pip-documents",
    "pipeline_name": "Documents",
    "docgrok_pipeline": "text-embedding-model",
    "pipeline_generation": "1",
    "pipeline_revision": 1,
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
