"""Pure helpers shared by the OneLake Iceberg watcher and its tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


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


def checkpoint_key(pipeline_id: str, pipeline_revision: str, source_ref: str) -> str:
    return f"{pipeline_id}:{pipeline_revision}:{source_ref}"


def message_id(pipeline_id: str, source_id: str, source_ref: str, digest: str) -> str:
    """Stable idempotency id for retries of the same source revision."""
    material = "\x1f".join((pipeline_id, source_id, source_ref, digest))
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
) -> bytes:
    return json.dumps(
        {
            "snapshot_id": snapshot_id,
            "processed": processed,
            "pipelines": pipelines or {},
            "submitted_at": submitted_at or {},
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
