"""Pure helpers shared by the OneLake Iceberg watcher and its tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq


OMNIVEC_MANAGED_FIELDS = frozenset(
    {
        "embedding",
        "embeddings",
        "content_hash",
        "pipeline_id",
        "pipeline_generation",
        "omnivec_writer",
        "omnivec_writer_marker",
        "embedded_at",
    }
)


def content_from_row(row: dict[str, Any], fields: Iterable[str]) -> str:
    """Build stable embedding input from configured user content fields only."""
    return "\n\n".join(
        str(row[field])
        for field in fields
        if field not in OMNIVEC_MANAGED_FIELDS
        and row.get(field) is not None
        and str(row[field]).strip()
    )


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_projected_rows(table: Any, projection: Iterable[str]) -> tuple[list[dict[str, Any]], bool]:
    """Read current rows, bypassing invalid Delta CDF columns in Iceberg name mappings."""
    columns = tuple(projection)
    try:
        return table.scan(selected_fields=columns).to_arrow().to_pylist(), False
    except ValueError as exc:
        if "Could not find field with name: _change_type" not in str(exc):
            raise

    tables: list[pa.Table] = []
    seen_paths: set[str] = set()
    for task in table.scan(selected_fields=columns).plan_files():
        if task.delete_files:
            raise ValueError(
                "Cannot bypass Iceberg name mapping for a scan containing delete files"
            )
        path = task.file.file_path
        if path in seen_paths:
            continue
        seen_paths.add(path)
        with table.io.new_input(path).open() as stream:
            parquet_file = pq.ParquetFile(stream)
            available = [column for column in columns if column in parquet_file.schema_arrow.names]
            current = parquet_file.read(columns=available)
        for column in columns:
            if column not in current.column_names:
                current = current.append_column(column, pa.nulls(current.num_rows))
        tables.append(current.select(columns))

    if not tables:
        return [], True
    return pa.concat_tables(tables, promote_options="default").to_pylist(), True


def ordered_source_version(timestamp_ms: int, sequence_number: int) -> int:
    """Create one ordering domain shared by Iceberg recovery and Delta CDF."""
    if timestamp_ms < 0 or sequence_number < 0:
        raise ValueError("source version components must be non-negative")
    return timestamp_ms * 1_000_000 + sequence_number


def checkpoint_key(pipeline_id: str, pipeline_revision: str, source_ref: str) -> str:
    return f"{pipeline_id}:{pipeline_revision}:{source_ref}"


def message_id(
    pipeline_id: str,
    source_id: str,
    source_ref: str,
    digest: str,
    source_version: int = 0,
) -> str:
    """Stable idempotency id for retries of the same source revision."""
    material = "\x1f".join((pipeline_id, source_id, source_ref, digest, str(source_version)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def pipeline_fingerprint(pipeline: dict[str, Any], destination: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "pipeline_id": pipeline.get("id"),
            "generation": str(pipeline.get("generation", "1")),
            "model": pipeline.get("docgrok_pipeline"),
            "destination_id": destination.get("id"),
            "writeback_columns": destination.get("config", {}).get("writeback_columns", {}),
            "mirror": destination.get("config", {}).get("mirror"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def checkpoint_json(
    snapshot_id: str | None,
    processed: dict[str, str],
    pipelines: dict[str, str] | None = None,
    submitted_at: dict[str, float] | None = None,
    known_refs: dict[str, list[str]] | None = None,
    pipeline_revisions: dict[str, int] | None = None,
    pending_empty_refs: dict[str, list[str]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "snapshot_id": snapshot_id,
            "processed": processed,
            "pipelines": pipelines or {},
            "submitted_at": submitted_at or {},
            "known_refs": known_refs or {},
            "pipeline_revisions": pipeline_revisions or {},
            "pending_empty_refs": pending_empty_refs or {},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def row_has_current_omnivec_embedding(
    row: dict[str, Any],
    digest: str,
    pipeline_id: str,
    model: str,
    generation: str,
    writeback: dict[str, str],
) -> bool:
    """Detect a completed OmniVec write-back without hashing managed fields."""
    return (
        str(row.get(writeback.get("content_hash_field", "content_hash"), "")) == digest
        and str(row.get(writeback.get("model_field", "embedding_model"), "")) == model
        and str(row.get(writeback.get("pipeline_generation_field", "pipeline_generation"), ""))
        == generation
        and str(row.get(writeback.get("pipeline_id_field", "pipeline_id"), "")) == pipeline_id
    )
