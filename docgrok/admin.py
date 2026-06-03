"""DocGrok Admin Control Plane API — Model Registry + K8s Management"""

import os
import uuid
import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
from pipelines import (
    Pipeline, PipelineStep, StepType, PIPELINES,
    LOCAL_FUNCTIONS, MODELS, init_default_pipelines
)
from model_store import ModelStore, create_store

router = APIRouter(prefix="/admin", tags=["admin"])

logger = logging.getLogger(__name__)

# Try to load kubernetes config
try:
    config.load_incluster_config()
    K8S_AVAILABLE = True
except Exception:
    try:
        config.load_kube_config()
        K8S_AVAILABLE = True
    except Exception:
        K8S_AVAILABLE = False

NAMESPACE = os.getenv("NAMESPACE", "docgrok")

# ============================================================================
# MODEL REGISTRY — DocGrok owns all models (native + external)
# ============================================================================

# In-memory registry for external models, keyed by model ID (mdl-ext-{8hex})
_MODEL_REGISTRY: dict = {}

# Native model name → backend URL mapping (set by api.py on startup)
_NATIVE_MODEL_URLS: dict = {}  # lgtm[py/unused-global-variable]

# Persistent store (set by init_store)
_store: ModelStore = None


def init_store():
    """Initialize the model store and load persisted models into cache."""
    global _store
    _store = create_store()
    models = _store.list_models()
    if models:
        _MODEL_REGISTRY.update(models)
        print(f"DocGrok: Loaded {len(models)} external model(s) from store: {list(models.keys())}")
    else:
        print("DocGrok: No persisted models found in store")


def _generate_ext_id() -> str:
    """Generate a unique external model ID."""
    return f"mdl-ext-{uuid.uuid4().hex[:8]}"


def resolve_model(model_id: str) -> Optional[dict]:
    """Return model config, lazy-loading from the persistent store on cache miss.

    Each pipeline-worker replica has its own in-memory _MODEL_REGISTRY. Writes
    via POST /admin/models/registry only update the replica that received the
    request; other replicas would otherwise see stale state until restart. By
    falling back to the shared store on miss, every replica converges without
    needing a broadcast or restart.
    """
    cfg = _MODEL_REGISTRY.get(model_id)
    if cfg is not None:
        return cfg
    if _store is None or not model_id.startswith("mdl-ext-"):
        return None
    try:
        cfg = _store.get_model(model_id)
    except Exception:
        cfg = None
    if cfg:
        _MODEL_REGISTRY[model_id] = cfg
    return cfg


def forget_model(model_id: str) -> None:
    """Remove a model from the local in-memory cache (used after deletes)."""
    _MODEL_REGISTRY.pop(model_id, None)


def sync_cache_from_store() -> None:
    """Reconcile the local in-memory cache against the persistent store.

    Each pipeline-worker replica caches the registry independently. After a
    write hits one replica, the others diverge until restart. Calling this on
    the read path (LIST, name-based duplicate check) makes the cache eventually
    consistent without requiring broadcast or sticky sessions.
    """
    if _store is None:
        return
    try:
        persisted = _store.list_models()
    except Exception:
        return
    # Add or refresh entries that exist in the store
    for mid, cfg in persisted.items():
        _MODEL_REGISTRY[mid] = cfg
    # Drop entries that were deleted on another replica
    stale = [mid for mid in _MODEL_REGISTRY
             if mid.startswith("mdl-ext-") and mid not in persisted]
    for mid in stale:
        _MODEL_REGISTRY.pop(mid, None)


class ScaleRequest(BaseModel):
    replicas: int


# ============================================================================
# MODEL REGISTRY CRUD
# ============================================================================

