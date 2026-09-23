"""Bounded, read-only operational evidence. Configuration is not processing health."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import re

from pydantic import BaseModel, Field

from . import tool
from . import k8s, omnivec_api, servicebus


MAX_PIPELINES = 6
MAX_SOURCES = 8
MAX_AGE_SECONDS = 600
SAMPLE_SECONDS = 5
SNAPSHOT_TIMEOUT = 35

RUNBOOKS = {
    "workers_stopped": "Approve scaling only the named stopped deployment to one replica; respect intentional maintenance/HPA. Preserve jobs and verify destination progress.",
    "workload_unready": "Inspect the named pod's readiness, events and log signals. After fixing the cause, approve one controller-owned pod restart, then verify progress; do not restart repeatedly.",
    "missing_model": "Restore the referenced embedding model/route via the model configuration workflow. Preserve model IDs, dimensions and pipeline bindings; do not substitute a chat model or reset the pipeline.",
    "dimension_mismatch": "Correct the incompatible model/vector-index configuration through the owner workflow. Preserve existing model IDs and data; do not silently substitute a model or recreate the destination.",
    "dependency_failed": "Have the dependency owner correct the reported source/destination/model fault, then wait for a fresh health check and verify real writes. Credentials and infrastructure permissions are not automatically repairable.",
    "source_permissions": "Ask the source owner to restore the existing identity's required access. Never request or display credentials, bypass permissions, or reset checkpoints.",
    "dlq": "Inspect poison-message error metadata with the operator, fix its cause, then approve a bounded replay with duplicate handling. Never purge to make health green; the current agent replay adapter fails closed until configured.",
    "blob_consumer_disabled": "Enable the existing BlobEventConsumer through the deployment's Helm configuration and validate Event Grid delivery, queue consumption and destination writes. Do not recreate subscriptions or discard events.",
    "bad_chunk_config": "Correct chunk size/overlap/unit in the existing pipeline edit workflow. Preserve source/destination/model IDs and checkpoints; verify newly processed chunks, not just saved configuration.",
    "no_progress": "Compare job backlog, source/embedded counts and poller/worker evidence over a suitable source-specific window. A short quiet window is not proof of a dead poller. Fix the observed dependency first; allow one targeted restart only with evidence, otherwise escalate.",
    "paused": "If not intentional maintenance, approve resume_pipeline for this pipeline only. Verify downstream writes; active status alone is not success.",
    "agent_identity_federation": "The identity owner must configure federation for the dedicated agent service account using the approved AKS issuer and api://AzureADTokenExchange audience. Do not reuse a more privileged service account or bypass authentication; pipeline health remains unknown until required observations are available.",
    "agent_observation_permissions": "The operator must check the agent's existing service authentication, namespaced read RBAC or scoped Azure read permissions for the named observation. An agent observation failure is not proof that ingestion is broken. Do not grant broad roles, switch identities or restart workers to hide it.",
    "source_inventory_unavailable": "The control plane intentionally does not enumerate Blob containers, and its source count does not cover multiple sources. Use an authorized synthetic document and verify new destination writes with unchanged bindings. Do not restart workers just because an inventory counter is absent; processing proof does not establish complete source coverage.",
    "telemetry_unavailable": "Inspect the named missing counters and control-plane/dependency health, then collect a fresh observation. Do not replace missing counters with zero or infer a stopped worker from missing telemetry.",
}

_SIGNALS = {
    "source_permissions": r"\b(Forbidden|Unauthorized|AuthorizationPermissionMismatch|AccessDenied|403|401)\b",
    "missing_model": r"(model.{0,40}not found|unknown model|no route|pipeline.{0,40}not found)",
    "bad_chunk_config": r"(chunk.{0,40}(invalid|overlap|size)|overlap.{0,30}chunk)",
    "workload_unready": r"(OOMKilled|CrashLoopBackOff|ImagePullBackOff|connection refused|timed out)",
}


class PipelineRef(BaseModel):
    pipeline_id: str = Field(..., max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RecoveryRef(BaseModel):
    pipeline_id: str | None = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Empty(BaseModel):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh(ts: str | None) -> bool:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        return 0 <= age <= MAX_AGE_SECONDS
    except (TypeError, ValueError, AttributeError):
        return False


async def _observe(call) -> dict:
    try:
        value = await asyncio.wait_for(call, timeout=12)
        if isinstance(value, dict) and (value.get("error") or value.get("stub") or value.get("success") is False):
            return {"ok": False, "reason": "adapter reported failure"}
        return {"ok": True, "value": value}
    except Exception as exc:
        # Exception text can contain signed URLs, tokens or connection strings.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if not isinstance(status, int) or isinstance(status, bool):
            status = None
        error = {"ok": False, "reason": type(exc).__name__, "http_status": status}
        if "AADSTS700213" in str(exc):
            error["error_code"] = "AADSTS700213"
        return error


def _rows(value, key: str) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return _rows(value.get(key, []), key)
    return []


def _finding(code: str, evidence: str, *, confidence: str = "high", severity: str = "blocked", action: dict | None = None) -> dict:
    return {
        "code": code, "severity": severity, "likely_cause": code.replace("_", " "),
        "evidence": evidence, "confidence": confidence,
        "next_action": RUNBOOKS.get(code, RUNBOOKS["dependency_failed"]),
        "approved_tool_candidate": action,
    }


def _number(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v if v >= 0 else None


def _observation_finding(observation: dict, label: str) -> dict | None:
    if observation.get("ok"):
        return None
    if observation.get("error_code") == "AADSTS700213":
        return _finding("agent_identity_federation", f"Agent cannot observe {label}: AADSTS700213; pipeline health is not established.", severity="unknown")
    if observation.get("http_status") in (401, 403):
        return _finding("agent_observation_permissions", f"Agent cannot observe {label}: HTTP {observation['http_status']}; this is an observation permission failure.", severity="unknown")
    return None


def _repair_plan(findings: list[dict], unknown: list[str], scope: str | None) -> list[dict]:
    priorities = {"blocked": 0, "unhealthy": 1, "unknown": 2}
    plan, seen = [], set()
    for finding in sorted(findings, key=lambda f: priorities.get(f["severity"], 3)):
        key = (finding["code"], finding["evidence"])
        if key in seen:
            continue
        seen.add(key)
        action = finding.get("approved_tool_candidate")
        plan.append({
            "priority": len(plan) + 1, "scope": scope, "code": finding["code"],
            "execution": "approval_required" if action else "operator_investigation",
            "action": action, "instructions": finding["next_action"],
            "precondition": "Confirm the evidence and intended operating state; resolve unknown dependencies before approving a mutation.",
            "verification": "Fresh healthy dependencies and increased destination progress with unchanged source/model/destination/checkpoint bindings; idle alone is not repair.",
        })
    if not plan and unknown:
        plan.append({
            "priority": 1, "scope": scope, "code": "telemetry_unavailable",
            "execution": "observe_only", "action": None,
            "instructions": RUNBOOKS["telemetry_unavailable"], "missing_evidence": unknown,
        })
    return plan


def _model_refs(value, depth=0) -> set[str]:
    if depth > 8:
        return set()
    if isinstance(value, str):
        return {value} if re.fullmatch(r"mdl-[A-Za-z0-9_.-]+", value) else set()
    if isinstance(value, dict):
        return set().union(*(_model_refs(v, depth + 1) for v in list(value.values())[:80])) if value else set()
    if isinstance(value, list):
        return set().union(*(_model_refs(v, depth + 1) for v in value[:30])) if value else set()
    return set()


async def _cluster_snapshot() -> dict:
    deployments, pods = await asyncio.gather(
        _observe(k8s._KUBE.list_deployments(k8s.NAMESPACE)),
        _observe(k8s._KUBE.list_pods(k8s.NAMESPACE)),
    )
    result = {"deployments": deployments, "pods": pods, "signals": [], "unknown": []}
    if not deployments["ok"] or not pods["ok"]:
        result["unknown"].append("Kubernetes readiness/deployment evidence unavailable")
    rows = pods.get("value", []) if pods["ok"] else []
    candidates = [p for p in rows if p.get("app") in k8s.DEPLOYMENTS]
    candidates.sort(key=lambda p: (p.get("ready", False), -p.get("restarts", 0)))
    for p in candidates[:3]:
        events, logs = await asyncio.gather(
            _observe(k8s._KUBE.get_pod_events(k8s.NAMESPACE, p["name"])),
            _observe(k8s._KUBE.get_pod_logs(k8s.NAMESPACE, p["name"], 80)),
        )
        text = str(logs.get("value", "")) + str(events.get("value", ""))
        codes = [code for code, pattern in _SIGNALS.items() if re.search(pattern, text, re.I)]
        result["signals"].append({
            "pod": p["name"], "app": p.get("app"), "codes": codes,
            "logs_observed": logs["ok"], "events_observed": events["ok"],
            "uncertainty": "Log/event matches may be historical or concern another pipeline; not a proven root cause.",
        })
    return result


async def _queues_snapshot() -> dict:
    names = servicebus.configured_queues()
    subscriptions = servicebus.configured_subscriptions()
    if not names and not subscriptions:
        return {"ok": False, "reason": "AGENT_QUEUE_NAMES/AGENT_SB_SUBSCRIPTIONS not configured; backlog is UNKNOWN"}
    calls = [_observe(servicebus._SB.queue_depth(servicebus._fqns(None), name)) for name in names]
    for topic, sub in subscriptions:
        names.append(f"{topic}/Subscriptions/{sub}")
        calls.append(_observe(servicebus._SB.subscription_depth(servicebus._fqns(None), topic, sub)))
    values = await asyncio.gather(*calls)
    return {"ok": all(v["ok"] for v in values), "queues": dict(zip(names, values)),
            "scope": "shared namespace queues; not attributable to a single pipeline"}


def _dependency_health(health: dict, section: str, identifier: str) -> dict:
    row = next((v for v in _rows(health, section) if v.get("id") == identifier), {})
    checks = [{k: c.get(k) for k in ("check", "status", "dimensions", "vector_field")}
              for c in row.get("checks", []) if isinstance(c, dict)]
    return {
        "id": identifier, "kind": section, "status": row.get("status", "unknown"),
        "fresh": _fresh(row.get("checked_at") or health.get("checked_at")),
        "checked_at": row.get("checked_at") or health.get("checked_at"), "checks": checks,
        # Classify error text without exposing source credentials or raw log contents.
        "signals": [code for code, pattern in _SIGNALS.items()
                    if re.search(pattern, str(row.get("checks", [])), re.I)],
    }


async def _pipeline_snapshot(pid: str, health: dict, models: dict) -> dict:
    obs = await _observe(omnivec_api._get_resource("pipelines", pid))
    if not obs["ok"]:
        return {"id": pid, "unknown": ["Pipeline could not be read"], "observation": obs}
    p = obs["value"]
    if not isinstance(p, dict) or p.get("id") != pid:
        return {"id": pid, "unknown": ["Invalid pipeline response"]}
    out = {k: p.get(k) for k in (
        "id", "status", "processing_mode", "generation", "reset_at", "destination_id",
        "docgrok_pipeline", "content_strategy", "chunk_config",
        "vector_index_path",
    )}
    # Chunk text-field/id templates and arbitrary config are not necessary evidence.
    out["chunk_config"] = {k: (p.get("chunk_config") or {}).get(k) for k in ("chunk_size", "chunk_overlap", "chunk_unit")}
    stats = p.get("stats") or {}
    out["stats"] = {k: stats.get(k) for k in (
        "documents_processed", "embedded_count", "source_doc_count", "last_run",
        "recent_throughput_docs_per_sec",
    )}
    out["stats"]["jobs"] = {k: (stats.get("jobs") or {}).get(k) for k in ("pending", "processing", "completed", "failed")}
    out.update(sources=[], dependencies=[], unknown=[], findings=[])
    sources = p.get("sources") or []
    out["source_ids"] = sorted(ref.get("source_id", "") for ref in sources)
    if not sources or len(sources) > MAX_SOURCES:
        out["unknown"].append("Missing sources or source inspection limit exceeded")
    for ref in sources[:MAX_SOURCES]:
        sid = ref.get("source_id", "")
        src = await _observe(omnivec_api._get_resource("sources", sid))
        dep = _dependency_health(health, "sources", sid)
        if src["ok"] and isinstance(src["value"], dict) and src["value"].get("id") == sid:
            s = src["value"]
            out["sources"].append({k: s.get(k) for k in ("id", "type", "enabled", "triggers")})
            dep["exists"] = True
            dep["enabled"] = s.get("enabled", True)
        else:
            dep["exists"] = False if src.get("http_status") == 404 else None
        out["dependencies"].append(dep)
    did = p.get("destination_id") or ""
    dest = await _observe(omnivec_api._get_resource("destinations", did))
    dep = _dependency_health(health, "destinations", did)
    valid_dest = dest["ok"] and isinstance(dest["value"], dict) and dest["value"].get("id") == did
    dep["exists"] = True if valid_dest else (False if dest.get("http_status") == 404 else None)
    if valid_dest:
        dep["enabled"] = dest["value"].get("enabled", True)
        dep["configured_dimensions"] = _number((dest["value"].get("config") or {}).get("vector_dimensions"))
    out["dependencies"].append(dep)
    route = p.get("docgrok_pipeline") or ""
    mids = {route} if route.startswith("mdl-") else set()
    if route and not mids:
        routed = await _observe(omnivec_api._get_resource("docgrok_pipelines", route))
        if routed["ok"]:
            mids = _model_refs(routed["value"])
            if not mids:
                out["unknown"].append("Route exists but its embedding model binding could not be resolved")
        elif routed.get("http_status") == 404:
            out["findings"].append(_finding("missing_model", f"Route {route} returned 404"))
        else:
            out["unknown"].append("Model route lookup unavailable")
    if not route:
        out["findings"].append(_finding("missing_model", "Pipeline has no embedding model/route"))
    if len(mids) > 8:
        out["unknown"].append("Model inspection limit exceeded")
    out["model_ids"] = sorted(mids)[:8]
    for mid in out["model_ids"]:
        model = next((m for m in _rows(models.get("value"), "models") if m.get("id") == mid), None)
        dep = _dependency_health(health, "models", mid)
        dep["exists"] = bool(model) if models.get("ok") else None
        if model:
            dep["category"] = model.get("model_category")
            dep["enabled"] = model.get("enabled", True)
            dep["embedding_dim"] = _number(model.get("embedding_dim"))
        out["dependencies"].append(dep)
    return out


async def _collect(pipeline_id: str | None) -> dict:
    cluster, queues, health_obs, models = await asyncio.gather(
        _cluster_snapshot(), _queues_snapshot(), _observe(omnivec_api._get("/api/health/checks")),
        _observe(omnivec_api._get("/api/models")),
    )
    health = health_obs.get("value", {}) if health_obs["ok"] else {}
    registry = models.get("value")
    if models.get("ok") and not isinstance(registry, list) and not (isinstance(registry, dict) and isinstance(registry.get("models"), list)):
        models = {"ok": False, "reason": "Model registry shape invalid"}
    unknown = list(cluster["unknown"])
    findings = []
    for observation, label in (
        (cluster["deployments"], "Kubernetes deployments"),
        (cluster["pods"], "Kubernetes pods"),
        (health_obs, "control-plane dependency health"),
        (models, "model registry"),
    ):
        finding = _observation_finding(observation, label)
        if finding:
            findings.append(finding)
    if not health_obs["ok"]:
        unknown.append("Dependency health observations unavailable")
    if pipeline_id:
        ids = [pipeline_id]
    else:
        pipes = await _observe(omnivec_api._get("/api/pipelines", {"include_stats": "false"}))
        ids = [p["id"] for p in _rows(pipes.get("value"), "pipelines") if p.get("id")]
        if not pipes["ok"] or len(ids) > MAX_PIPELINES:
            unknown.append("System pipeline coverage incomplete (maximum six pipelines); diagnose named pipelines separately")
        ids = ids[:MAX_PIPELINES]
    pipelines = await asyncio.gather(*(_pipeline_snapshot(pid, health, models) for pid in ids))
    return {"scope": pipeline_id or "system", "observed_at": _now(), "cluster": cluster,
            "queues": queues, "pipelines": pipelines, "unknown": unknown, "findings": findings}


async def collect_snapshot(pipeline_id: str | None = None) -> dict:
    try:
        return await asyncio.wait_for(_collect(pipeline_id), timeout=SNAPSHOT_TIMEOUT)
    except Exception as exc:
        return {"scope": pipeline_id or "system", "observed_at": _now(), "pipelines": [],
                "unknown": [f"Snapshot unavailable ({type(exc).__name__}); no health conclusion possible"]}


def evaluate(snapshot: dict, baseline: dict | None = None) -> dict:
    """Conservative state machine; only observed destination progress is processing proof."""
    findings, unknown = list(snapshot.get("findings", [])), list(snapshot.get("unknown", []))
    limitations = []
    cluster = snapshot.get("cluster", {})
    deployments = {d["name"]: d for d in cluster.get("deployments", {}).get("value", [])}
    queues = snapshot.get("queues", {})
    queue_depths = []
    bus_required = snapshot.get("scope") == "system" or any(
        p.get("processing_mode") != "inline" for p in snapshot.get("pipelines", [])
    )
    for name, observation in (queues.get("queues", {}) if bus_required else {}).items():
        value = observation.get("value", {})
        if not observation.get("ok") or any(_number(value.get(k)) is None for k in ("active_message_count", "dead_letter_message_count")):
            unknown.append(f"Queue {name} counters unavailable")
            finding = _observation_finding(observation, f"Service Bus {name}")
            if finding:
                findings.append(finding)
            continue
        queue_depths.append(value["active_message_count"])
        if value["dead_letter_message_count"] or value.get("transfer_dead_letter_message_count", 0):
            findings.append(_finding("dlq", f"Shared queue {name} has dead letters; pipeline attribution unknown", severity="unhealthy"))
    results = []
    before = {p["id"]: p for p in (baseline or {}).get("pipelines", [])}
    for p in snapshot.get("pipelines", []):
        pid = p["id"]
        pf, pu = list(p.get("findings", [])), list(p.get("unknown", []))
        if p.get("status") in ("paused", "disabled"):
            pf.append(_finding("paused", f"Pipeline {pid} is {p['status']}", action={"tool": "resume_pipeline", "args": {"pipeline_id": pid}}))
        elif p.get("status") != "active":
            pu.append("Pipeline active state not established")
        if p.get("content_strategy") == "chunk":
            c = p.get("chunk_config") or {}
            size, overlap = c.get("chunk_size"), c.get("chunk_overlap")
            if (not isinstance(size, int) or not isinstance(overlap, int) or size <= 0
                    or overlap < 0 or overlap >= size or c.get("chunk_unit") not in ("chars", "tokens")):
                pf.append(_finding("bad_chunk_config", "Chunk size, overlap or unit is invalid"))
        for dep in p.get("dependencies", []):
            label = f"{dep['kind']}:{dep['id']}"
            failed = dep.get("exists") is False or dep.get("enabled") is False or dep.get("category") == "chat"
            fresh_failure = dep.get("fresh") and (dep.get("status") in ("unhealthy", "error") or any(c.get("status") == "fail" for c in dep.get("checks", [])))
            if failed or fresh_failure:
                code = "missing_model" if dep["kind"] == "models" and failed else "dependency_failed"
                if "source_permissions" in dep.get("signals", []):
                    code = "source_permissions"
                pf.append(_finding(code, f"{label}: exists={dep.get('exists')}, enabled={dep.get('enabled')}, health={dep.get('status')}; checks={dep.get('checks')}"))
            elif dep.get("exists") is not True or not dep.get("fresh") or dep.get("status") != "healthy":
                pu.append(f"{label}: health unknown, warning, stale or configuration unreadable")
        destination = next((d for d in p.get("dependencies", []) if d["kind"] == "destinations"), {})
        dimensions = destination.get("configured_dimensions")
        for check in destination.get("checks", []):
            if check.get("vector_field") == str(p.get("vector_index_path") or "").lstrip("/") and destination.get("fresh"):
                dimensions = _number(check.get("dimensions")) or dimensions
        for model in (d for d in p.get("dependencies", []) if d["kind"] == "models"):
            if dimensions and model.get("embedding_dim") and dimensions != model["embedding_dim"]:
                pf.append(_finding("dimension_mismatch", f"Model {model['id']} emits {model['embedding_dim']} dimensions; selected destination expects {dimensions}"))
        required = {"omnivec-api", "omnivec-controller", "docgrok"}
        inline = p.get("processing_mode") == "inline"
        required.add("omnivec-cosmos-changefeed" if inline else "omnivec-dotnet-worker")
        if not inline and not queues.get("ok"):
            pu.append("Service Bus observations unavailable")
        for source in p.get("sources", []):
            st = source.get("type", "")
            if st in ("azure_blob", "azure-blob", "blob"):
                required.add("omnivec-blob-ingestor")
                flags = deployments.get("omnivec-blob-ingestor", {}).get("flags", {})
                if any(t in ("event-grid", "eventgrid") for t in (source.get("triggers") or [])):
                    enabled = str(flags.get("ChangeFeed__BlobEventConsumerEnabled", "")).lower()
                    if enabled == "false":
                        pf.append(_finding("blob_consumer_disabled", "Blob event trigger configured but BlobEventConsumerEnabled=false"))
                    elif enabled != "true":
                        pu.append("Blob event consumer enablement unknown")
            if st in ("cosmosdb", "cosmos"):
                required.add("omnivec-cosmos-changefeed")
            if st == "sharepoint":
                required.add("omnivec-sharepoint-watcher")
            if st in ("onelake-iceberg", "onelake_iceberg"):
                required.add("omnivec-onelake-iceberg-watcher")
        for name in sorted(required):
            d = deployments.get(name)
            if not d:
                pu.append(f"Required workload {name} not observed")
            elif d.get("desired") == 0:
                pf.append(_finding("workers_stopped", f"{name}: desired=0, ready={d.get('ready')}",
                                   action={"tool": "scale_deployment", "args": {"deployment": name, "replicas": 1, "pipeline_id": pid}}))
            elif (d.get("ready", 0) < d.get("desired", 1)
                  or d.get("observed_generation") != d.get("generation")):
                pf.append(_finding("workload_unready", f"{name}: desired={d.get('desired')}, ready={d.get('ready')}, rollout observed={d.get('observed_generation')}"))
        pods = cluster.get("pods", {})
        if not pods.get("ok"):
            pu.append("Pod readiness/restart observations unavailable")
        for pod in pods.get("value", []):
            if pod.get("app") in required and pod.get("phase") not in ("Succeeded",):
                if pod.get("ready") is not True:
                    pf.append(_finding("workload_unready", f"Pod {pod['name']}: phase={pod.get('phase')}, ready={pod.get('ready')}, restarts={pod.get('restarts')}"))
        stats = p.get("stats", {})
        jobs = stats.get("jobs", {})
        pending, processing, failed = (_number(jobs.get(k)) for k in ("pending", "processing", "failed"))
        embedded, source_count = _number(stats.get("embedded_count")), _number(stats.get("source_doc_count"))
        source_types = [s.get("type") for s in p.get("sources", [])]
        source_ids = p.get("source_ids") or [s.get("id") for s in p.get("sources", [])]
        non_enumerable_sources = {
            "azure_blob", "azure-blob", "blob", "sharepoint",
            "onelake-iceberg", "onelake_iceberg",
        }
        inventory_not_collected = (
            len(source_ids) > 1 or
            bool(source_types) and all(t in non_enumerable_sources for t in source_types)
            and source_count is None
        )
        pl = []
        if len(source_ids) > 1:
            source_count = None
        if inventory_not_collected:
            pl.append("Complete source inventory is not collected for connector-driven or multi-source pipelines; source coverage and catch-up are not verified.")
        backlog = (pending or 0) + (processing or 0)
        if source_count is not None and embedded is not None:
            backlog = max(backlog, source_count - embedded)
        if failed:
            pf.append(_finding("dependency_failed", f"{pid}: failed jobs={failed}; inspect failures before replay", severity="unhealthy"))
        progress = False
        old = before.get(pid)
        if old:
            preserved = all(old.get(k) == p.get(k) for k in (
                "generation", "reset_at", "destination_id", "docgrok_pipeline", "model_ids",
                "source_ids", "processing_mode", "content_strategy", "chunk_config", "vector_index_path",
            ))
            previous = _number(old.get("stats", {}).get("embedded_count"))
            if not preserved:
                pu.append("Generation/checkpoint/destination/model binding changed; progress comparison invalid")
            elif previous is not None and embedded is not None:
                progress = embedded > previous
                if embedded < previous:
                    pu.append("Destination count regressed; cannot verify recovery")
            if backlog and not progress:
                pf.append(_finding("no_progress", f"{pid}: outstanding work={backlog}, no destination-count increase across observations",
                                   confidence="low", severity="unknown"))
                pu.append("No progress in bounded observation window; poller fault not proven")
        missing = [name for name, value in (
            ("jobs.pending", pending), ("jobs.processing", processing),
            ("jobs.failed", failed), ("embedded_count", embedded),
        ) if value is None]
        if source_count is None and not inventory_not_collected:
            missing.append("source_doc_count")
        if missing:
            pu.append("Required counters unavailable: " + ", ".join(missing))
            pf.append(_finding("telemetry_unavailable", f"{pid}: missing or invalid counters: {', '.join(missing)}", severity="unknown"))
        if inventory_not_collected and not progress:
            pu.append("Source inventory is not collected; no destination progress observed to verify processing.")
            pf.append(_finding("source_inventory_unavailable", f"{pid}: source inventory not collected; known job backlog={backlog}, embedded_count={embedded}", severity="unknown"))
        idle = backlog == 0 and all(v is not None for v in (pending, processing, failed, embedded, source_count))
        if not inline and (not queue_depths or any(queue_depths)):
            idle = False
        state = "BLOCKED" if any(f["severity"] == "blocked" for f in pf) else (
            "UNHEALTHY" if any(f["severity"] == "unhealthy" for f in pf) else
            "UNKNOWN" if pu else "HEALTHY" if progress else "READY_IDLE" if idle else "UNKNOWN"
        )
        results.append({"pipeline_id": pid, "status": state, "processing_verified": progress and state == "HEALTHY",
                        "outstanding_work": backlog if not missing and source_count is not None else None,
                        "outstanding_work_lower_bound": backlog, "findings": pf, "unknown": pu,
                        "limitations": pl, "telemetry": {
                            "missing_required_counters": missing,
                            "source_inventory": "not_collected" if inventory_not_collected else
                                                "available" if source_count is not None else "unavailable",
                            "known_job_backlog": pending + processing if pending is not None and processing is not None else None,
                            "destination_count": embedded,
                        },
                        "progress": {"embedded_before": (old or {}).get("stats", {}).get("embedded_count"),
                                     "embedded_after": embedded, "last_run": stats.get("last_run")}})
        findings.extend(pf)
        unknown.extend(pu)
        limitations.extend(pl)
    if not results:
        unknown.append("No pipelines observed; processing health is not established")
    statuses = {r["status"] for r in results}
    status = "BLOCKED" if "BLOCKED" in statuses else (
        "UNHEALTHY" if "UNHEALTHY" in statuses or any(f["severity"] == "unhealthy" for f in findings) else
        "UNKNOWN" if unknown or "UNKNOWN" in statuses else
        "HEALTHY" if "HEALTHY" in statuses else "READY_IDLE"
    )
    return {
        "scope": snapshot.get("scope"), "status": status, "observed_at": snapshot.get("observed_at"),
        "baseline_at": (baseline or {}).get("observed_at"),
        "processing_verified": status == "HEALTHY" and any(r["processing_verified"] for r in results),
        "repair_plan": _repair_plan(findings, unknown, snapshot.get("scope")),
        "pipelines": results, "findings": findings, "unknown": sorted(set(unknown)),
        "limitations": sorted(set(limitations)),
        "evidence": {"queues": queues if bus_required else {
                         "required_for_scope": False,
                         "interpretation": "This inline pipeline does not use Service Bus; unrelated queue observations are excluded.",
                     }, "deployments": list(deployments.values()),
                     "pod_signals": cluster.get("signals", [])},
        "interpretation": "READY_IDLE means configured dependencies are ready with no observed work, not verified processing. Shared queues and sampled logs cannot prove a pipeline-specific root cause.",
    }


@tool("diagnose_system", "Bounded system snapshot: readiness, dependencies, queue/DLQ and up to six pipelines. UNKNOWN is not healthy.", Empty)
async def diagnose_system(_p: Empty, **_ctx) -> dict:
    return evaluate(await collect_snapshot())


@tool("diagnose_pipeline", "Correlate pipeline config, model routing, source/destination health, pods, queues and progress. Returns evidence-backed causes and approved-action candidates.", PipelineRef)
async def diagnose_pipeline(p: PipelineRef, **_ctx) -> dict:
    return evaluate(await collect_snapshot(p.pipeline_id))


@tool("verify_recovery", "Read-only two-observation verification using destination progress and fresh dependencies. Idle or insufficient evidence never means repaired.", RecoveryRef)
async def verify_recovery(p: RecoveryRef, **_ctx) -> dict:
    before = await collect_snapshot(p.pipeline_id)
    await asyncio.sleep(SAMPLE_SECONDS)
    return evaluate(await collect_snapshot(p.pipeline_id), before)
