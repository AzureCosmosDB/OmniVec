#!/usr/bin/env python3
"""BGE-Large Text Embedding Service."""

import asyncio
import gc
import os
import time
import io  # lgtm[py/unused-import]
from contextlib import asynccontextmanager
from typing import Optional, List  # lgtm[py/unused-import]

import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel  # lgtm[py/unused-import]

# Azure SDK for managed identity blob access
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient
import httpx

# CUDA optimizations
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

MODEL_NAME = "BAAI/bge-large-en-v1.5"
MODEL_VERSION = "1.0.0"
EMBEDDING_DIM = 1024
DOWNLOAD_TIMEOUT_SECONDS = 120

model = None
tokenizer = None
device = None

# --- Dynamic batching (server-side) ---------------------------------------
# Accumulates concurrent /embed and /v1/embeddings requests for a short
# window and runs a single forward pass on the batch. Dramatically improves
# throughput on the GPU when many small client requests arrive in parallel
# (e.g. multiple .NET workers each sending batches of 50-128 docs).
_DYN_BATCH_MAX = int(os.getenv("BGE_DYN_BATCH_MAX", "256"))
_DYN_BATCH_WAIT_MS = int(os.getenv("BGE_DYN_BATCH_WAIT_MS", "10"))
_DYN_BATCH_DISABLE = os.getenv("BGE_DYN_BATCH_DISABLE", "0") == "1"
_request_queue: "Optional[asyncio.Queue]" = None
_scheduler_task: "Optional[asyncio.Task]" = None


class _BatchItem:
    __slots__ = ("texts", "future")

    def __init__(self, texts, future):
        self.texts = texts
        self.future = future