@router.get("/models/registry")
async def list_registry_models():
    """List all models — native (K8s) + registered external."""
    sync_cache_from_store()
    models = []

    # Native models from K8s deployments
    # Only include deployments labelled as actual ML models (omnivec/role=model).
    # Without this filter every infra deployment in the namespace (omnivec-api,
    # omnivec-web, docgrok-controller, etc.) shows up as a "native model".
    if K8S_AVAILABLE:
        try:
            apps_v1 = client.AppsV1Api()
            deployments = apps_v1.list_namespaced_deployment(
                namespace=NAMESPACE,
                label_selector="omnivec/role=model",
            )
            for dep in deployments.items:
                name = dep.metadata.name
                if name == "docgrok":
                    continue

                labels = dep.metadata.labels or {}
                replicas = dep.spec.replicas or 0
                ready_replicas = dep.status.ready_replicas or 0
                container = dep.spec.template.spec.containers[0]
                resources = container.resources

                gpu_request = "0"
                if resources.requests:
                    gpu_request = resources.requests.get("nvidia.com/gpu", "0")

                # Prefer explicit type label (text/vision/chat) over name heuristic
                model_type = labels.get("omnivec/model-type")
                if not model_type:
                    model_type = "vision" if name in ("dse-qwen2", "clip") else "text"
                models.append({
                    "id": f"mdl-native-{name}",
                    "name": name,
                    "kind": "native",
                    "model_type": model_type,
                    "status": "running" if ready_replicas > 0 else "stopped",
                    "replicas": replicas,
                    "ready_replicas": ready_replicas,
                    "image": container.image,
                    "gpu": gpu_request,
                    "memory": resources.requests.get("memory", "unknown") if resources.requests else "unknown",
                })
        except Exception as e:
            print(f"Error listing K8s models: {e}")

    # External models from registry
    for model_id, cfg in _MODEL_REGISTRY.items():
        models.append({
            "id": model_id,
            "name": cfg.get("name", ""),
            "kind": "external",
            "type": cfg.get("type", ""),
            "endpoint": cfg.get("endpoint", ""),
            "deployment": cfg.get("deployment", ""),
            "embedding_dim": cfg.get("embedding_dim", 0),
            "api_version": cfg.get("api_version", ""),
            "auth_type": cfg.get("auth_type", "key"),
            "status": "available",
        })

    return {"models": models}


@router.post("/models/registry")
async def register_model(request: dict):
    """Register an external model. Accepts optional 'id' to preserve existing ID."""
    name = request.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")

    model_type = request.get("type", "azure-openai")
    endpoint = request.get("endpoint", "").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="'endpoint' is required")

    auth_type = request.get("auth_type", "key")  # "key" or "managed-identity"
    cfg_data = {
        "name": name,
        "type": model_type,
        "endpoint": endpoint,
        "auth_type": auth_type,
        "api_key": request.get("api_key", ""),
        "deployment": request.get("deployment", name),
        "embedding_dim": int(request.get("embedding_dim", 1536)),
        "api_version": request.get("api_version", "2024-06-01"),
    }
    if auth_type == "managed-identity":
        client_id = request.get("client_id", "")
        if client_id:
            cfg_data["client_id"] = client_id

    # Check for duplicate name — update existing.
    # Refresh from store so we see models registered by other replicas.
    sync_cache_from_store()
    for mid, cfg in _MODEL_REGISTRY.items():
        if cfg.get("name") == name:
            cfg_data["api_key"] = request.get("api_key", cfg.get("api_key", ""))
            _MODEL_REGISTRY[mid].update(cfg_data)
            if _store:
                _store.upsert_model(mid, _MODEL_REGISTRY[mid])
            return {"id": mid, **{k: v for k, v in _MODEL_REGISTRY[mid].items() if k != "api_key"}}

    # Use provided ID or generate new one
    model_id = request.get("id", "") if request.get("id", "").startswith("mdl-ext-") else _generate_ext_id()
    _MODEL_REGISTRY[model_id] = cfg_data
    if _store:
        _store.upsert_model(model_id, cfg_data)

    return {"id": model_id, **{k: v for k, v in _MODEL_REGISTRY[model_id].items() if k != "api_key"}}


