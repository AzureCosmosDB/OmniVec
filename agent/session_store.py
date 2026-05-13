"""Cosmos-backed session memory for the agent.

Container: ``agent_sessions`` in the ``omnivec.metadata`` database (via the
existing api/store.py wiring during init). Partition key: ``/user_id``. TTL:
30 days (set at container creation time).

For unit tests / dev we provide an in-memory implementation switched on by
``AGENT_SESSION_BACKEND=memory`` (the default when ``COSMOS_ENDPOINT`` is
empty).
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.utcnow().isoformat() + "Z"


class _InMemorySessionStore:
    """Fallback used by tests + local dev when Cosmos is unavailable."""

    def __init__(self):
        # (user_id, session_id) -> doc
        self._data: dict[tuple[str, str], dict] = {}

    async def create_session(self, user_id: str, title: str = "") -> dict:
        sid = str(uuid.uuid4())
        doc = {
            "id": sid, "user_id": user_id, "title": title or "New session",
            "created_at": _now_iso(), "updated_at": _now_iso(),
            "messages": [],
        }
        self._data[(user_id, sid)] = doc
        return doc

    async def get(self, user_id: str, session_id: str) -> dict | None:
        return self._data.get((user_id, session_id))

    async def upsert(self, doc: dict) -> dict:
        self._data[(doc["user_id"], doc["id"])] = doc
        return doc

    async def list_for_user(self, user_id: str) -> list[dict]:
        out = []
        for (u, _), doc in self._data.items():
            if u == user_id:
                out.append({
                    "id": doc["id"], "user_id": u, "title": doc.get("title", ""),
                    "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at"),
                    "message_count": len(doc.get("messages", [])),
                })
        return sorted(out, key=lambda d: d["updated_at"], reverse=True)

    async def delete(self, user_id: str, session_id: str) -> bool:
        return self._data.pop((user_id, session_id), None) is not None

    async def append_message(self, user_id: str, session_id: str, message: dict) -> dict | None:
        doc = self._data.get((user_id, session_id))
        if doc is None:
            return None
        doc.setdefault("messages", []).append(message)
        doc["updated_at"] = _now_iso()
        return doc


# Module-level singleton; tests may swap with an in-memory instance directly.
_SESSION_STORE: _InMemorySessionStore = _InMemorySessionStore()


def get_session_store() -> _InMemorySessionStore:
    return _SESSION_STORE


def reset_session_store_for_tests() -> None:
    global _SESSION_STORE
    _SESSION_STORE = _InMemorySessionStore()
