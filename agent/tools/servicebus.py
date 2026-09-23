"""Service Bus depth tools (read-only via management API)."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from . import tool


class _SBClient:
    """Module-level facade — overridden in tests."""

    async def queue_depth(self, fqns: str, queue: str) -> dict:  # pragma: no cover
        _check_scope(fqns, queue)
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio.management import ServiceBusAdministrationClient
        async with DefaultAzureCredential() as credential:
            async with ServiceBusAdministrationClient(fqns, credential) as client:
                p = await client.get_queue_runtime_properties(queue)
                return {
                    "queue": queue,
                    "active_message_count": p.active_message_count,
                    "dead_letter_message_count": p.dead_letter_message_count,
                    "total_message_count": p.total_message_count,
                    "scheduled_message_count": p.scheduled_message_count,
                    "transfer_dead_letter_message_count": p.transfer_dead_letter_message_count,
                }

    async def list_topics(self, fqns: str) -> list[str]:  # pragma: no cover
        _check_scope(fqns)
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio.management import ServiceBusAdministrationClient
        async with DefaultAzureCredential() as credential:
            async with ServiceBusAdministrationClient(fqns, credential) as client:
                topics = []
                async for topic in client.list_topics():
                    topics.append(topic.name)
                    if len(topics) >= 100:
                        break
                return topics

    async def subscription_depth(self, fqns: str, topic: str, subscription: str) -> dict:  # pragma: no cover
        _check_scope(fqns)
        if (topic, subscription) not in configured_subscriptions():
            raise ValueError("subscription is not in AGENT_SB_SUBSCRIPTIONS")
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio.management import ServiceBusAdministrationClient
        async with DefaultAzureCredential() as credential:
            async with ServiceBusAdministrationClient(fqns, credential) as client:
                p = await client.get_subscription_runtime_properties(topic, subscription)
                return {
                    "topic": topic, "subscription": subscription,
                    "active_message_count": p.active_message_count,
                    "dead_letter_message_count": p.dead_letter_message_count,
                    "total_message_count": p.total_message_count,
                    "transfer_dead_letter_message_count": p.transfer_dead_letter_message_count,
                }

    # --- Phase 2 mutating shims --------------------------------------------
    async def retry_dlq(self, fqns: str, queue: str, max_messages: int) -> dict:  # pragma: no cover
        # Real implementation: read from {queue}/$DeadLetterQueue and re-publish
        # to {queue}. Kept as a stub so tests can override; production code in
        # the worker handles the actual replay.
        raise RuntimeError("DLQ replay adapter is not configured; no messages were moved. Fix the poison-message cause and use the approved operator replay runbook.")

    async def purge_dlq(self, fqns: str, queue: str) -> dict:  # pragma: no cover
        raise RuntimeError("DLQ purge adapter is not configured; no messages were deleted")


def configured_queues() -> list[str]:
    return list(dict.fromkeys(q.strip() for q in os.getenv("AGENT_QUEUE_NAMES", "").split(",") if q.strip()))[:8]


def configured_subscriptions() -> list[tuple[str, str]]:
    return list(dict.fromkeys(
        tuple(s.strip().split("/")) for s in os.getenv("AGENT_SB_SUBSCRIPTIONS", "").split(",")
        if s.strip().count("/") == 1 and all(s.strip().split("/"))
    ))[:8]


def _check_scope(fqns: str, queue: str | None = None) -> None:
    configured = os.getenv("SERVICEBUS_FQNS", "").strip()
    if not configured or fqns != configured:
        raise ValueError("Service Bus namespace is unavailable or outside configured scope")
    if queue is not None and queue not in configured_queues():
        raise ValueError("queue is not in AGENT_QUEUE_NAMES")


_SB: _SBClient = _SBClient()


def _fqns(override: str | None) -> str:
    return (override or os.environ.get("SERVICEBUS_FQNS", "")).strip()


class _QueueRef(BaseModel):
    queue: str = Field(..., min_length=1)
    namespace_fqns: str | None = Field(default=None, description="FQNS override.")


class _NSRef(BaseModel):
    namespace_fqns: str | None = None


class _SubscriptionRef(_NSRef):
    topic: str = Field(..., min_length=1)
    subscription: str = Field(..., min_length=1)


@tool("get_subscription_depth", "Actual active/DLQ counters for a configured embedding topic subscription.", _SubscriptionRef)
async def get_subscription_depth(p: _SubscriptionRef, **_ctx) -> Any:
    return await _SB.subscription_depth(_fqns(p.namespace_fqns), p.topic, p.subscription)


@tool("get_queue_depth", "Active + dead-lettered message counts for a Service Bus queue.", _QueueRef)
async def get_queue_depth(p: _QueueRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    return await _SB.queue_depth(fqns, p.queue)


@tool("get_dlq_count", "Dead-letter count for a Service Bus queue (shortcut over get_queue_depth).", _QueueRef)
async def get_dlq_count(p: _QueueRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    rt = await _SB.queue_depth(fqns, p.queue)
    if "dead_letter_message_count" not in rt:
        return {"queue": p.queue, "status": "UNKNOWN", "error": "DLQ observation unavailable"}
    return {"queue": p.queue, "dead_letter_message_count": rt["dead_letter_message_count"]}


@tool("list_topics", "List topics in the Service Bus namespace.", _NSRef)
async def list_topics(p: _NSRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    return {"topics": await _SB.list_topics(fqns)}
