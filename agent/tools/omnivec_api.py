"""Read-only tools wrapping the OmniVec control-plane REST API."""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from . import tool


OMNIVEC_API_URL = os.environ.get("OMNIVEC_API_URL", "http://omnivec-api")

_HTTP_CLIENT: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _HTTP_CLIENT


async def _get(path: str, params: dict | None = None) -> Any:
    """GET ``{OMNIVEC_API_URL}{path}`` and return parsed JSON."""
    client = _get_client()
    url = OMNIVEC_API_URL.rstrip("/") + path
    # Host header matches api.py's internal-call allowlist (no auth needed).
    headers = {"Host": "omnivec-api"}
    resp = await client.get(url, params=params or None, headers=headers)
    resp.raise_for_status()
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return {"text": resp.text}


class _Empty(BaseModel):
    pass


class _SourceId(BaseModel):
    source_id: str = Field(..., min_length=1, description="Source identifier")


class _DestinationId(BaseModel):
    destination_id: str = Field(..., min_length=1)


class _PipelineId(BaseModel):
    pipeline_id: str = Field(..., min_length=1)


class _ModelId(BaseModel):
    model_id: str = Field(..., min_length=1)


class _JobId(BaseModel):
    job_id: str = Field(..., min_length=1)


class _AuditFilter(BaseModel):
    actor: str | None = None
    path_prefix: str | None = None
    method: str | None = None
    since: str | None = None
    limit: int = Field(default=50, ge=1, le=1000)


@tool("list_sources", "List all configured data sources.", _Empty)
async def list_sources(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/sources")


@tool("get_source", "Return a single source by id.", _SourceId)
async def get_source(p: _SourceId, **_ctx) -> Any:
    return await _get(f"/api/sources/{p.source_id}")


@tool("list_destinations", "List all configured destinations.", _Empty)
async def list_destinations(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/destinations")


@tool("get_destination", "Return a single destination by id.", _DestinationId)
async def get_destination(p: _DestinationId, **_ctx) -> Any:
    return await _get(f"/api/destinations/{p.destination_id}")


@tool("list_pipelines", "List all pipelines and their headline status.", _Empty)
async def list_pipelines(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/pipelines")


@tool("get_pipeline", "Return a single pipeline by id.", _PipelineId)
async def get_pipeline(p: _PipelineId, **_ctx) -> Any:
    return await _get(f"/api/pipelines/{p.pipeline_id}")


@tool("get_pipeline_status", "Return the current status of a pipeline.", _PipelineId)
async def get_pipeline_status(p: _PipelineId, **_ctx) -> Any:
    # The control-plane has no /status sub-route; the parent pipeline document
    # already carries status + processing_mode + generation, so project those.
    pipe = await _get(f"/api/pipelines/{p.pipeline_id}")
    return {
        "id": pipe.get("id"),
        "name": pipe.get("name"),
        "status": pipe.get("status"),
        "processing_mode": pipe.get("processing_mode"),
        "generation": pipe.get("generation"),
        "updated_at": pipe.get("updated_at"),
        "reset_at": pipe.get("reset_at"),
    }


@tool("get_pipeline_metrics", "Return aggregate metrics for a pipeline.", _PipelineId)
async def get_pipeline_metrics(p: _PipelineId, **_ctx) -> Any:
    # No /metrics sub-route either — the parent doc embeds `stats`.
    pipe = await _get(f"/api/pipelines/{p.pipeline_id}")
    return {"id": pipe.get("id"), "name": pipe.get("name"), "stats": pipe.get("stats", {})}


@tool("list_models", "List registered embedding models.", _Empty)
async def list_models(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/models")


@tool("get_model", "Return a single model by id.", _ModelId)
async def get_model(p: _ModelId, **_ctx) -> Any:
    return await _get(f"/api/models/{p.model_id}")


@tool("list_jobs", "List recent ingestion jobs.", _Empty)
async def list_jobs(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/jobs")


@tool("get_job", "Return a single job by id.", _JobId)
async def get_job(p: _JobId, **_ctx) -> Any:
    return await _get(f"/api/jobs/{p.job_id}")


@tool("get_audit_log", "Recent audit-log entries (filterable by actor, method, path).", _AuditFilter)
async def get_audit_log(p: _AuditFilter, **_ctx) -> Any:
    params = {k: v for k, v in p.model_dump().items() if v is not None}
    return await _get("/api/audit-log", params=params)


@tool("get_capabilities", "Feature flags for this deployment.", _Empty)
async def get_capabilities(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/capabilities")


@tool("get_health_checks", "Run/return liveness checks for sources, destinations, pipelines, models.", _Empty)
async def get_health_checks(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/health/checks")


@tool("get_metrics_summary", "Aggregated in-memory metrics snapshot from the api telemetry store.", _Empty)
async def get_metrics_summary(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/metrics")


@tool("get_stats", "Overall control-plane stats (counts, totals).", _Empty)
async def get_stats(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/stats")


@tool("list_assistants", "List configured per-customer RAG assistants (read-only).", _Empty)
async def list_assistants(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/assistants")


@tool("get_settings", "Return the current control-plane settings document.", _Empty)
async def get_settings(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/settings")


@tool("list_transforms", "List configured transforms.", _Empty)
async def list_transforms(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/transforms")


@tool("list_docgrok_pipelines", "List pipelines registered with the DocGrok router.", _Empty)
async def list_docgrok_pipelines(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/docgrok/pipelines")


@tool("get_eventgrid_triggers", "Return registered Event Grid blob triggers.", _Empty)
async def get_eventgrid_triggers(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/eventgrid/triggers")


@tool("get_changefeed_leases", "Return Cosmos change-feed lease info.", _Empty)
async def get_changefeed_leases(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/metrics/changefeed")


@tool("list_deployments", "List active deployments.", _Empty)
async def list_deployments(_p: _Empty, **_ctx) -> Any:
    return await _get("/api/deployments")