async def _scheduler_loop():
    """Single consumer: pull requests, group up to _DYN_BATCH_MAX texts
    within a _DYN_BATCH_WAIT_MS window, run one forward pass, fan out."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            first: _BatchItem = await _request_queue.get()
        except asyncio.CancelledError:
            return
        items = [first]
        total = len(first.texts)
        deadline = loop.time() + (_DYN_BATCH_WAIT_MS / 1000.0)
        while total < _DYN_BATCH_MAX:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(_request_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            items.append(item)
            total += len(item.texts)

        flat_texts: List[str] = []
        slices = []
        for it in items:
            start = len(flat_texts)
            flat_texts.extend(it.texts)
            slices.append((it, start, start + len(it.texts)))

        try:
            emb_tensor = await asyncio.to_thread(get_text_embedding, flat_texts)
            emb_list = emb_tensor.cpu().tolist()
            for it, start, end in slices:
                if not it.future.done():
                    it.future.set_result(emb_list[start:end])
        except Exception as e:  # noqa: BLE001
            for it, _s, _e in slices:
                if not it.future.done():
                    it.future.set_exception(e)


async def embed_batched(texts: List[str]) -> List[List[float]]:
    """Submit texts to the scheduler and await the batched result. Falls
    back to direct inference if dynamic batching is disabled."""
    if _DYN_BATCH_DISABLE or _request_queue is None:
        emb = await asyncio.to_thread(get_text_embedding, texts)
        return emb.cpu().tolist()
    fut = asyncio.get_event_loop().create_future()
    await _request_queue.put(_BatchItem(texts, fut))
    return await fut
# --------------------------------------------------------------------------


def load_model():
    global model, tokenizer, device
    from transformers import AutoTokenizer, AutoModel

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Loading BGE-Large model on: {device}")

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    model = AutoModel.from_pretrained(
        MODEL_NAME,
        torch_dtype=dtype,
        low_cpu_mem_usage=True
    ).to(device).eval()

    print(f"BGE-Large model loaded! Embedding dim: {EMBEDDING_DIM}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    global _request_queue, _scheduler_task
    if not _DYN_BATCH_DISABLE:
        _request_queue = asyncio.Queue()
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        print(
            f"Dynamic batching enabled: max={_DYN_BATCH_MAX} wait_ms={_DYN_BATCH_WAIT_MS}"
        )
    yield
    if _scheduler_task is not None:
        _scheduler_task.cancel()


app = FastAPI(
    title="BGE-Large Text Embedding API",
    version="1.0.0",
    lifespan=lifespan
)


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


def is_azure_blob_url(url: str) -> bool:
    """Strict suffix match — the previous substring check was bypassable
    (e.g. ``https://attacker.com/.blob.core.windows.net/``)."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(suf) for suf in _AZURE_BLOB_HOST_SUFFIXES)


def _validate_blob_url(url: str) -> str:
    """Reject SSRF payloads before any outbound HTTP. Returns the URL on
    success, raises ``HTTPException(400)`` on rejection."""
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
    # Reject obvious literal IPs in private ranges.
    if host in ("localhost", "metadata.google.internal", "metadata.azure.internal") \
            or any(host.startswith(p) for p in _PRIVATE_HOST_PREFIXES) \
            or host.startswith("169.254.") \
            or host == "::1":
        raise HTTPException(status_code=400, detail=f"host '{host}' is not allowed")
    allow = _outbound_allowlist()
    if not any(host.endswith(suf) for suf in allow):
        raise HTTPException(status_code=400, detail=f"host '{host}' not in outbound allowlist")
    return url


def download_azure_blob(url: str) -> bytes:
    """Download blob using managed identity with timeout."""
    url = _validate_blob_url(url)
    credential = DefaultAzureCredential()
    blob_client = BlobClient.from_blob_url(url, credential=credential)
    return blob_client.download_blob(timeout=DOWNLOAD_TIMEOUT_SECONDS).readall()


def get_text_embedding(texts: List[str]) -> torch.Tensor:
    """Get normalized text embeddings using BGE-Large."""
    # BGE models work best with a query prefix for retrieval
    # For general embedding, we use texts as-is
    inputs = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=512,
        return_tensors="pt"
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.inference_mode():
        outputs = model(**inputs)
        # Use CLS token embedding (first token)
        embeddings = outputs.last_hidden_state[:, 0]

    # Normalize embeddings
    embeddings = embeddings / embeddings.norm(p=2, dim=-1, keepdim=True)
    return embeddings


@app.get("/health")
async def health():
    return {
        "status": "healthy" if model else "unhealthy",
        "model_loaded": model is not None,
        "device": device or "unknown",
        "gpu_available": torch.cuda.is_available(),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    }


@app.post("/embed")
async def embed(request: Request):
    """Generate embedding for text from URL or direct text (legacy OmniVec API)."""
    start_time = time.time()

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    request_id = body.get("requestId", "")
    blob_url = body.get("blobUrl", "")
    etag = body.get("expectedEtag", "")
    direct_text = body.get("text", "")  # Allow direct text input

    # Get text content either from URL or direct input
    if direct_text:
        text_content = direct_text
    elif blob_url:
        # Download text content
        download_start = time.time()
        try:
            blob_url = _validate_blob_url(blob_url)
            if is_azure_blob_url(blob_url):
                content_bytes = download_azure_blob(blob_url)
            else:
                async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
                    resp = await client.get(blob_url)
                    resp.raise_for_status()
                    content_bytes = resp.content

            # Decode text content
            text_content = content_bytes.decode('utf-8')
        except UnicodeDecodeError:
            # Try other encodings
            try:
                text_content = content_bytes.decode('latin-1')
            except Exception:
                raise HTTPException(status_code=400, detail="Failed to decode text content")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to download: {str(e)}")

        download_time = time.time() - download_start  # lgtm[py/unused-local-variable]
    else:
        raise HTTPException(status_code=400, detail="Either blobUrl or text is required")

    # Truncate very long texts (BGE max is 512 tokens, ~2000 chars is safe)
    if len(text_content) > 8000:
        text_content = text_content[:8000]

    # Generate embedding
    try:
        emb_list = await embed_batched([text_content])
        embedding_list = emb_list[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")

    return JSONResponse(content={
        "requestId": request_id,
        "etagUsed": etag,
        "mediaType": body.get("contentTypeHint", "text/plain"),
        "model": {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "embeddingDim": EMBEDDING_DIM
        },
        "embeddings": [embedding_list],
        "textLength": len(text_content),
        "processingTimeSeconds": round(time.time() - start_time, 3)
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# =============================================================================
# OpenAI-compatible endpoints
# =============================================================================
import base64
import struct


def _serve_openai_id() -> str:
    return MODEL_NAME


_OPENAI_ALIASES = {
    MODEL_NAME,
    "bge-large-en-v1.5",
    "bge-large",
    "text-embedding-bge-large",
    # Common OpenAI names — accepted for drop-in compatibility (still served by BGE).
    "text-embedding-ada-002",
    "text-embedding-3-small",
    "text-embedding-3-large",
}


def _approx_token_count(s: str) -> int:
    # Rough proxy used only for the `usage` field. Real tokenization happens
    # inside the BGE tokenizer; this is a cheap O(n) heuristic.
    return max(1, len(s) // 4)


def _encode_floats(vec, fmt: str) -> object:
    if fmt == "base64":
        packed = struct.pack(f"{len(vec)}f", *vec)
        return base64.b64encode(packed).decode("ascii")
    return vec


@app.get("/v1/models")
async def openai_list_models():
    """OpenAI-compatible model list."""
    return {
        "object": "list",
        "data": [
            {
                "id": _serve_openai_id(),
                "object": "model",
                "created": 0,
                "owned_by": "omnivec",
            }
        ],
    }


@app.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    """OpenAI-compatible embeddings endpoint.

    Spec: https://platform.openai.com/docs/api-reference/embeddings/create
    Request body:
        {
          "input": "text" | ["t1", "t2", ...],
          "model": "<any-supported-id>",
          "encoding_format": "float" | "base64",
          "dimensions": <int>  // optional, truncates to this many dims
        }
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    raw_input = body.get("input")
    if raw_input is None:
        raise HTTPException(status_code=400, detail="`input` is required")

    if isinstance(raw_input, str):
        inputs: List[str] = [raw_input]
    elif isinstance(raw_input, list) and all(isinstance(x, str) for x in raw_input):
        if not raw_input:
            raise HTTPException(status_code=400, detail="`input` list must be non-empty")
        inputs = raw_input
    else:
        raise HTTPException(
            status_code=400,
            detail="`input` must be a string or array of strings",
        )

    requested_model = str(body.get("model") or _serve_openai_id())
    encoding_format = str(body.get("encoding_format") or "float").lower()
    if encoding_format not in ("float", "base64"):
        raise HTTPException(status_code=400, detail="encoding_format must be 'float' or 'base64'")

    dims_override = body.get("dimensions")
    if dims_override is not None:
        try:
            dims_override = int(dims_override)
            if dims_override <= 0 or dims_override > EMBEDDING_DIM:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"`dimensions` must be a positive int <= {EMBEDDING_DIM}",
            )

    try:
        emb_list = await embed_batched(inputs)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    data = []
    prompt_tokens = 0
    for idx, (text, vec) in enumerate(zip(inputs, emb_list)):
        if dims_override is not None:
            vec = vec[:dims_override]
        data.append(
            {
                "object": "embedding",
                "index": idx,
                "embedding": _encode_floats(vec, encoding_format),
            }
        )
        prompt_tokens += _approx_token_count(text)

    return JSONResponse(
        content={
            "object": "list",
            "data": data,
            "model": requested_model if requested_model in _OPENAI_ALIASES else _serve_openai_id(),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        }
    )