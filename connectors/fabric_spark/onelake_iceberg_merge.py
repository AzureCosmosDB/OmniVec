"""Fabric Spark Job Definition entry point for OmniVec OneLake Iceberg batches.

Pass ``--staging-path`` as the durable JSONL file written by the .NET worker
and ``--target-table`` as a catalog-qualified Iceberg table identifier.
"""

import argparse
import base64
import json
import re

from pyspark.sql import SparkSession, functions as F


WRITER_MARKER = "omnivec-onelake-iceberg-v1"
DEFAULT_COLUMNS = {
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
    "embedded_at_field": "embedded_at",
}


def quoted(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"Unsafe configured write-back column: {identifier!r}")
    return f"`{identifier}`"


def quoted_table(identifier: str) -> str:
    parts = identifier.split(".")
    if not parts or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise ValueError(f"Unsafe configured target table: {identifier!r}")
    return ".".join(quoted(part) for part in parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-path", required=True)
    parser.add_argument("--target-table", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--writer-marker", default=WRITER_MARKER)
    parser.add_argument("--writeback-columns-base64", default="e30=")
    args = parser.parse_args()
    columns = {
        **DEFAULT_COLUMNS,
        **json.loads(base64.b64decode(args.writeback_columns_base64).decode("utf-8")),
    }
    target_table = quoted_table(args.target_table)

    spark = SparkSession.builder.getOrCreate()
    staged = (
        spark.read.json(args.staging_path)
        .filter(F.col("omnivec_writer_marker") == F.lit(args.writer_marker))
        .filter(F.col("omnivec_run_id") == F.lit(args.run_id))
        .select(
            "id",
            "source_id",
            "source_ref",
            "content_hash",
            "pipeline_id",
            "pipeline_generation",
            "model",
            "embedding",
            "content",
            F.to_json("source_content_fields").alias("source_content_fields"),
            "omnivec_writer_marker",
            "omnivec_run_id",
        )
        .dropDuplicates(["pipeline_id", "source_id", "source_ref"])
    )
    target = spark.table(target_table)
    source_ids = staged.select("id").distinct()
    missing = source_ids.join(
        target.select(F.col(columns["id_field"]).cast("string").alias("id")),
        "id",
        "left_anti",
    ).limit(1)
    if missing.count():
        raise RuntimeError("Refusing OneLake write-back: a staged source row does not exist in target_table")

    staged.createOrReplaceTempView("omnivec_staged_embeddings")
    id_field = quoted(columns["id_field"])
    content_hash_field = quoted(columns["content_hash_field"])
    pipeline_id_field = quoted(columns["pipeline_id_field"])
    pipeline_generation_field = quoted(columns["pipeline_generation_field"])
    model_field = quoted(columns["model_field"])
    source_id_field = quoted(columns["source_id_field"])
    source_ref_field = quoted(columns["source_ref_field"])
    writer_marker_field = quoted(columns["writer_marker_field"])
    run_id_field = quoted(columns["run_id_field"])
    embedding_field = quoted(columns["embedding_field"])
    embedded_at_field = quoted(columns["embedded_at_field"])

    # Write back to the existing source row only. Replaying an accepted Fabric
    # job is harmless because the hash/model/generation predicate skips it.
    spark.sql(
        f"""
        MERGE INTO {target_table} AS target
        USING omnivec_staged_embeddings AS source
        ON CAST(target.{id_field} AS STRING) = source.id
        WHEN MATCHED AND (
          NOT (target.{content_hash_field} <=> source.content_hash)
          OR NOT (target.{model_field} <=> source.model)
          OR NOT (target.{pipeline_generation_field} <=> source.pipeline_generation)
          OR NOT (target.{pipeline_id_field} <=> source.pipeline_id)
        ) THEN UPDATE SET
          {embedding_field} = source.embedding,
          {content_hash_field} = source.content_hash,
          {pipeline_id_field} = source.pipeline_id,
          {pipeline_generation_field} = source.pipeline_generation,
          {model_field} = source.model,
          {source_id_field} = source.source_id,
          {source_ref_field} = source.source_ref,
          {writer_marker_field} = source.omnivec_writer_marker,
          {run_id_field} = source.omnivec_run_id,
          {embedded_at_field} = current_timestamp()
        """
    )


if __name__ == "__main__":
    main()
