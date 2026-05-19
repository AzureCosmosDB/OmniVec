#!/usr/bin/env python3
"""CLIP ViT-Large/14 Image Embedding Service - URL-based API."""

import asyncio
import io
import gc
import os
import time
import requests as http_requests
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional, List  # lgtm[py/unused-import]

_DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=128, thread_name_prefix="clip-dl")

import torch
from PIL import Image
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel  # lgtm[py/unused-import]

# Azure SDK for managed identity blob access
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

# CUDA optimizations
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

MODEL_NAME = "openai/clip-vit-large-patch14"
MODEL_VERSION = "1.0.0"
EMBEDDING_DIM = 768

model = None
processor = None
device = None


def load_model():
    global model, processor, device
    from transformers import CLIPProcessor, CLIPModel

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Loading CLIP model on: {device}")

    processor = CLIPProcessor.from_pretrained(MODEL_NAME)

    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    model = CLIPModel.from_pretrained(
        MODEL_NAME,
        torch_dtype=dtype,
        low_cpu_mem_usage=True
    ).to(device).eval()

    print(f"CLIP model loaded! Embedding dim: {EMBEDDING_DIM}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    task = None
    if not _DYN_BATCH_DISABLE:
        task = asyncio.create_task(_scheduler_loop())
        print(f"CLIP dynamic batching enabled: max={_DYN_BATCH_MAX} wait_ms={_DYN_BATCH_WAIT_MS}")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="CLIP Image Embedding API",
    version="1.0.0",
    lifespan=lifespan
)


# =============================================================================
# Dynamic batching for image embeddings.
#
# The CLIP /embed endpoint historically ran one image per forward pass, which
# leaves the GPU mostly idle when there are many concurrent inflight requests.
# We collect concurrent embed requests for up to CLIP_DYN_BATCH_WAIT_MS or
# until CLIP_DYN_BATCH_MAX images are queued, then run a single batched forward
# pass and fan results back via per-request futures.
# =============================================================================
_DYN_BATCH_MAX = int(os.environ.get("CLIP_DYN_BATCH_MAX", "64"))
_DYN_BATCH_WAIT_MS = int(os.environ.get("CLIP_DYN_BATCH_WAIT_MS", "10"))
_DYN_BATCH_DISABLE = os.environ.get("CLIP_DYN_BATCH_DISABLE", "").lower() in ("1", "true", "yes")
_image_queue: "asyncio.Queue[_ImageBatchItem]" = None  # type: ignore[assignment]


class _ImageBatchItem:
    __slots__ = ("images", "future")

    def __init__(self, images: List["Image.Image"]):
        self.images = images
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()


