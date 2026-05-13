"""Mutating tools — admin-only, gated by the approval flow.

Every tool here is registered with ``readonly=False`` so ``agent_loop`` will
NOT execute it automatically; instead it parks the call as a pending
approval, emits an ``approval_required`` event, and waits for a human Approve
/ Deny via ``POST /v1/chat/approve``. See ``agent/approvals.py``.

Three categories:

* **k8s**: restart a pod, scale a deployment.
* **omnivec api**: pause / resume / reset pipelines, retry / cancel jobs.
* **service-bus**: replay or purge DLQ messages.

The shims for k8s scale / pod-delete and service-bus DLQ replay live in
``agent/tools/k8s.py`` and ``agent/tools/servicebus.py`` (alongside their
read-only counterparts) so a single facade is overridden in tests.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from . import tool
from .k8s import _KUBE, NAMESPACE
from .servicebus import _SB, _fqns


OMNIVEC_API_URL = os.environ.get("OMNIVEC_API_URL", "http://omnivec-api")


# ---------------------------------------------------------------------------
# Shared HTTP client + helper for control-plane POSTs.
# ---------------------------------------------------------------------------
_HTTP: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _HTTP


async def _post(path: str, json_body: dict | None = None) -> Any:
    url = OMNIVEC_API_URL.rstrip("/") + path
    headers = {"Host": "omnivec-api"}
    r = await _client().post(url, json=(json_body or {}), headers=headers)
    r.raise_for_status()
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"text": r.text}


# ---------------------------------------------------------------------------
# K8s mutations.
# ---------------------------------------------------------------------------
class _PodTarget(BaseModel):
    namespace: Optional[str] = Field(default=None, description="Override OMNIVEC_NAMESPACE.")
    pod_name: str = Field(..., min_length=1)

    def ns(self) -> str:
        return (self.namespace or NAMESPACE).strip()


class _ScaleTarget(BaseModel):
    namespace: Optional[str] = None
    deployment: str = Field(..., min_length=1)
    replicas: int = Field(..., ge=0, le=20, description="Target replica count (0-20).")

    def ns(self) -> str:
        return (self.namespace or NAMESPACE).strip()


@tool(
    "restart_pod",
    "Delete a pod so its owning controller (Deployment/StatefulSet) recreates it. "
    "Use to recover from a wedged process. Low risk — equivalent to a graceful restart.",
    _PodTarget, role="admin", readonly=False,
)
async def restart_pod(p: _PodTarget, **_ctx) -> Any:
    return await _KUBE.delete_pod(p.ns(), p.pod_name)


@tool(
    "scale_deployment",
    "Set the replica count for a deployment. Scaling to 0 effectively pauses the workload.",
    _ScaleTarget, role="admin", readonly=False,
)
async def scale_deployment(p: _ScaleTarget, **_ctx) -> Any:
    return await _KUBE.scale_deployment(p.ns(), p.deployment, p.replicas)


# ---------------------------------------------------------------------------
# Pipeline / job control via omnivec api.
# ---------------------------------------------------------------------------
class _PipelineId(BaseModel):
    pipeline_id: str = Field(..., min_length=1)


class _JobId(BaseModel):
    job_id: str = Field(..., min_length=1)


@tool(
    "pause_pipeline",
    "Pause a pipeline: new triggers stop firing, in-flight messages complete.",
    _PipelineId, role="admin", readonly=False,
)
async def pause_pipeline(p: _PipelineId, **_ctx) -> Any:
    return await _post(f"/api/pipelines/{p.pipeline_id}/pause")


@tool(
    "resume_pipeline",
    "Resume a previously-paused pipeline.",
    _PipelineId, role="admin", readonly=False,
)
async def resume_pipeline(p: _PipelineId, **_ctx) -> Any:
    return await _post(f"/api/pipelines/{p.pipeline_id}/resume")


@tool(
    "reset_pipeline_offsets",
    "DESTRUCTIVE: reset change-feed offsets / leases for a pipeline. "
    "Causes the pipeline to re-process from the beginning. Use with extreme care.",
    _PipelineId, role="admin", readonly=False,
)
async def reset_pipeline_offsets(p: _PipelineId, **_ctx) -> Any:
    return await _post(f"/api/pipelines/{p.pipeline_id}/reset")


@tool(
    "retry_job",
    "Re-queue a failed ingestion job for another attempt.",
    _JobId, role="admin", readonly=False,
)
async def retry_job(p: _JobId, **_ctx) -> Any:
    return await _post(f"/api/jobs/{p.job_id}/retry")


@tool(
    "cancel_job",
    "Cancel an in-flight or queued job. The job will not be retried.",
    _JobId, role="admin", readonly=False,
)
async def cancel_job(p: _JobId, **_ctx) -> Any:
    return await _post(f"/api/jobs/{p.job_id}/cancel")


# ---------------------------------------------------------------------------
# Service-bus DLQ mutations.
# ---------------------------------------------------------------------------
class _DlqRetry(BaseModel):
    queue: str = Field(..., min_length=1)
    max_messages: int = Field(default=10, ge=1, le=500, description="Cap on messages to replay.")
    namespace_fqns: Optional[str] = None


class _DlqPurge(BaseModel):
    queue: str = Field(..., min_length=1)
    confirm: bool = Field(..., description="Must be true; an extra safety latch on top of the human approval.")
    namespace_fqns: Optional[str] = None


@tool(
    "retry_dlq_messages",
    "Replay up to N messages from the dead-letter sub-queue back to the active queue.",
    _DlqRetry, role="admin", readonly=False,
)
async def retry_dlq_messages(p: _DlqRetry, **_ctx) -> Any:
    fqns = _fqns(p.namespace_fqns)
    return await _SB.retry_dlq(fqns, p.queue, p.max_messages)


@tool(
    "purge_dlq",
    "DESTRUCTIVE: discard all messages in the dead-letter sub-queue. "
    "Set confirm=true. Use only after triaging the failures.",
    _DlqPurge, role="admin", readonly=False,
)
async def purge_dlq(p: _DlqPurge, **_ctx) -> Any:
    if not p.confirm:
        return {"error": "purge_dlq requires confirm=true"}
    fqns = _fqns(p.namespace_fqns)
    return await _SB.purge_dlq(fqns, p.queue)


# ---------------------------------------------------------------------------
# Danger-level lookup used by agent_loop when emitting approval_required.
# Tools NOT listed here default to "medium".
# ---------------------------------------------------------------------------
DANGER_LEVELS: dict[str, str] = {
    "restart_pod": "low",
    "scale_deployment": "medium",
    "pause_pipeline": "low",
    "resume_pipeline": "low",
    "retry_job": "low",
    "cancel_job": "medium",
    "reset_pipeline_offsets": "high",
    "retry_dlq_messages": "medium",
    "purge_dlq": "high",
}


def danger_level(tool_name: str) -> str:
    return DANGER_LEVELS.get(tool_name, "medium")