@router.get("/models/registry/{model_id}")
async def get_registry_model(model_id: str):
    """Get a single model by ID."""
    # External model
    cfg = resolve_model(model_id)
    if cfg is not None:
        return {"id": model_id, **{k: v for k, v in cfg.items() if k != "api_key"}}

    # Native model
    if model_id.startswith("mdl-native-"):
        name = model_id[len("mdl-native-"):]
        if K8S_AVAILABLE:
            try:
                apps_v1 = client.AppsV1Api()
                dep = apps_v1.read_namespaced_deployment(name=name, namespace=NAMESPACE)
                labels = (dep.metadata.labels or {})
                if labels.get("omnivec/role") != "model":
                    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
                replicas = dep.spec.replicas or 0
                ready_replicas = dep.status.ready_replicas or 0
                model_type = labels.get("omnivec/model-type") or ("vision" if name in ("dse-qwen2", "clip") else "text")
                return {
                    "id": model_id,
                    "name": name,
                    "kind": "native",
                    "model_type": model_type,
                    "status": "running" if ready_replicas > 0 else "stopped",
                    "replicas": replicas,
                    "ready_replicas": ready_replicas,
                }
            except HTTPException:
                raise
            except Exception:
                pass

    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")


@router.delete("/models/registry/{model_id}")
async def delete_registry_model(model_id: str):
    """Delete an external model from the registry."""
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    name = cfg.get("name", "")
    if _store:
        _store.delete_model(model_id)
    forget_model(model_id)
    return {"deleted": model_id, "name": name}


@router.post("/models/registry/{model_id}/healthcheck")
async def healthcheck_model(model_id: str):
    """Verify an external model is reachable and the stored api_key is valid.

    api.py callers used to fetch model config (with a redacted api_key) and
    call the upstream themselves — that always 401'd. Doing the probe here
    means the decrypted key never leaves docgrok and we still get an
    authoritative reachability + auth signal.
    """
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    endpoint = cfg.get("endpoint", "").rstrip("/")
    deployment = cfg.get("deployment", cfg.get("name", ""))
    api_version = cfg.get("api_version", "2024-06-01")
    api_key = cfg.get("api_key", "")
    auth_type = cfg.get("auth_type", "key")
    model_type = cfg.get("type", "azure-openai")

    if not endpoint or not deployment:
        return {"ok": False, "status": 0, "detail": "endpoint or deployment not configured"}

    if model_type != "azure-openai":
        return {"ok": False, "status": 0, "detail": f"healthcheck only supports azure-openai (got {model_type})"}

    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
    headers = {"Content-Type": "application/json"}
    if auth_type == "managed-identity":
        try:
            from azure.identity import DefaultAzureCredential
            client_id = cfg.get("client_id") or os.environ.get("AZURE_CLIENT_ID")
            cred = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id else DefaultAzureCredential()
            token = cred.get_token("https://cognitiveservices.azure.com/.default").token
            headers["Authorization"] = f"Bearer {token}"
        except Exception:
            logger.exception("failed to acquire MI token for model %s", model_id)
            return {"ok": False, "status": 0, "detail": "failed to acquire managed-identity token; see server logs"}
    else:
        if not api_key:
            return {"ok": False, "status": 0, "detail": "no api_key configured"}
        headers["api-key"] = api_key

    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=15.0) as c:
        try:
            r = await c.post(url, headers=headers, json={"input": "health check", "model": deployment})
        except Exception:
            logger.exception("healthcheck request failed for model %s", model_id)
            return {"ok": False, "status": 0, "detail": "request failed; see server logs"}

    body = r.text[:200]
    if r.status_code == 200:
        return {"ok": True, "status": 200, "detail": "endpoint reachable, auth valid"}
    if r.status_code == 429:
        return {"ok": True, "status": 429, "detail": "rate limited — auth is valid"}
    return {"ok": False, "status": r.status_code, "detail": body}


# ============================================================================
# API KEY MANAGEMENT (rotate / revoke / status)
# ============================================================================
#
# Plain rotation flow: PUT a new api_key in. model_store encrypts at rest using
# envelope encryption (see docgrok/crypto.py); the hot embed path decrypts
# locally against a cached DEK so we don't pay a Key Vault RTT per embed.

@router.put("/models/registry/{model_id}/api-key")
async def rotate_model_api_key(model_id: str, request: dict):
    """Rotate (or set) the api_key for an external model.

    Body: {"api_key": "<new-key>"}
    The plaintext key is never logged and never persisted in cleartext.
    """
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    new_key = (request.get("api_key") or "").strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="'api_key' is required")
    cfg["api_key"] = new_key
    if _store:
        _store.upsert_model(model_id, cfg)
    return {"id": model_id, "rotated": True}


