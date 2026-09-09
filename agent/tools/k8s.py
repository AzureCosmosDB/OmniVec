"""Kubernetes tools — namespace-scoped pod inspection."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import tool


NAMESPACE = os.environ.get("OMNIVEC_NAMESPACE", "omnivec")
DEPLOYMENTS = {
    "omnivec-api", "omnivec-controller", "omnivec-dotnet-worker",
    "omnivec-blob-ingestor", "omnivec-cosmos-changefeed",
    "omnivec-sharepoint-watcher", "omnivec-search",
    "docgrok", "docgrok-controller", "docgrok-pipeline-worker",
}


def check_namespace(namespace: str) -> str:
    if namespace != NAMESPACE:
        raise ValueError("agent operations are restricted to OMNIVEC_NAMESPACE")
    return namespace


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
                configured = os.getenv("KUBECONFIG", "").strip()
                if not configured:
                    raise RuntimeError("In-cluster configuration unavailable; explicit KUBECONFIG required") from None
                kconfig.load_kube_config(config_file=configured)
            self._core = kclient.CoreV1Api()
        return self._core

    async def list_pods(self, namespace: str) -> list[dict]:  # pragma: no cover
        api = self._api()
        result = await asyncio.to_thread(
            api.list_namespaced_pod, namespace=check_namespace(namespace),
            limit=100, _request_timeout=10,
        )
        if result.metadata and result.metadata._continue:
            raise RuntimeError("pod inspection limit exceeded; narrow the deployment")
        return [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "node": p.spec.node_name,
                "start_time": str(p.status.start_time) if p.status.start_time else None,
                "app": (p.metadata.labels or {}).get("app"),
                "ready": any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])),
                "restarts": sum(c.restart_count for c in (p.status.container_statuses or [])),
            }
            for p in result.items
        ]

    async def get_pod_status(self, namespace: str, name: str) -> dict:  # pragma: no cover
        api = self._api()
        p = await asyncio.to_thread(api.read_namespaced_pod_status, name=name, namespace=check_namespace(namespace), _request_timeout=10)
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
        return await asyncio.to_thread(
            api.read_namespaced_pod_log, name=name, namespace=check_namespace(namespace),
            tail_lines=tail_lines, limit_bytes=16000, _request_timeout=10,
        )

    async def get_pod_events(self, namespace: str, name: str) -> list[dict]:  # pragma: no cover
        api = self._api()
        events = await asyncio.to_thread(
            api.list_namespaced_event,
            namespace=check_namespace(namespace),
            field_selector=f"involvedObject.name={name}",
            limit=30, _request_timeout=10,
        )
        return [
            {"type": e.type, "reason": e.reason, "message": e.message, "ts": str(e.last_timestamp)}
            for e in events.items
        ]

    # --- Phase 2 mutating shims --------------------------------------------
    async def delete_pod(self, namespace: str, name: str) -> dict:  # pragma: no cover
        api = self._api()
        check_namespace(namespace)
        pod = await asyncio.to_thread(api.read_namespaced_pod, name=name, namespace=namespace, _request_timeout=10)
        if (pod.metadata.labels or {}).get("app") not in DEPLOYMENTS:
            raise ValueError("pod is not an allowlisted OmniVec workload")
        if not any(o.controller and o.kind in ("ReplicaSet", "StatefulSet") for o in (pod.metadata.owner_references or [])):
            raise ValueError("refusing to delete a pod without a recreating controller")
        await asyncio.to_thread(api.delete_namespaced_pod, name=name, namespace=namespace, _request_timeout=10)
        return {"deleted": True, "namespace": namespace, "pod": name}

    async def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> dict:  # pragma: no cover
        check_namespace(namespace)
        if deployment not in DEPLOYMENTS:
            raise ValueError("deployment is not in the agent allowlist")
        self._api()
        from kubernetes import client as kclient
        apps = kclient.AppsV1Api()
        body = {"spec": {"replicas": int(replicas)}}
        await asyncio.to_thread(apps.patch_namespaced_deployment_scale, name=deployment, namespace=namespace, body=body, _request_timeout=10)
        return {"scaled": True, "namespace": namespace, "deployment": deployment, "replicas": replicas}

    async def list_deployments(self, namespace: str) -> list[dict]:  # pragma: no cover
        self._api()
        from kubernetes import client
        result = await asyncio.to_thread(
            client.AppsV1Api().list_namespaced_deployment,
            namespace=check_namespace(namespace), limit=100, _request_timeout=10,
        )
        if result.metadata and result.metadata._continue:
            raise RuntimeError("deployment inspection limit exceeded")
        rows = []
        for d in result.items:
            if d.metadata.name not in DEPLOYMENTS:
                continue
            # Never return arbitrary environment variables or secret references.
            flags = {
                e.name: e.value for c in d.spec.template.spec.containers for e in (c.env or [])
                if e.name in ("ChangeFeed__BlobEventConsumerEnabled", "ChangeFeed__BlobEventQueueName")
            }
            rows.append({
                "name": d.metadata.name, "desired": d.spec.replicas,
                "ready": d.status.ready_replicas or 0,
                "available": d.status.available_replicas or 0,
                "generation": d.metadata.generation,
                "observed_generation": d.status.observed_generation,
                "flags": flags,
            })
        return rows


_KUBE: _KubeClient = _KubeClient()


class _NS(BaseModel):
    namespace: Optional[str] = Field(default=None, description="Override OMNIVEC_NAMESPACE.")

    def ns(self) -> str:
        return check_namespace((self.namespace or NAMESPACE).strip())


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
