"""Cosmos diagnostic tools (read-only)."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from . import tool


METADATA_DB = os.environ.get("COSMOS_METADATA_DB", "omnivec")

_ALLOWED_CONTAINERS = {
    "metadata", "agent_sessions", "agent_audit", "leases", "video_state",
}


class _CosmosClientFacade:
    """Module-level facade tests can monkey-patch."""

    async def count(self, database: str, container: str) -> int:  # pragma: no cover
        return 0

    async def get(self, database: str, container: str, doc_id: str, partition_key: str) -> dict | None:  # pragma: no cover
        return None

    async def query(self, database: str, container: str, sql: str, parameters: list | None) -> list[dict]:  # pragma: no cover
        return []


_COSMOS: _CosmosClientFacade = _CosmosClientFacade()


def _check_container(name: str) -> None:
    if name not in _ALLOWED_CONTAINERS:
        raise ValueError(f"container {name!r} is not in the agent's read-only allow-list")


class _ContainerRef(BaseModel):
    container: str = Field(..., description="Container name (must be in the agent's allow-list).")


class _DocRef(_ContainerRef):
    doc_id: str = Field(..., min_length=1)
    partition_key: str = Field(..., min_length=1)


class _QueryRef(_ContainerRef):
    sql: str = Field(..., min_length=1, description="Read-only SELECT statement.")
    parameters: list[dict] | None = None


@tool("count_docs_in_container", "Return the count of documents in a metadata container.", _ContainerRef)
async def count_docs_in_container(p: _ContainerRef, **_ctx) -> Any:
    _check_container(p.container)
    return {"container": p.container, "count": await _COSMOS.count(METADATA_DB, p.container)}


@tool("get_doc_by_id", "Read a single document by id + partition_key from an allow-listed container.", _DocRef)
async def get_doc_by_id(p: _DocRef, **_ctx) -> Any:
    _check_container(p.container)
    return await _COSMOS.get(METADATA_DB, p.container, p.doc_id, p.partition_key) or {}


@tool("query_diag", "Run a read-only SELECT against an allow-listed container (returns ≤100 rows).", _QueryRef)
async def query_diag(p: _QueryRef, **_ctx) -> Any:
    _check_container(p.container)
    sql = p.sql.lstrip()
    if not sql.upper().startswith("SELECT"):
        raise ValueError("query_diag only accepts SELECT statements")
    return {"rows": await _COSMOS.query(METADATA_DB, p.container, p.sql, p.parameters)}
