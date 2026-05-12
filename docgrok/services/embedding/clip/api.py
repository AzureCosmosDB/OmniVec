#!/usr/bin/env python3
"""CLIP ViT-Large/14 Image Embedding Service - URL-based API."""

import io
import gc
import time
import requests as http_requests
from contextlib import asynccontextmanager
from typing import Optional, List  # lgtm[py/unused-import]

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
    yield


app = FastAPI(
    title="CLIP Image Embedding API",
    version="1.0.0",
    lifespan=lifespan
)


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


def download_azure_blob(url: str) -> bytes:
    """Download blob using managed identity with timeout."""
    url = _validate_blob_url(url)
    credential = DefaultAzureCredential()
    blob_client = BlobClient.from_blob_url(url, credential=credential)
    return blob_client.download_blob(timeout=DOWNLOAD_TIMEOUT_SECONDS).readall()


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

    if not blob_url:
        raise HTTPException(status_code=400, detail="blobUrl is required")

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
        embedding = get_image_embedding([image])
        embedding_list = embedding[0].cpu().tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")
    finally:
        image.close()
        gc.collect()
        if device and device.startswith("cuda"):
            torch.cuda.empty_cache()

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
