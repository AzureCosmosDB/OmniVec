"""Pending-approval store for the OmniVec Agent.

Phase 2 mutating tools (``readonly=False``) don't execute when the LLM
proposes them; the agent first pushes an ``approval_required`` SSE event,
parks the call here, and ends the turn. The web UI shows an Approve / Deny
card; the next HTTP call (``POST /v1/chat/approve``) pops the parked record
and either executes the tool or feeds the denial back to the LLM.

The store is **in-memory + TTL** for now. That's adequate because pending
approvals are short-lived (operator decides in seconds) and surviving an
agent pod restart is not a hard requirement — a restart simply forces the
user to re-issue the request. A Cosmos-backed implementation can drop in
later without changing the interface.

Concurrency: ``asyncio.Lock`` guards all mutations.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any


# 15 minutes is plenty for a human to click a button; old entries auto-expire
# so the dict can't grow unboundedly under a buggy / abandoned UI.
DEFAULT_TTL_SECONDS = 15 * 60


def _now() -> float:
    return _dt.datetime.utcnow().timestamp()


@dataclass
class PendingApproval:
    """A single mutating tool call awaiting human approval."""
    session_id: str
    call_id: str
    user_id: str
    role: str
    tool_name: str
    args: dict
    danger_level: str  # "low" | "medium" | "high"
    summary: str
    # Conversation context needed to resume the loop after the decision.
    # ``history`` is the full messages list INCLUDING the assistant turn that
    # proposed the tool_call — resume just appends the tool_result.
    history: list[dict] = field(default_factory=list)
    # The raw OpenAI-style tool_call dict ({"id","type","function":{...}}) so
    # we can execute it with the original argument string after approval.
    tool_call: dict = field(default_factory=dict)
    model_id: str | None = None
    created_at: float = field(default_factory=_now)


class _InMemoryApprovalStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._data: dict[tuple[str, str], PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    async def put(self, p: PendingApproval) -> None:
        async with self._lock:
            self._prune_locked()
            self._data[(p.session_id, p.call_id)] = p

    async def pop(self, session_id: str, call_id: str) -> PendingApproval | None:
        async with self._lock:
            self._prune_locked()
            return self._data.pop((session_id, call_id), None)

    async def get(self, session_id: str, call_id: str) -> PendingApproval | None:
        async with self._lock:
            self._prune_locked()
            return self._data.get((session_id, call_id))

    async def list_for_session(self, session_id: str) -> list[PendingApproval]:
        async with self._lock:
            self._prune_locked()
            return [p for (sid, _), p in self._data.items() if sid == session_id]

    def _prune_locked(self) -> None:
        cutoff = _now() - self._ttl
        stale = [k for k, v in self._data.items() if v.created_at < cutoff]
        for k in stale:
            self._data.pop(k, None)


_APPROVALS: _InMemoryApprovalStore = _InMemoryApprovalStore()


def get_approvals_store() -> _InMemoryApprovalStore:
    return _APPROVALS


def reset_approvals_store_for_tests() -> None:
    global _APPROVALS
    _APPROVALS = _InMemoryApprovalStore()