async def _scheduler_loop():
    """Coroutine: drains queue, batches up to MAX images or WAIT_MS, runs one forward."""
    global _image_queue
    _image_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    while True:
        try:
            first: _ImageBatchItem = await _image_queue.get()
        except asyncio.CancelledError:
            raise

        items: List[_ImageBatchItem] = [first]
        total = len(first.images)
        deadline = loop.time() + (_DYN_BATCH_WAIT_MS / 1000.0)
        while total < _DYN_BATCH_MAX:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                nxt: _ImageBatchItem = await asyncio.wait_for(_image_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            items.append(nxt)
            total += len(nxt.images)

        flat: List["Image.Image"] = []
        for it in items:
            flat.extend(it.images)
        try:
            emb = await asyncio.to_thread(get_image_embedding, flat)
            emb_list = emb.cpu().tolist()
        except Exception as e:
            for it in items:
                if not it.future.done():
                    it.future.set_exception(e)
            continue

        cursor = 0
        for it in items:
            n = len(it.images)
            it.future.set_result(emb_list[cursor:cursor + n])
            cursor += n


async def embed_images_batched(images: List["Image.Image"]) -> List[List[float]]:
    """Submit images to the scheduler and await the batched result."""
    if _DYN_BATCH_DISABLE or _image_queue is None:
        emb = await asyncio.to_thread(get_image_embedding, images)
        return emb.cpu().tolist()
    item = _ImageBatchItem(images)
    await _image_queue.put(item)
    return await item.future


DOWNLOAD_TIMEOUT_SECONDS = 120  # 2 minute timeout for downloads


_AZURE_BLOB_HOST_SUFFIXES = (
    ".blob.core.windows.net",
    ".blob.core.usgovcloudapi.net",
    ".blob.core.chinacloudapi.cn",
    ".blob.core.cloudapi.de",
)

_PRIVATE_HOST_PREFIXES = ("10.", "127.", "169.254.", "192.168.", "0.")


def _outbound_allowlist():
    import os as _os
    raw = _os.getenv("OUTBOUND_HOST_ALLOWLIST", "") or ""
    return _AZURE_BLOB_HOST_SUFFIXES + tuple(
        s.strip().lower() for s in raw.split(",") if s.strip()
    )


def is_azure_blob_url(url: str) -> bool:
    """Strict suffix match — the previous substring check was bypassable."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(suf) for suf in _AZURE_BLOB_HOST_SUFFIXES)


def _validate_blob_url(url: str) -> str:
    """Reject SSRF payloads before any outbound HTTP."""
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
    return url


_AZURE_CRED = None
_HTTP_SESSION = None
_TOKEN_CACHE = {"token": None, "exp": 0.0}


def _get_azure_credential():
    global _AZURE_CRED
    if _AZURE_CRED is None:
        _AZURE_CRED = DefaultAzureCredential()
    return _AZURE_CRED


def _get_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        from requests.adapters import HTTPAdapter
        s = http_requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=256, max_retries=2)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _HTTP_SESSION = s
    return _HTTP_SESSION


def _get_blob_token() -> str:
    """Cached AAD token for Azure Blob (refreshes 60s before expiry)."""
    import time as _t
    now = _t.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] - now > 60:
        return _TOKEN_CACHE["token"]
    cred = _get_azure_credential()
    tk = cred.get_token("https://storage.azure.com/.default")
    _TOKEN_CACHE["token"] = tk.token
    _TOKEN_CACHE["exp"] = float(tk.expires_on)
    return tk.token


def download_azure_blob(url: str) -> bytes:
    """Download blob via plain HTTP with cached AAD token (much faster under parallelism)."""
    url = _validate_blob_url(url)
    headers = {
        "Authorization": f"Bearer {_get_blob_token()}",
        "x-ms-version": "2021-12-02",
    }
    resp = _get_http_session().get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    if resp.status_code == 401:
        # token might have just expired race; force refresh
        _TOKEN_CACHE["exp"] = 0.0
        headers["Authorization"] = f"Bearer {_get_blob_token()}"
        resp = _get_http_session().get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.content


def get_image_embedding(images: List[Image.Image]) -> torch.Tensor:
    """Get normalized image embeddings."""
    inputs = processor(images=images, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.inference_mode():
        outputs = model.get_image_features(**inputs)

    # Normalize embeddings
    embeddings = outputs / outputs.norm(p=2, dim=-1, keepdim=True)
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
    """Generate embedding for image from URL (same API format as DSE-Qwen2)."""
    start_time = time.time()

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    request_id = body.get("requestId", "")
    blob_url = body.get("blobUrl", "")
    etag = body.get("expectedEtag", "")

    # Text path: the docgrok router calls /embed with {"text": "..."} when it
    # needs to embed a search query against a CLIP index. Use the CLIP text
    # tower so the embedding lands in the same vector space as image embeddings.
    text_input = body.get("text")
    if text_input and not blob_url:
        texts = text_input if isinstance(text_input, list) else [text_input]
        try:
            embeddings = get_text_embedding_clip(texts).cpu().tolist()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Text embedding failed: {str(e)}")
        return JSONResponse(content={
            "requestId": request_id,
            "model": {
                "name": MODEL_NAME,
                "version": MODEL_VERSION,
                "embeddingDim": EMBEDDING_DIM,
            },
            "embeddings": embeddings,
            "pages": embeddings,
            "processingTimeSeconds": round(time.time() - start_time, 3),
        })

    if not blob_url:
        raise HTTPException(status_code=400, detail="blobUrl or text is required")

    # Download image
    try:
        blob_url = _validate_blob_url(blob_url)
        if is_azure_blob_url(blob_url):
            # Use managed identity for Azure Blob Storage
            image_bytes = download_azure_blob(blob_url)
        else:
            # Use HTTP for other URLs
            resp = http_requests.get(blob_url, timeout=DOWNLOAD_TIMEOUT_SECONDS, allow_redirects=False)
            resp.raise_for_status()
            image_bytes = resp.content
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image: {str(e)}")

    # Generate embedding
    try:
        embedding_list = (await embed_images_batched([image]))[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")
    finally:
        image.close()

    return JSONResponse(content={
        "requestId": request_id,
        "etagUsed": etag,
        "mediaType": body.get("contentTypeHint", "image/jpeg"),
        "model": {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "embeddingDim": EMBEDDING_DIM
        },
        "embeddings": [embedding_list],
        "processingTimeSeconds": round(time.time() - start_time, 3)
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# =============================================================================
# OpenAI-compatible endpoints (CLIP image embeddings)
#
# OpenAI doesn't ship a native image-embedding API. We extend the standard
# /v1/embeddings shape so callers can pass image URLs (string or list of
# strings) in the `input` field. Text embeddings via CLIP's text tower are
# also supported when `input` is a non-URL string.
# =============================================================================
import base64
import struct


_OPENAI_ALIASES = {
    MODEL_NAME,
    "clip-vit-large-patch14",
    "clip",
    "text-embedding-clip",
}


def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://") or s.startswith("data:image/")


def _encode_floats(vec, fmt: str):
    if fmt == "base64":
        packed = struct.pack(f"{len(vec)}f", *vec)
        return base64.b64encode(packed).decode("ascii")
    return vec


def get_text_embedding_clip(texts: List[str]) -> torch.Tensor:
    """Encode text via the CLIP text tower (shared embedding space)."""
    inputs = processor(text=texts, return_tensors="pt", padding=True, truncation=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.inference_mode():
        outputs = model.get_text_features(**inputs)
    return outputs / outputs.norm(p=2, dim=-1, keepdim=True)


def _download_image_for_clip(url: str) -> Image.Image:
    if url.startswith("data:image/"):
        # data URI: data:image/...;base64,<payload>
        try:
            header, b64 = url.split(",", 1)
            return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid data URI: {e}")
    url = _validate_blob_url(url)
    if is_azure_blob_url(url):
        img_bytes = download_azure_blob(url)
    else:
        resp = http_requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, allow_redirects=False)
        resp.raise_for_status()
        img_bytes = resp.content
    return Image.open(io.BytesIO(img_bytes)).convert("RGB")


@app.get("/v1/models")
async def openai_list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "omnivec",
            }
        ],
    }


@app.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    """OpenAI-compatible embeddings endpoint for CLIP.

    `input` accepts:
      - an image URL (http(s)://... or data:image/...) -> image embedding
      - a plain text string -> text embedding (CLIP text tower)
      - an array of the above (all elements processed independently)

    Standard fields supported: model, encoding_format ('float'|'base64'),
    dimensions (truncates output).
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if model is None or processor is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    raw_input = body.get("input")
    if raw_input is None:
        raise HTTPException(status_code=400, detail="`input` is required")
    if isinstance(raw_input, str):
        items: List[str] = [raw_input]
    elif isinstance(raw_input, list) and all(isinstance(x, str) for x in raw_input):
        if not raw_input:
            raise HTTPException(status_code=400, detail="`input` list must be non-empty")
        items = raw_input
    else:
        raise HTTPException(status_code=400, detail="`input` must be a string or array of strings")

    requested_model = str(body.get("model") or MODEL_NAME)
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

    images: List[Image.Image] = []
    image_idx: List[int] = []
    texts: List[str] = []
    text_idx: List[int] = []
    url_items: List[tuple] = []  # (idx, url)
    for i, item in enumerate(items):
        if _looks_like_url(item):
            url_items.append((i, item))
        else:
            texts.append(item)
            text_idx.append(i)

    if url_items:
        urls_only = [u for _, u in url_items]
        try:
            downloaded = list(_DOWNLOAD_POOL.map(_download_image_for_clip, urls_only))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to load images: {e}")
        for (i, _), img in zip(url_items, downloaded):
            images.append(img)
            image_idx.append(i)

    results: List[Optional[List[float]]] = [None] * len(items)
    try:
        if images:
            img_emb = await embed_images_batched(images)
            for slot, vec in zip(image_idx, img_emb):
                results[slot] = vec
        if texts:
            txt_emb = get_text_embedding_clip(texts).cpu().tolist()
            for slot, vec in zip(text_idx, txt_emb):
                results[slot] = vec
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")
    finally:
        for img in images:
            try:
                img.close()
            except Exception:
                pass

    data = []
    for idx, vec in enumerate(results):
        if vec is None:
            raise HTTPException(status_code=500, detail=f"Missing embedding for input[{idx}]")
        if dims_override is not None:
            vec = vec[:dims_override]
        data.append(
            {
                "object": "embedding",
                "index": idx,
                "embedding": _encode_floats(vec, encoding_format),
            }
        )

    # CLIP doesn't have a 'prompt_tokens' concept for images; approximate.
    prompt_tokens = sum(max(1, len(t) // 4) for t in texts) + len(images)
    return JSONResponse(
        content={
            "object": "list",
            "data": data,
            "model": requested_model if requested_model in _OPENAI_ALIASES else MODEL_NAME,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        }
    )
