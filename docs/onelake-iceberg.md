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
    "type": "redis",
    "destination_id": "optional-serving-destination-id",
    "best_effort": false,
    "config": {
      "endpoint": "<managed-redis-host>:10000",
      "use_entra_auth": true,
      "tls": true,
      "key_prefix": "omnivec",
      "ttl_seconds": 86400
    }
  }
}
```

Staged JSONL names are deterministic from the target table and
pipeline/source/ref/content hashes. The worker first proves the file durable,
then accepts HTTP 200/201/202 from the Fabric Jobs API as asynchronous success.
The Spark job refuses to insert a missing source row: it updates only the
configured `id_field` in the existing target/source table. When configured, the
Redis/Garnet mirror uses deterministic keys
`<prefix>:<pipelineId>:<sourceId>:<sourceRef>`. It defaults to TLS and
Microsoft Entra authentication. Mirror failures fail the Service Bus batch
unless `mirror.best_effort` is true. To mirror to Cosmos DB vector instead,
set `mirror.type` to `cosmosdb-vector` and provide the usual existing
Cosmos destination `endpoint`, `database`, `container`, and `vector_field`
configuration in `mirror.config`; it reuses the worker's Cosmos writer.

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
Send/Receive, Fabric job execution permission, and Azure Managed Redis data
access when mirroring is enabled. Enable the watcher only after assigning these
permissions:

```bash
helm upgrade --install omnivec helm/omnivec \
  --set onelakeIcebergWatcher.enabled=true
```
