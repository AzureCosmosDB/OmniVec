#!/usr/bin/env python3
"""BGE-Large Text Embedding Service."""

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
    yield


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
    """Generate embedding for text from URL or direct text."""
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
        embedding = get_text_embedding([text_content])
        embedding_list = embedding[0].cpu().tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")
    finally:
        gc.collect()
        if device and device.startswith("cuda"):
            torch.cuda.empty_cache()

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
