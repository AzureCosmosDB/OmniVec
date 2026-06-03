#!/usr/bin/env python3
"""DocGrok - Intelligent document routing to ML backends"""

import os
import base64
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipelines import PIPELINES, PipelineExecutor, StepType
from admin import _MODEL_REGISTRY, _NATIVE_MODEL_URLS, resolve_model

# Backend URLs for local models
DSE_QWEN2_URL = os.getenv("DSE_QWEN2_URL", "http://dse-qwen2-svc:8000")
CLIP_URL = os.getenv("CLIP_URL", "http://clip-svc:8000")
BGE_URL = os.getenv("BGE_URL", "http://bge-svc:8000")
BGE_SMALL_URL = os.getenv("BGE_SMALL_URL", "http://bge-small-svc:8000")

# Content types that should route to CLIP (images)
IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp", "image/tiff"
}

# Content types that should route to BGE (text)
TEXT_TYPES = {
    "text/plain", "text/html", "text/csv", "text/xml",
    "application/json", "application/xml", "application/javascript",
    "text/markdown", "text/x-markdown"
}

app = FastAPI(title="DocGrok", version="6.0.0")


# CR/LF/control-char scrubber on root logger — mitigates py/log-injection.
import logging as _logging
import re as _re

class _CtrlCharLogFilter(_logging.Filter):
    _CTRL_RE = _re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\r\n|\r|\n')

    @classmethod
    def _scrub(cls, v):
        return cls._CTRL_RE.sub(' ', v) if isinstance(v, str) else v

    def filter(self, record):
        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


_logging.getLogger().addFilter(_CtrlCharLogFilter())
for _h in _logging.getLogger().handlers:
    _h.addFilter(_CtrlCharLogFilter())

# ---------------------------------------------------------------------------
# Outbound URL allowlist (mitigates py/full-ssrf, py/partial-ssrf)
# ---------------------------------------------------------------------------

_AZURE_BLOB_HOST_SUFFIXES = (
    ".blob.core.windows.net",
    ".blob.core.usgovcloudapi.net",
    ".blob.core.chinacloudapi.cn",
    ".blob.core.cloudapi.de",
)
_PRIVATE_HOST_PREFIXES = ("10.", "127.", "169.254.", "192.168.", "0.")


def _outbound_allowlist():
    raw = os.getenv("OUTBOUND_HOST_ALLOWLIST", "") or ""
    return _AZURE_BLOB_HOST_SUFFIXES + tuple(
        s.strip().lower() for s in raw.split(",") if s.strip()
    )


def _validate_blob_url(url: str) -> str:
    """Validate a user-supplied blob URL before outbound HTTP fetch."""
    from urllib.parse import urlparse
    if not isinstance(url, str) or not url:
        raise HTTPException(status_code=400, detail="blobUrl must be a non-empty string")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"scheme '{scheme}' not allowed")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="blobUrl must not contain credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="blobUrl must contain a host")
    if host in ("localhost", "metadata.google.internal", "metadata.azure.internal") \
            or any(host.startswith(p) for p in _PRIVATE_HOST_PREFIXES) \
            or host == "::1":
        raise HTTPException(status_code=400, detail=f"host '{host}' is not allowed")
    allow = _outbound_allowlist()
    if not any(host.endswith(suf) for suf in allow):
        raise HTTPException(status_code=400, detail=f"host '{host}' not in outbound allowlist")
    # Rebuild URL from validated parts WITHOUT re-encoding path/query so SAS
    # tokens (which are already percent-encoded by the Azure SDK) survive
    # untouched. Reconstruction breaks string identity, which is enough to
    # mark this as a sanitizer boundary for static analyzers, and we drop
    # userinfo + fragment for defense in depth.
    from urllib.parse import urlunparse
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port}"
    return urlunparse((scheme, netloc, parsed.path or "", "", parsed.query or "", ""))

# Include admin router
from admin import router as admin_router
app.include_router(admin_router)

# Serve static files for web UI
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Async HTTP client
http_client = None

# Native model name → URL mapping
NATIVE_URLS = {}