@router.delete("/models/registry/{model_id}/api-key")
async def revoke_model_api_key(model_id: str):
    """Revoke the api_key for a model (crypto-shred its envelope)."""
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    cfg["api_key"] = ""
    if _store:
        shred = dict(cfg)
        shred["api_key"] = ""
        shred["_clear_api_key"] = True
        _store.upsert_model(model_id, shred)
    return {"id": model_id, "revoked": True}


@router.get("/models/registry/{model_id}/api-key")
async def get_model_api_key_status(model_id: str):
    """Return metadata about whether an api_key is configured (never the key)."""
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return {
        "id": model_id,
        "has_api_key": bool(cfg.get("api_key")),
        "auth_type": cfg.get("auth_type", "key"),
    }


# ============================================================================
# K8S MODEL MANAGEMENT (scale, enable, disable, restart)
# ============================================================================

@router.get("/models")
async def list_k8s_models():
    """List K8s model deployments (legacy endpoint, use /models/registry instead)."""
    if not K8S_AVAILABLE:
        return {"error": "Kubernetes not available", "models": []}

    apps_v1 = client.AppsV1Api()
    models = []
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace=NAMESPACE)
        for dep in deployments.items:
            name = dep.metadata.name
            if name == "docgrok":
                continue
            replicas = dep.spec.replicas or 0
            ready_replicas = dep.status.ready_replicas or 0
            container = dep.spec.template.spec.containers[0]
            resources = container.resources
            gpu_request = "0"
            if resources.requests:
                gpu_request = resources.requests.get("nvidia.com/gpu", "0")
            models.append({
                "name": name,
                "replicas": replicas,
                "readyReplicas": ready_replicas,
                "status": "running" if ready_replicas > 0 else "stopped",
                "image": container.image,
                "gpu": gpu_request,
                "memory": resources.requests.get("memory", "unknown") if resources.requests else "unknown"
            })
    except Exception as e:
        return {"error": str(e), "models": []}  # lgtm[py/stack-trace-exposure]
    return {"models": models}


@router.post("/models/{name}/scale")
async def scale_model(name: str, request: ScaleRequest):
    """Scale a model deployment."""
    if not K8S_AVAILABLE:
        raise HTTPException(status_code=503, detail="Kubernetes not available")
    if request.replicas < 0 or request.replicas > 10:
        raise HTTPException(status_code=400, detail="Replicas must be between 0 and 10")
    apps_v1 = client.AppsV1Api()
    try:
        body = {"spec": {"replicas": request.replicas}}
        apps_v1.patch_namespaced_deployment_scale(name=name, namespace=NAMESPACE, body=body)
        return {"success": True, "name": name, "replicas": request.replicas}
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to scale: {e.reason}")


@router.post("/models/{name}/enable")
async def enable_model(name: str):
    """Enable a model (scale to 1)."""
    return await scale_model(name, ScaleRequest(replicas=1))


@router.post("/models/{name}/disable")
async def disable_model(name: str):
    """Disable a model (scale to 0)."""
    return await scale_model(name, ScaleRequest(replicas=0))


@router.post("/models/{name}/restart")
async def restart_model(name: str):
    """Restart a model deployment."""
    if not K8S_AVAILABLE:
        raise HTTPException(status_code=503, detail="Kubernetes not available")
    apps_v1 = client.AppsV1Api()
    try:
        import datetime
        now = datetime.datetime.utcnow().isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(name=name, namespace=NAMESPACE, body=body)
        return {"success": True, "name": name, "action": "restart"}
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to restart: {e.reason}")


# ============================================================================
# LOGS
# ============================================================================

@router.get("/logs/{name}")
async def get_logs(name: str, lines: int = 100):
    """Get logs from a model deployment."""
    if not K8S_AVAILABLE:
        raise HTTPException(status_code=503, detail="Kubernetes not available")
    core_v1 = client.CoreV1Api()
    try:
        pods = core_v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=f"app={name}")
        if not pods.items:
            return {"name": name, "logs": "No pods found"}
        pod_name = pods.items[0].metadata.name
        logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=NAMESPACE, tail_lines=lines)
        return {"name": name, "pod": pod_name, "logs": logs}
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Failed to get logs: {e.reason}")


# ============================================================================
# SYSTEM INFO
# ============================================================================

