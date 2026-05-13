"""Lightweight metrics tools (wrap api.py /api/metrics)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from . import tool
from .omnivec_api import _get


class _N(BaseModel):
    n: int = Field(default=20, ge=1, le=500)


class _Empty(BaseModel):
    pass


def _extract_errors(snapshot: dict, n: int) -> list[dict]:
    errs = snapshot.get("recent_errors") or snapshot.get("errors") or []
    return errs[:n] if isinstance(errs, list) else []


def _extract_latency(snapshot: dict) -> dict:
    lat = snapshot.get("latency") or {}
    return {"p50_ms": lat.get("p50"), "p95_ms": lat.get("p95"), "p99_ms": lat.get("p99")}


def _extract_throughput(snapshot: dict) -> dict:
    return {
        "requests_per_minute": snapshot.get("requests_per_minute"),
        "embeddings_per_minute": snapshot.get("embeddings_per_minute"),
        "errors_per_minute": snapshot.get("errors_per_minute"),
    }


@tool("recent_errors_last_n", "Return up to N most-recent error entries from the metrics store.", _N)
async def recent_errors_last_n(p: _N, **_ctx) -> Any:
    snap = await _get("/api/metrics")
    return {"errors": _extract_errors(snap or {}, p.n)}


@tool("latency_p99_last_hour", "Return p50/p95/p99 latency for the last hour.", _Empty)
async def latency_p99_last_hour(_p: _Empty, **_ctx) -> Any:
    snap = await _get("/api/metrics")
    return _extract_latency(snap or {})


@tool("throughput_last_hour", "Return request / embedding / error throughput for the last hour.", _Empty)
async def throughput_last_hour(_p: _Empty, **_ctx) -> Any:
    snap = await _get("/api/metrics")
    return _extract_throughput(snap or {})
