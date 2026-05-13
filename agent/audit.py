"""Audit log writer for agent tool invocations.

One document per tool call goes into the ``agent_audit`` container:

    {
      "id": "<uuid>",
      "doc_type": "agent_audit",
      "session_id": "<session>",
      "user_id": "<caller>",
      "role": "reader|admin",
      "tool_name": "...",
      "args": {...},
      "result_summary": "...",
      "ts": "<iso>",
      "reasoning_trace_snippet": ""
    }

The default implementation is in-memory (and exposed for tests) — production
swaps in a Cosmos-backed writer at startup.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from dataclasses import dataclass, field


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


@dataclass
class AuditWriter:
    """In-memory async-safe writer used by tests and local dev."""

    entries: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(
        self,
        *,
        session_id: str,
        user: str,
        role: str,
        tool_name: str,
        args: dict,
        result_summary: str,
        reasoning_trace_snippet: str = "",
    ) -> dict:
        doc = {
            "id": str(uuid.uuid4()),
            "doc_type": "agent_audit",
            "session_id": session_id,
            "user_id": user,
            "role": role,
            "tool_name": tool_name,
            "args": args,
            "result_summary": result_summary,
            "ts": _now_iso(),
            "reasoning_trace_snippet": reasoning_trace_snippet,
        }
        async with self._lock:
            self.entries.append(doc)
        return doc

    async def list(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        async with self._lock:
            rows = list(self.entries)
        if session_id:
            rows = [r for r in rows if r["session_id"] == session_id]
        return rows[-limit:]


_AUDIT: AuditWriter = AuditWriter()


def get_audit_writer() -> AuditWriter:
    return _AUDIT


def reset_audit_writer_for_tests() -> None:
    global _AUDIT
    _AUDIT = AuditWriter()
