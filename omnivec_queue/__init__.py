"""Versioned queue envelopes shared by Python ingestion publishers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

MAX_ENVELOPE_BYTES = 224 * 1024
MAX_SEND_BATCH_BYTES = 240 * 1024
MAX_ENVELOPE_DOCUMENTS = 1000
MAX_SUPPORTED_ENVELOPE_BYTES = 960 * 1024
MAX_SUPPORTED_SEND_BATCH_BYTES = 1024 * 1024
SEND_BATCH_HEADROOM_BYTES = 16 * 1024
QUEUE_POLICY_FIELDS = (
    "queue_documents_per_message", "queue_max_envelope_bytes",
    "queue_send_batch_max_bytes", "queue_publish_batch_size",
)


@dataclass(frozen=True)
class QueuePublishPolicy:
    documents_per_message: int = 1
    max_envelope_bytes: int = MAX_ENVELOPE_BYTES
    send_batch_max_bytes: int = MAX_SEND_BATCH_BYTES
    publish_batch_size: int = 200


def queue_publish_policy(policy: dict[str, Any]) -> QueuePublishPolicy:
    count = queue_document_limit(policy)
    values = (
        ("queue_max_envelope_bytes", MAX_ENVELOPE_BYTES, 1024, MAX_SUPPORTED_ENVELOPE_BYTES),
        ("queue_send_batch_max_bytes", MAX_SEND_BATCH_BYTES, 1024, MAX_SUPPORTED_SEND_BATCH_BYTES),
        ("queue_publish_batch_size", 200, 1, MAX_ENVELOPE_DOCUMENTS),
    )
    settings = []
    for name, default, minimum, maximum in values:
        value = policy.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        settings.append(value)
    if settings[1] < settings[0] + SEND_BATCH_HEADROOM_BYTES:
        raise ValueError("queue_send_batch_max_bytes must reserve at least 16 KiB above queue_max_envelope_bytes")
    return QueuePublishPolicy(count, *settings)


QueueLimits = int | QueuePublishPolicy | dict[str, int | QueuePublishPolicy]


def _policy_for(limits: QueueLimits, pipeline_id: str) -> QueuePublishPolicy:
    value = limits.get(pipeline_id, 1) if isinstance(limits, dict) else limits
    if isinstance(value, QueuePublishPolicy):
        return queue_publish_policy(dict(zip(QUEUE_POLICY_FIELDS, (
            value.documents_per_message, value.max_envelope_bytes,
            value.send_batch_max_bytes, value.publish_batch_size,
        ))))
    return queue_publish_policy({"queue_documents_per_message": value})


def queue_document_limit(policy: dict[str, Any]) -> int:
    count = policy.get("queue_documents_per_message", 1)
    if type(count) is not int or not 1 <= count <= MAX_ENVELOPE_DOCUMENTS:
        raise ValueError("queue_documents_per_message must be an integer from 1 to 1000")
    return count


def _serialize(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def queue_messages(
    documents: Iterable[dict[str, Any]],
    limits: QueueLimits = 1,
) -> Iterator[tuple[dict[str, Any], bytes, int]]:
    pending: list[dict[str, Any]] = []
    route = None
    byte_count = 0
    identities: set[str] = set()
    pending_policy = QueuePublishPolicy()

    def emit() -> tuple[dict[str, Any], bytes, int]:
        if len(pending) == 1:
            body = pending[0]
        else:
            digest = hashlib.sha256(
                "\n".join(doc["message_id"] for doc in pending).encode("utf-8")
            ).hexdigest()
            body = {
                "queue_envelope_version": 1,
                "message_id": f"qe-{digest}",
                "pipeline_id": pending[0]["pipeline_id"],
                "documents": pending,
            }
        encoded = _serialize(body)
        if len(pending) > 1 and len(encoded) > pending_policy.max_envelope_bytes:
            raise ValueError("Queue envelope exceeds its serialized byte limit")
        return body, encoded, len(pending)

    for document in documents:
        policy = _policy_for(limits, document["pipeline_id"])
        count = min(policy.documents_per_message, policy.publish_batch_size)
        current_route = _serialize({
            key: document.get(key) for key in (
                "pipeline_id", "pipeline_generation", "source_id", "destination_id",
                "destination_type", "destination_config", "docgrok_pipeline",
                "message_type", "content_type", "resource_weight",
                "max_concurrency_per_worker", "resource_priority", "workload_class",
                "embed_batch_size", "model_max_batch_size",
                "model_context_window_tokens", "model_max_batch_tokens",
                "benchmark_mock_embedding", "benchmark_mock_embedding_dimensions",
            )
        })
        size = len(_serialize(document))
        # Reserve the version, routing, ID and array framing, including Unicode IDs.
        overhead = 256 + len(_serialize(document["pipeline_id"]))
        if pending and (
            route != current_route or pending_policy != policy or len(pending) >= count
            or document["message_id"] in identities
            or byte_count + size + len(pending) + overhead > policy.max_envelope_bytes
        ):
            yield emit()
            pending = []
            byte_count = 0
            identities.clear()
        pending.append(document)
        identities.add(document["message_id"])
        byte_count += size
        route = current_route
        pending_policy = policy
    if pending:
        yield emit()


async def publish_queue_documents(sender: Any, documents: Iterable[dict[str, Any]],
                                  limits: QueueLimits = 1, *,
                                  preserve_legacy_identity: bool = False) -> int:
    """Flush at the source-page boundary; return only after broker acceptance."""
    from azure.servicebus import ServiceBusMessage
    from azure.servicebus.exceptions import MessageSizeExceededError

    batch = None
    batch_policy = None
    accepted = 0
    batch_documents = 0
    for message, body, count in queue_messages(documents, limits):
        policy = _policy_for(limits, message["pipeline_id"])
        if batch is not None and (
            batch_policy != policy or batch_documents + count > policy.publish_batch_size
        ):
            if len(batch):
                await sender.send_messages(batch)
                accepted += batch_documents
            batch = None
        if batch is None:
            batch = await sender.create_message_batch(max_size_in_bytes=policy.send_batch_max_bytes)
            batch_documents = 0
            batch_policy = policy
        identity = {} if preserve_legacy_identity and policy.documents_per_message == 1 else {
            "message_id": message["message_id"], "subject": message["pipeline_id"],
            "content_type": "application/json",
        }
        outgoing = ServiceBusMessage(body, **identity)
        try:
            batch.add_message(outgoing)
        except MessageSizeExceededError:
            if len(batch) == 0:
                raise
            await sender.send_messages(batch)
            accepted += batch_documents
            batch = await sender.create_message_batch(max_size_in_bytes=policy.send_batch_max_bytes)
            batch_documents = 0
            batch.add_message(outgoing)
        batch_documents += count
    if batch is not None and len(batch):
        await sender.send_messages(batch)
        accepted += batch_documents
    return accepted
