"""Synthetic benchmark message construction and paced Service Bus publishing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from omnivec_queue import (
    publish_queue_documents, queue_publish_policy,
)


log = logging.getLogger(__name__)


def _content_seed(index: int, pattern: str) -> str:
    if pattern == "repeated":
        return "synthetic benchmark document "
    digest = hashlib.sha256(f"synthetic:{index}".encode()).hexdigest()
    return f"synthetic {index:010d} {digest} "


def build_synthetic_message(
    *,
    source: dict[str, Any],
    pipeline: dict[str, Any],
    destination: dict[str, Any],
    run_id: str,
    index: int,
    message_size_bytes: int,
    content_pattern: str,
    mock_embedding: bool = False,
    mock_embedding_dimensions: int = 384,
) -> tuple[dict[str, Any], bytes]:
    """Build a deterministic worker message with an exact UTF-8 body size."""
    source_ref = f"{run_id}-{index:010d}"
    message_id = hashlib.sha256(
        f"{pipeline['id']}:{source['id']}:{source_ref}".encode()
    ).hexdigest()
    policy = pipeline.get("resource_policy") or {}
    limits = dict(pipeline.get("model_limits") or {})
    if mock_embedding:
        mock_batch_size = policy.get("embed_batch_size", 50)
        if type(mock_batch_size) is not int or not 1 <= mock_batch_size <= 4096:
            raise ValueError("embed_batch_size must be an integer from 1 to 4096")
        limits["max_batch_size"] = mock_batch_size
    message = {
        "message_id": message_id,
        "pipeline_id": pipeline["id"],
        "pipeline_name": pipeline["name"],
        "docgrok_pipeline": pipeline["docgrok_pipeline"],
        "source_id": source["id"],
        "source_ref": source_ref,
        "destination_id": destination["id"],
        "destination_type": destination["type"],
        "destination_config": destination["config"],
        "content": "",
        "content_hash": "0" * 64,
        "source_version": index + 1,
        "pipeline_revision": 1,
        "partition_key_value": source_ref,
        "partition_key_pattern": pipeline.get(
            "partition_key_pattern", "{source_partition}",
        ),
        "doc_id_pattern": pipeline.get(
            "doc_id_pattern", "{source_hash}-{pipeline}",
        ),
        "pipeline_generation": str(pipeline.get("generation", "1")),
        "resource_weight": policy.get("weight", 10),
        "max_concurrency_per_worker": policy.get(
            "max_concurrency_per_worker", 2,
        ),
        "embed_batch_size": policy.get("embed_batch_size", 50),
        "resource_priority": policy.get("priority", "normal"),
        "workload_class": policy.get("workload_class", "shared"),
        "model_max_batch_size": limits.get("max_batch_size", 256),
        "model_context_window_tokens": limits.get(
            "context_window_tokens", 8191,
        ),
        "model_max_batch_tokens": limits.get("max_batch_tokens", 6000),
    }
    if mock_embedding:
        message["benchmark_mock_embedding"] = True
        if mock_embedding_dimensions != 384:
            message["benchmark_mock_embedding_dimensions"] = (
                mock_embedding_dimensions
            )
    if pipeline.get("content_strategy", "truncate") != "truncate":
        message["content_strategy"] = pipeline["content_strategy"]
    if pipeline.get("chunk_config") is not None:
        message["chunk_config"] = pipeline["chunk_config"]
    if pipeline.get("store_content") is not None:
        message["store_content"] = pipeline["store_content"]
    if pipeline.get("content_field"):
        message["content_field"] = pipeline["content_field"]
    if pipeline.get("metadata_fields") is not None:
        message["metadata_fields"] = pipeline["metadata_fields"]

    empty_body = json.dumps(
        message, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    content_bytes = message_size_bytes - len(empty_body)
    if content_bytes < 1:
        raise ValueError(
            f"message_size_bytes must be at least {len(empty_body) + 1} "
            "for this pipeline and destination configuration"
        )

    seed = _content_seed(index, content_pattern)
    content = (seed * ((content_bytes // len(seed)) + 1))[:content_bytes]
    message["content"] = content
    message["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
    body = json.dumps(
        message, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    if len(body) != message_size_bytes:
        raise ValueError(
            f"Could not construct exact {message_size_bytes}-byte message"
        )
    return message, body


async def publish_synthetic_messages(
    *,
    namespace: str,
    topic_name: str,
    source: dict[str, Any],
    pipeline: dict[str, Any],
    destination: dict[str, Any],
    run_id: str,
    document_count: int,
    message_size_bytes: int,
    docs_per_second: float,
    content_pattern: str,
    mock_embedding: bool,
    mock_embedding_dimensions: int,
    progress: Callable[[int], Awaitable[None]],
    refresh_resource_policy: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Publish a globally paced run through the normal embedding topic."""
    credential = DefaultAzureCredential()
    published = 0
    started = time.monotonic()
    progress_interval = max(1, min(1000, document_count // 100 or 1))
    next_progress = progress_interval
    queue_policy = queue_publish_policy(pipeline.get("resource_policy") or {})
    next_refresh = started + 5
    try:
        async with ServiceBusClient(namespace, credential) as client:
            sender = client.get_topic_sender(topic_name)
            async with sender:
                while published < document_count:
                    if docs_per_second > 0:
                        target = started + published / docs_per_second
                        delay = target - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    if refresh_resource_policy is not None and time.monotonic() >= next_refresh:
                        resource_policy = await refresh_resource_policy()
                        updated = queue_publish_policy(resource_policy)
                        if resource_policy != (pipeline.get("resource_policy") or {}):
                            log.info("Synthetic run %s live capacity policy changed at document %s: %s",
                                     run_id, published, resource_policy)
                        queue_policy = updated
                        pipeline = {**pipeline, "resource_policy": resource_policy}
                        next_refresh = time.monotonic() + 5

                    publish_batch_limit = queue_policy.publish_batch_size
                    if docs_per_second > 0:
                        publish_batch_limit = max(1, min(publish_batch_limit, math.ceil(docs_per_second / 10)))
                    publish_batch_limit = min(publish_batch_limit, max(
                        1, (queue_policy.send_batch_max_bytes - 2048) // message_size_bytes,
                    ))
                    documents = []
                    for index in range(published, min(document_count, published + publish_batch_limit)):
                        message, _ = build_synthetic_message(
                            source=source,
                            pipeline=pipeline,
                            destination=destination,
                            run_id=run_id,
                            index=index,
                            message_size_bytes=message_size_bytes,
                            content_pattern=content_pattern,
                            mock_embedding=mock_embedding,
                            mock_embedding_dimensions=mock_embedding_dimensions,
                        )
                        documents.append(message)
                    published += await publish_queue_documents(sender, documents, queue_policy)
                    if published == document_count or published >= next_progress:
                        await progress(published)
                        while next_progress <= published:
                            next_progress += progress_interval
    finally:
        await credential.close()
