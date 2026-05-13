"""Kubernetes tools — namespace-scoped pod inspection."""
from __future__ import annotations

import os
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import tool


NAMESPACE = os.environ.get("OMNIVEC_NAMESPACE", "omnivec")


class _KubeClient:
    """Thin facade over kubernetes.client.CoreV1Api — lazy SDK import."""

    def __init__(self):
        self._core = None

    def _api(self):  # pragma: no cover - real-env only
        if self._core is None:
            from kubernetes import client as kclient, config as kconfig
            try:
                kconfig.load_incluster_config()
            except Exception:
                kconfig.load_kube_config()
            self._core = kclient.CoreV1Api()
        return self._core

    async def list_pods(self, namespace: str) -> list[dict]:  # pragma: no cover
        api = self._api()
        result = api.list_namespaced_pod(namespace=namespace)
        return [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "node": p.spec.node_name,
                "start_time": str(p.status.start_time) if p.status.start_time else None,
            }
            for p in result.items
        ]

    async def get_pod_status(self, namespace: str, name: str) -> dict:  # pragma: no cover
        api = self._api()
        p = api.read_namespaced_pod_status(name=name, namespace=namespace)
        return {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason}
                for c in (p.status.conditions or [])
            ],
            "container_statuses": [
                {"name": cs.name, "ready": cs.ready, "restart_count": cs.restart_count}
                for cs in (p.status.container_statuses or [])
            ],
        }

    async def get_pod_logs(self, namespace: str, name: str, tail_lines: int) -> str:  # pragma: no cover
        api = self._api()
        return api.read_namespaced_pod_log(name=name, namespace=namespace, tail_lines=tail_lines)

    async def get_pod_events(self, namespace: str, name: str) -> list[dict]:  # pragma: no cover
        api = self._api()
        events = api.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name}",
        )
        return [
            {"type": e.type, "reason": e.reason, "message": e.message, "ts": str(e.last_timestamp)}
            for e in events.items
        ]


_KUBE: _KubeClient = _KubeClient()


class _NS(BaseModel):
    namespace: Optional[str] = Field(default=None, description="Override OMNIVEC_NAMESPACE.")

    def ns(self) -> str:
        return (self.namespace or NAMESPACE).strip()


class _PodRef(_NS):
    pod_name: str = Field(..., min_length=1)


class _PodLogs(_PodRef):
    tail_lines: int = Field(default=200, ge=1, le=5000)


@tool("list_pods", "List pods in the OmniVec namespace.", _NS)
async def list_pods(p: _NS, **_ctx) -> Any:
    return {"pods": await _KUBE.list_pods(p.ns())}


@tool("get_pod_status", "Detailed status (phase, conditions, container restart counts) for a pod.", _PodRef)
async def get_pod_status(p: _PodRef, **_ctx) -> Any:
    return await _KUBE.get_pod_status(p.ns(), p.pod_name)


@tool("get_pod_logs", "Tail the logs of a pod (max 5000 lines).", _PodLogs)
async def get_pod_logs(p: _PodLogs, **_ctx) -> Any:
    return {"logs": await _KUBE.get_pod_logs(p.ns(), p.pod_name, p.tail_lines)}


@tool("get_pod_events", "Recent Kubernetes events for a pod (CrashLoopBackOff, OOM, etc.).", _PodRef)
async def get_pod_events(p: _PodRef, **_ctx) -> Any:
    return {"events": await _KUBE.get_pod_events(p.ns(), p.pod_name)}
