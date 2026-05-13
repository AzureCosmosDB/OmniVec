"""Service Bus depth tools (read-only via management API)."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from . import tool


class _SBClient:
    """Module-level facade — overridden in tests."""

    async def queue_depth(self, fqns: str, queue: str) -> dict:  # pragma: no cover
        return {"queue": queue, "active_message_count": 0, "dead_letter_message_count": 0, "total_message_count": 0}

    async def list_topics(self, fqns: str) -> list[str]:  # pragma: no cover
        return []

    # --- Phase 2 mutating shims --------------------------------------------
    async def retry_dlq(self, fqns: str, queue: str, max_messages: int) -> dict:  # pragma: no cover
        # Real implementation: read from {queue}/$DeadLetterQueue and re-publish
        # to {queue}. Kept as a stub so tests can override; production code in
        # the worker handles the actual replay.
        return {"queue": queue, "replayed": 0, "requested": max_messages, "stub": True}

    async def purge_dlq(self, fqns: str, queue: str) -> dict:  # pragma: no cover
        return {"queue": queue, "purged": 0, "stub": True}


_SB: _SBClient = _SBClient()


def _fqns(override: str | None) -> str:
    return (override or os.environ.get("SERVICEBUS_FQNS", "")).strip()


class _QueueRef(BaseModel):
    queue: str = Field(..., min_length=1)
    namespace_fqns: str | None = Field(default=None, description="FQNS override.")


class _NSRef(BaseModel):
    namespace_fqns: str | None = None


@tool("get_queue_depth", "Active + dead-lettered message counts for a Service Bus queue.", _QueueRef)
async def get_queue_depth(p: _QueueRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    return await _SB.queue_depth(fqns, p.queue)


@tool("get_dlq_count", "Dead-letter count for a Service Bus queue (shortcut over get_queue_depth).", _QueueRef)
async def get_dlq_count(p: _QueueRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    rt = await _SB.queue_depth(fqns, p.queue)
    return {"queue": p.queue, "dead_letter_message_count": int(rt.get("dead_letter_message_count", 0))}


@tool("list_topics", "List topics in the Service Bus namespace.", _NSRef)
async def list_topics(p: _NSRef, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    return {"topics": await _SB.list_topics(fqns)}