@router.get("/system")
async def system_info():
    """Get system information."""
    info = {
        "namespace": NAMESPACE,
        "k8s_available": K8S_AVAILABLE,
        "registered_models": len(_MODEL_REGISTRY),
    }
    if K8S_AVAILABLE:
        try:
            core_v1 = client.CoreV1Api()
            nodes = core_v1.list_node()
            info["nodes"] = len(nodes.items)
            gpus = []
            for node in nodes.items:
                if node.status.capacity:
                    gpu_count = node.status.capacity.get("nvidia.com/gpu", "0")
                    if gpu_count != "0":
                        gpus.append({"node": node.metadata.name, "gpus": gpu_count})
            info["gpu_nodes"] = gpus
        except Exception:  # lgtm[py/empty-except]
            pass
    return info


# ============================================================================
# TRANSFORM PIPELINE MANAGEMENT (disabled for now, but keep CRUD)
# ============================================================================

class CreatePipelineStep(BaseModel):
    id: str
    type: str
    function: Optional[str] = None
    model: Optional[str] = None
    config: dict = {}
    depends_on: List[str] = []


class CreatePipelineRequest(BaseModel):
    name: str
    description: str
    steps: List[CreatePipelineStep]


@router.get("/pipelines")
async def list_pipelines():
    """List all transform pipelines."""
    result = []
    for name, p in PIPELINES.items():
        result.append({
            "name": p.name,
            "description": p.description,
            "steps": [
                {"id": s.id, "type": s.type.value, "function": s.function,
                 "model": s.model, "depends_on": s.depends_on}
                for s in p.steps
            ]
        })
    return {"pipelines": result}


@router.get("/pipelines/options")
async def get_pipeline_options():
    """Get available options for building pipelines."""
    external_models = [
        {"id": mid, "name": cfg.get("name", "")}
        for mid, cfg in _MODEL_REGISTRY.items()
    ]
    return {
        "local_functions": [
            {"name": k, "description": v} for k, v in LOCAL_FUNCTIONS.items()
        ],
        "models": [
            {"name": k, "type": v["type"], "description": v["description"]}
            for k, v in MODELS.items()
        ],
        "external": external_models
    }


@router.get("/pipelines/{name}")
async def get_pipeline(name: str):
    """Get a specific transform pipeline."""
    if name not in PIPELINES:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found")
    p = PIPELINES[name]
    return {
        "name": p.name,
        "description": p.description,
        "steps": [
            {"id": s.id, "type": s.type.value, "function": s.function,
             "model": s.model, "config": s.config, "depends_on": s.depends_on}
            for s in p.steps
        ]
    }


@router.post("/pipelines")
async def create_pipeline(req: CreatePipelineRequest):
    """Create a new transform pipeline."""
    if req.name in PIPELINES:
        raise HTTPException(status_code=400, detail=f"Pipeline '{req.name}' already exists")
    steps = []
    for s in req.steps:
        steps.append(PipelineStep(
            id=s.id, type=StepType(s.type), function=s.function,
            model=s.model, config=s.config, depends_on=s.depends_on
        ))
    pipeline = Pipeline(name=req.name, description=req.description, steps=steps)
    PIPELINES[req.name] = pipeline
    return {"success": True, "name": req.name}


@router.put("/pipelines/{name}")
async def update_pipeline(name: str, req: CreatePipelineRequest):
    """Update an existing transform pipeline."""
    if name not in PIPELINES:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found")
    steps = []
    for s in req.steps:
        steps.append(PipelineStep(
            id=s.id, type=StepType(s.type), function=s.function,
            model=s.model, config=s.config, depends_on=s.depends_on
        ))
    pipeline = Pipeline(name=req.name, description=req.description, steps=steps)
    if name != req.name:
        del PIPELINES[name]
    PIPELINES[req.name] = pipeline
    return {"success": True, "name": req.name}


@router.delete("/pipelines/{name}")
async def delete_pipeline(name: str):
    """Delete a transform pipeline."""
    if name not in PIPELINES:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found")
    del PIPELINES[name]
    return {"success": True, "name": name}


@router.post("/pipelines/{name}/reset")
async def reset_pipelines(name: str = "all"):
    """Reset pipelines to defaults."""
    init_default_pipelines()
    return {"success": True, "message": "Pipelines reset to defaults"}