@app.get("/ui")
async def serve_ui():
    """Serve the control plane web UI."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.on_event("startup")
async def startup():
    global http_client, NATIVE_URLS
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    NATIVE_URLS = {
        "dse-qwen2": DSE_QWEN2_URL,
        "clip": CLIP_URL,
        "bge": BGE_URL,
        "bge-small": BGE_SMALL_URL,
    }
    # Share with admin module
    _NATIVE_MODEL_URLS.update(NATIVE_URLS)

    # Load persisted external models from store (CosmosDB, etc.)
    from admin import init_store
    init_store()


@app.on_event("shutdown")
async def shutdown():
    global http_client
    if http_client:
        await http_client.aclose()


def get_backend_for_content_type(content_type_hint: str) -> tuple[str, str]:
    """Determine which backend to use based on content type hint."""
    if content_type_hint:
        ct = content_type_hint.lower()
        if ct in IMAGE_TYPES:
            return CLIP_URL, "clip"
        if ct in TEXT_TYPES:
            return BGE_URL, "bge"
    return DSE_QWEN2_URL, "dse-qwen2"


async def call_model(model_id: str, text, request_id: str = "") -> dict:
    """Call a model by its ID. Works for both native and external models.
    text can be a string or a list of strings (for batch).
    """
    # Native model
    if model_id.startswith("mdl-native-"):
        name = model_id[len("mdl-native-"):]
        url = NATIVE_URLS.get(name)
        if not url:
            raise HTTPException(status_code=400, detail=f"Unknown native model: '{name}'")

        if isinstance(text, list):
            # Try native /embed/batch first (bge-small supports it)
            try:
                payload = {"texts": text, "model_id": model_id}
                resp = await http_client.post(f"{url}/embed/batch", json=payload, timeout=300)
                resp.raise_for_status()
                r = resp.json()
                return {"embeddings": r.get("embeddings", []), "model_id": model_id}
            except (httpx.HTTPStatusError, httpx.ConnectError):
                # Fallback: use OpenAI-compatible /v1/embeddings which accepts a
                # list and performs a single batched GPU forward pass (BGE, etc.)
                try:
                    payload = {"input": text, "model": model_id}
                    resp = await http_client.post(f"{url}/v1/embeddings", json=payload, timeout=300)
                    resp.raise_for_status()
                    r = resp.json()
                    data = r.get("data", [])
                    results = [None] * len(text)
                    for item in data:
                        idx = int(item.get("index", 0))
                        if 0 <= idx < len(results):
                            results[idx] = item.get("embedding", [])
                    return {"embeddings": results, "model_id": model_id}
                except (httpx.HTTPStatusError, httpx.ConnectError):
                    # Last-resort fallback: per-text /embed (slow, kept for compat)
                    results = []
                    for t in text:
                        payload = {"text": t, "requestId": request_id}
                        resp = await http_client.post(f"{url}/embed", json=payload, timeout=300)
                        resp.raise_for_status()
                        r = resp.json()
                        embedding = r.get("pages", r.get("embeddings", [[]]))[0] if isinstance(r.get("pages", r.get("embeddings")), list) else []
                        results.append(embedding)
                    return {"embeddings": results, "model_id": model_id}
        else:
            payload = {"text": text, "requestId": request_id}
            resp = await http_client.post(f"{url}/embed", json=payload, timeout=300)
            resp.raise_for_status()
            result = resp.json()
            # Normalize response
            pages = result.get("pages") or result.get("embeddings") or []
            return {
                "requestId": request_id,
                "model_id": model_id,
                "pages": pages,
                "model": {"name": name},
            }

    # External model
    if model_id.startswith("mdl-ext-"):
        cfg = resolve_model(model_id)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found in registry")

        model_type = cfg.get("type", "")
        endpoint = cfg["endpoint"]
        api_key = cfg.get("api_key", "")
        deployment = cfg.get("deployment", cfg["name"])
        api_version = cfg.get("api_version", "2024-06-01")
        embedding_dim = cfg.get("embedding_dim", 0)
        auth_type = cfg.get("auth_type", "key")

        input_data = text if isinstance(text, list) else [text]

        if auth_type == "managed-identity":
            # Use DefaultAzureCredential to get a bearer token for Azure AI services
            from azure.identity import DefaultAzureCredential
            client_id = cfg.get("client_id") or os.environ.get("AZURE_CLIENT_ID")
            credential = DefaultAzureCredential(
                managed_identity_client_id=client_id
            ) if client_id else DefaultAzureCredential()
            token = credential.get_token("https://cognitiveservices.azure.com/.default").token

            if model_type == "azure-openai":
                url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            else:
                raise HTTPException(status_code=400, detail="Managed identity auth is only supported for azure-openai models")
        else:
            if not api_key:
                # Try env var fallback
                env_key = f"MODEL_{model_id.upper().replace('-', '_')}_API_KEY"
                api_key = os.environ.get(env_key, "")

            if not api_key:
                raise HTTPException(status_code=500, detail=f"API key not configured for model: {model_id}")

            if model_type == "azure-openai":
                url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
                headers = {"api-key": api_key, "Content-Type": "application/json"}
            elif model_type == "openai":
                url = f"{endpoint}/embeddings"
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported model type: {model_type}")

        payload = {"input": input_data}
        if model_type != "azure-openai":
            payload["model"] = cfg["name"]

        resp = await http_client.post(url, json=payload, headers=headers, timeout=120)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"External API error: {resp.text}")

        result = resp.json()
        embeddings = [item["embedding"] for item in result.get("data", [])]

        if isinstance(text, list):
            # Batch response
            return {
                "embeddings": embeddings,
                "model_id": model_id,
                "usage": result.get("usage", {}),
            }
        else:
            # Single response
            return {
                "requestId": request_id,
                "model_id": model_id,
                "pages": embeddings,
                "model": {
                    "name": cfg["name"],
                    "deployment": deployment,
                    "embeddingDim": embedding_dim,
                },
                "usage": result.get("usage", {}),
            }

    raise HTTPException(status_code=400, detail=f"Invalid model ID format: '{model_id}'")


@app.get("/health")
async def health():
    """Health check."""
    backends = {}

    for name, url in NATIVE_URLS.items():
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{url}/health")
                backends[name] = r.json() if r.status_code == 200 else {"error": str(r.status_code)}
        except Exception as e:
            backends[name] = {"error": str(e)}

    # List registered external models
    external_models = {}
    for model_id, cfg in _MODEL_REGISTRY.items():
        external_models[model_id] = {
            "status": "configured",
            "name": cfg.get("name", ""),
            "type": cfg.get("type", ""),
        }

    return {
        "status": "healthy",
        "service": "DocGrok",
        "version": "6.0.0",
        "backends": backends,
        "external_models": external_models,
    }


@app.post("/embed")
async def embed(request: Request):
    """
    Route to appropriate backend.

    model_id routing (preferred):
      {"model_id": "mdl-ext-a3f19c02", "text": "..."}
      {"model_id": "mdl-native-bge", "text": "..."}

    For transform pipelines:
      {"pipeline": "pdf-vision", "data": "<base64-encoded-data>"}

    For content-type routing (legacy):
      {"blobUrl": "...", "contentTypeHint": "application/pdf"}
    """
    try:
        body = await request.json()
        request_id = body.get("requestId", "")

        # Model ID routing (new)
        model_id = body.get("model_id")
        if model_id:
            text = body.get("text", "")
            blob_name = body.get("blob_name")
            blob_url_in = body.get("blobUrl") or body.get("blob_url")
            blob_names_list = body.get("blob_names")  # bulk: list[str]
            blob_urls_list = body.get("blob_urls") or body.get("blobUrls")  # bulk: list[str]
            is_bulk = isinstance(blob_names_list, list) or isinstance(blob_urls_list, list)

            # Bulk image (blob) routing — N images in a single call. The
            # ingestion worker posts {model_id, blob_names: [...], blob_container,
            # blob_account_url} (or blob_urls: [...]); we construct the URL list
            # once and forward to the backend's /v1/embeddings (OpenAI-compat),
            # which parallel-downloads all images in its _DOWNLOAD_POOL and
            # runs a single batched forward pass. Response is reshaped into
            # {chunks: [...]} (one chunk per input) so callers can keep their
            # per-doc bookkeeping.
            if is_bulk and not text:
                if not model_id.startswith("mdl-native-"):
                    raise HTTPException(status_code=400, detail="Bulk blob embedding only supported for native models")
                name = model_id[len("mdl-native-"):]
                url = NATIVE_URLS.get(name)
                if not url:
                    raise HTTPException(status_code=400, detail=f"Unknown native model: '{name}'")

                if isinstance(blob_urls_list, list) and blob_urls_list:
                    urls = blob_urls_list
                else:
                    account = (body.get("blob_account_url") or "").rstrip("/")
                    container = body.get("blob_container") or ""
                    if not account or not container:
                        raise HTTPException(status_code=400, detail="blob_account_url and blob_container required when using blob_names")
                    urls = [f"{account}/{container}/{n}" for n in (blob_names_list or [])]

                if not urls:
                    return JSONResponse(content={"requestId": request_id, "model_id": model_id, "chunks": [], "_routed_to": model_id})

                payload = {"input": urls, "model": model_id}
                resp = await http_client.post(f"{url}/v1/embeddings", json=payload, timeout=600)
                if resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=f"Backend error: {resp.text}")
                r = resp.json()
                data = r.get("data") or []
                chunks = [{"text": "", "embedding": (d.get("embedding") or [])} for d in data]
                return JSONResponse(content={
                    "requestId": request_id,
                    "model_id": model_id,
                    "chunks": chunks,
                    "_routed_to": model_id,
                })

            # Image (blob) routing for native image models like CLIP. The
            # ingestion worker posts {model_id, blob_name, blob_container,
            # blob_account_url}; we construct a blobUrl and forward to the
            # backend's /embed, then re-shape the response into the
            # {chunks:[{text, embedding}]} format the worker expects.
            if (blob_name or blob_url_in) and not text:
                if not model_id.startswith("mdl-native-"):
                    raise HTTPException(status_code=400, detail="Blob embedding only supported for native models")
                name = model_id[len("mdl-native-"):]
                url = NATIVE_URLS.get(name)
                if not url:
                    raise HTTPException(status_code=400, detail=f"Unknown native model: '{name}'")

                if blob_url_in:
                    blob_url = blob_url_in
                else:
                    account = (body.get("blob_account_url") or "").rstrip("/")
                    container = body.get("blob_container") or ""
                    if not account or not container:
                        raise HTTPException(status_code=400, detail="blob_account_url and blob_container required when using blob_name")
                    blob_url = f"{account}/{container}/{blob_name}"

                payload = {
                    "requestId": request_id,
                    "blobUrl": blob_url,
                    "contentTypeHint": body.get("contentTypeHint", "image/jpeg"),
                    "expectedEtag": body.get("expectedEtag", ""),
                }
                resp = await http_client.post(f"{url}/embed", json=payload, timeout=300)
                if resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=f"Backend error: {resp.text}")
                r = resp.json()
                embeddings = r.get("embeddings") or r.get("pages") or []
                vec = embeddings[0] if embeddings else []
                return JSONResponse(content={
                    "requestId": request_id,
                    "model_id": model_id,
                    "chunks": [{"text": "", "embedding": vec}],
                    "_routed_to": model_id,
                })

            if not text:
                raise HTTPException(status_code=400, detail="'text' field is required for model_id routing")
            result = await call_model(model_id, text, request_id)
            result["_routed_to"] = model_id
            return JSONResponse(content=result)

        # Transform pipeline routing
        pipeline_name = body.get("pipeline")
        if pipeline_name:
            # Mock embedding — instant random vectors for throughput testing
            if pipeline_name == "mock-embedding":
                import random
                dim = int(os.environ.get("MOCK_EMBEDDING_DIM", "128"))
                vec = [random.random() for _ in range(dim)]
                return JSONResponse(content={
                    "output": [vec],
                    "requestId": request_id,
                    "pipeline": "mock-embedding",
                    "model": {"name": "mock", "embeddingDim": dim}
                })

            if pipeline_name == "mock-1536":
                import random
                vec = [random.random() for _ in range(1536)]
                return JSONResponse(content={
                    "output": [vec],
                    "requestId": request_id,
                    "pipeline": "mock-1536",
                    "model": {"name": "mock-1536", "embeddingDim": 1536}
                })

            if pipeline_name not in PIPELINES:
                raise HTTPException(
                    status_code=404,
                    detail=f"Pipeline '{pipeline_name}' not found. Available: {list(PIPELINES.keys())}"
                )

            pipeline = PIPELINES[pipeline_name]

            # For text-only embed requests, resolve the pipeline's embedding model
            # and call it directly (skip the full pipeline which needs pipeline-worker)
            text = body.get("text")
            if text and not body.get("data") and not body.get("blobUrl"):
                embed_model_id = None
                for step in pipeline.steps:
                    if step.type == StepType.MODEL and step.model:
                        embed_model_id = step.model
                        break
                if embed_model_id:
                    result = await call_model(embed_model_id, text, request_id)
                    result["_routed_to"] = embed_model_id
                    result["_resolved_from_pipeline"] = pipeline_name
                    return JSONResponse(content=result)

            input_data = None
            if body.get("data"):
                input_data = base64.b64decode(body["data"])
            elif text:
                input_data = text
            elif body.get("blobUrl"):
                safe_url = _validate_blob_url(body["blobUrl"])
                blob_resp = await http_client.get(safe_url)
                blob_resp.raise_for_status()
                input_data = blob_resp.content
            else:
                raise HTTPException(status_code=400, detail="Pipeline requires 'data', 'text', or 'blobUrl'")

            executor = PipelineExecutor(http_client, NATIVE_URLS, {})
            result = await executor.execute(pipeline, input_data)
            result["requestId"] = request_id
            return JSONResponse(content=result)

        # Content-type routing (legacy — for local models)
        content_type_hint = body.get("contentTypeHint", "")
        backend_url, backend_name = get_backend_for_content_type(content_type_hint)

        resp = await http_client.post(f"{backend_url}/embed", json=body)
        result = resp.json()
        result["_routed_to"] = backend_name

        if "embeddings" in result:
            result["pages"] = result.pop("embeddings")

        return JSONResponse(content=result, status_code=resp.status_code)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Backend timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/embed/batch")
async def embed_batch(request: Request):
    """
    Batch embedding endpoint.

    model_id routing (preferred):
      {"model_id": "mdl-ext-a3f19c02", "texts": ["t1", "t2", ...]}
      {"model_id": "mdl-native-bge", "texts": ["t1", "t2", ...]}

    For transform pipelines:
      {"pipeline": "mock-embedding", "texts": ["t1", "t2", ...]}
    """
    try:
        body = await request.json()
        texts = body.get("texts", [])
        if not texts:
            raise HTTPException(status_code=400, detail="'texts' list is required and must be non-empty")

        model_id = body.get("model_id")
        pipeline_name = body.get("pipeline")

        # Model ID routing (new)
        if model_id:
            result = await call_model(model_id, texts)
            embeddings = result.get("embeddings", [])
            outputs = [[e] for e in embeddings]
            return JSONResponse(content={
                "outputs": outputs,
                "model_id": model_id,
                "batch_size": len(texts),
                "usage": result.get("usage", {}),
                "model": result.get("model", {}),
            })

        # Transform pipeline routing
        if pipeline_name:
            if pipeline_name == "mock-embedding":
                import random
                dim = int(os.environ.get("MOCK_EMBEDDING_DIM", "128"))
                outputs = [[[random.random() for _ in range(dim)]] for _ in texts]
                return JSONResponse(content={
                    "outputs": outputs,
                    "pipeline": "mock-embedding",
                    "batch_size": len(texts),
                })

            if pipeline_name == "mock-1536":
                import random
                outputs = [[[random.random() for _ in range(1536)]] for _ in texts]
                return JSONResponse(content={
                    "outputs": outputs,
                    "pipeline": "mock-1536",
                    "batch_size": len(texts),
                })

            if pipeline_name not in PIPELINES:
                raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_name}' not found")

            pipeline = PIPELINES[pipeline_name]
            executor = PipelineExecutor(http_client, NATIVE_URLS, {})

            is_batchable = (
                len(pipeline.steps) == 1
                and pipeline.steps[0].type == StepType.EXTERNAL
                and not pipeline.steps[0].depends_on
            )

            if is_batchable:
                result = await executor.execute(pipeline, texts)
                embeddings = result.get("output", [])
                outputs = [[e] for e in embeddings]
                return JSONResponse(content={
                    "outputs": outputs,
                    "pipeline": pipeline_name,
                    "batch_size": len(texts),
                })
            else:
                outputs = []
                for text in texts:
                    result = await executor.execute(pipeline, text)
                    outputs.append(result.get("output", []))
                return JSONResponse(content={
                    "outputs": outputs,
                    "pipeline": pipeline_name,
                    "batch_size": len(texts),
                    "batched": False,
                })

        raise HTTPException(status_code=400, detail="Either 'model_id' or 'pipeline' is required")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
