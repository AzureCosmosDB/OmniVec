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
  "checkpoint_path": ".omnivec/checkpoints"
}
```

The watcher hashes only `content_fields`; managed fields such as `embedding`,
`content_hash`, `pipeline_id`, `pipeline_generation`, and
`omnivec_writer_marker` are never hashed. Checkpoints are stored at
`<lakehouseItemId>/Files/<checkpoint_path>/<sourceId>.json` in OneLake and
contain source-ref/content-hash idempotency state per pipeline generation.
They also store a pipeline fingerprint, so adding a pipeline or changing its
model, generation, write-back mapping, or serving mirror forces a rescan even
when the Iceberg snapshot itself has not changed.
Configure the pipeline's OneLake destination `target_table` to this same
source table (using the Spark catalog-qualified table form required by Fabric).

Create a destination with `type: "onelake-iceberg"`:

```json
{
  "workspace_id": "<workspaceId>",
  "lakehouse_item_id": "<lakehouseItemId>",
  "spark_job_definition_item_id": "<sparkJobDefinitionItemId>",
  "staging_account_url": "https://onelake.dfs.fabric.microsoft.com",
  "staging_file_system": "<workspaceId>",
  "staging_path": "<lakehouseItemId>/Files/omnivec/staging",
  "target_table": "dbo.documents",
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

Staged JSONL names are deterministic from the target table and
pipeline/source/ref/content hashes. The worker first proves the file durable,
then accepts HTTP 200/201/202 from the Fabric Jobs API as asynchronous success.
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
