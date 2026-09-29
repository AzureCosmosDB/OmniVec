"""Pure message-building helpers for the OneLake Delta CDF streaming job."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any


def content_from_record(record: dict[str, Any], fields: list[str]) -> str:
    return "\n\n".join(
        str(record[field])
        for field in fields
        if record.get(field) is not None and str(record[field]).strip()
    )


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ordered_source_version(commit_timestamp: datetime, commit_version: int) -> int:
    timestamp_ms = int(commit_timestamp.timestamp() * 1000)
    if timestamp_ms < 0 or commit_version < 0:
        raise ValueError("source version components must be non-negative")
    return timestamp_ms * 1_000_000 + commit_version


def message_id(
    pipeline_id: str,
    source_id: str,
    source_ref: str,
    digest: str,
    source_version: int,
) -> str:
    material = "\x1f".join(
        (pipeline_id, source_id, source_ref, digest, str(source_version))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_current_writeback(
    record: dict[str, Any],
    digest: str,
    plan: dict[str, Any],
) -> bool:
    if plan.get("destination_type") != "onelake-iceberg":
        return False
    columns = {
        "content_hash_field": "content_hash",
        "pipeline_id_field": "pipeline_id",
        "pipeline_generation_field": "pipeline_generation",
        "model_field": "embedding_model",
        **plan.get("writeback_columns", {}),
    }
    return (
        str(record.get(columns["content_hash_field"], "")) == digest
        and str(record.get(columns["pipeline_id_field"], "")) == plan["pipeline_id"]
        and str(record.get(columns["pipeline_generation_field"], ""))
        == str(plan.get("pipeline_generation", "1"))
        and str(record.get(columns["model_field"], "")) == plan["docgrok_pipeline"]
    )


def build_messages(
    record: dict[str, Any],
    change_type: str,
    source_version: int,
    plans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert one Delta CDF row into OmniVec worker messages."""
    if change_type not in {"insert", "update_postimage", "delete"}:
        return []

    messages: list[dict[str, Any]] = []
    for plan in plans:
        id_field = plan.get("id_field", "id")
        source_ref = str(record.get(id_field, ""))
        if not source_ref:
            continue

        fields = plan.get("content_fields") or ["content"]
        content = content_from_record(record, fields)
        deleting = change_type == "delete" or not content
        digest = content_hash(f"delete:{source_ref}") if deleting else content_hash(content)
        if not deleting and _is_current_writeback(record, digest, plan):
            continue

        message = {
            "message_id": message_id(
                plan["pipeline_id"],
                plan["source_id"],
                source_ref,
                digest,
                source_version,
            ),
            "pipeline_id": plan["pipeline_id"],
            "pipeline_name": plan["pipeline_name"],
            "docgrok_pipeline": plan["docgrok_pipeline"],
            "source_id": plan["source_id"],
            "source_ref": source_ref,
            "destination_id": plan["destination_id"],
            "destination_type": plan["destination_type"],
            "destination_config": plan["destination_config"],
            "content": "" if deleting else content,
            "content_hash": digest,
            "source_version": source_version,
            "pipeline_revision": int(plan.get("pipeline_revision", 1)),
            "partition_key_value": source_ref,
            "partition_key_pattern": plan.get(
                "partition_key_pattern", "{source_partition}"
            ),
            "doc_id_pattern": plan.get(
                "doc_id_pattern", "{source_hash}-{pipeline}"
            ),
            "pipeline_generation": str(plan.get("pipeline_generation", "1")),
            "source_content_fields": (
                {}
                if deleting
                else {
                    field: str(record[field])
                    for field in fields
                    if record.get(field) is not None
                }
            ),
        }
        if deleting:
            message["message_type"] = "delete"
        messages.append(message)
    return messages
