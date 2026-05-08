#!/usr/bin/env python3
"""FastAPI service for PDF document embedding using DSE-Qwen2 model."""

import io  # lgtm[py/unused-import]
import gc
import time
from contextlib import asynccontextmanager
from typing import Optional

import torch
import fitz
from PIL import Image

# CUDA optimizations
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.set_float32_matmul_precision('high')
from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from pydantic import BaseModel
from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
from qwen_vl_utils import process_vision_info

MODEL_NAME = "MrLight/dse-qwen2-2b-mrl-v1"
DEFAULT_EMBEDDING_DIM = 1536
DEFAULT_BATCH_SIZE = 1  # Reduced for higher DPI
DEFAULT_DPI = 200  # Higher quality
MAX_PAGES_PER_REQUEST = 50  # Reduced for memory
MAX_IMAGE_PIXELS = 800000  # Reduced for higher DPI

model = None
processor = None
device = None


class EmbeddingResponse(BaseModel):
    pdf_name: str
    num_pages: int
    total_pages: int
    start_page: int
    end_page: int
    embedding_dim: int
    embeddings: list[list[float]]
    processing_time_seconds: float
    pages_per_second: float


class QueryRequest(BaseModel):
    query: str


class QueryResult(BaseModel):
    query: str
    embedding: list[float]
    processing_time_seconds: float


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    device: str
    gpu_available: bool
    gpu_name: Optional[str] = None


def load_model():
    global model, processor, device
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Loading model on: {device}")

    processor = AutoProcessor.from_pretrained(
        MODEL_NAME, min_pixels=256, max_pixels=MAX_IMAGE_PIXELS
    )

    dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    try:
        model = Qwen2VLForConditionalGeneration.from_pretrained(
            MODEL_NAME, attn_implementation="flash_attention_2",
            torch_dtype=dtype, low_cpu_mem_usage=True
        ).to(device).eval()
        print("Using Flash Attention 2")
    except Exception as e:
        print(f"Flash Attention 2 not available: {e}")
        model = Qwen2VLForConditionalGeneration.from_pretrained(
            MODEL_NAME, torch_dtype=dtype, low_cpu_mem_usage=True
        ).to(device).eval()

    processor.tokenizer.padding_side = "left"
    model.padding_side = "left"

    # torch.compile disabled - requires full build toolchain
    # Other optimizations (TF32, inference_mode, etc.) still provide speedup

    print("Model loaded!")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    yield

app = FastAPI(title="DSE-Qwen2 PDF Embedding API", version="1.0.0", lifespan=lifespan)


def get_embedding(hidden_state: torch.Tensor, dim: int) -> torch.Tensor:
    reps = hidden_state[:, -1]
    return torch.nn.functional.normalize(reps[:, :dim], p=2, dim=-1)


def pdf_to_images(pdf_bytes: bytes, dpi: int, start_page: int = 0, end_page: int = -1) -> tuple[list[Image.Image], int]:
    """Convert PDF pages to images. Returns (images, total_pages)."""
    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = len(doc)

    if end_page < 0 or end_page > total_pages:
        end_page = min(start_page + MAX_PAGES_PER_REQUEST, total_pages)

    for i in range(start_page, end_page):
        page = doc[i]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        # Resize large images to limit memory
        max_dim = 900  # Reduced for 200 DPI
        if img.width > max_dim or img.height > max_dim:
            ratio = min(max_dim / img.width, max_dim / img.height)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        images.append(img)
        del pix
    doc.close()
    gc.collect()
    return images, total_pages


def encode_images(images: list[Image.Image], batch_size: int, embedding_dim: int) -> torch.Tensor:
    all_embeddings = []
    for i in range(0, len(images), batch_size):
        batch = images[i:i + batch_size]
        messages = [[{'role': 'user', 'content': [
            {'type': 'image', 'image': img},
            {'type': 'text', 'text': 'What is shown in this image?'}
        ]}] for img in batch]

        texts = [processor.apply_chat_template(m, tokenize=False, add_generation_prompt=True) + "<|endoftext|>" for m in messages]
        img_inputs, vid_inputs = process_vision_info(messages)
        inputs = processor(text=texts, images=img_inputs, videos=vid_inputs, padding='longest', return_tensors='pt').to(device)

        with torch.inference_mode():
            output = model(**inputs, return_dict=True, output_hidden_states=True)
        all_embeddings.append(get_embedding(output.hidden_states[-1], embedding_dim).cpu())

        # Aggressive memory cleanup
        del inputs, output, texts, img_inputs, vid_inputs, messages
        gc.collect()
        if device.startswith("cuda"):
            torch.cuda.empty_cache()

    # Clean up images
    for img in images:
        img.close()
    gc.collect()

    return torch.cat(all_embeddings, dim=0)


def encode_query(query: str, embedding_dim: int) -> torch.Tensor:
    message = [{'role': 'user', 'content': [
        {'type': 'image', 'image': Image.new('RGB', (28, 28)), 'resized_height': 1, 'resized_width': 1},
        {'type': 'text', 'text': f'Query: {query}'}
    ]}]
    text = processor.apply_chat_template(message, tokenize=False, add_generation_prompt=True) + "<|endoftext|>"
    img_inputs, vid_inputs = process_vision_info([message])
    inputs = processor(text=[text], images=img_inputs, videos=vid_inputs, padding='longest', return_tensors='pt').to(device)

    with torch.inference_mode():
        output = model(**inputs, return_dict=True, output_hidden_states=True)
    return get_embedding(output.hidden_states[-1], embedding_dim).cpu()


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="healthy" if model else "unhealthy",
        model_loaded=model is not None,
        device=device or "unknown",
        gpu_available=torch.cuda.is_available(),
        gpu_name=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    )


@app.post("/embed/pdf", response_model=EmbeddingResponse)
async def embed_pdf(
    file: UploadFile = File(...),
    batch_size: int = Query(DEFAULT_BATCH_SIZE, ge=1, le=16),
    embedding_dim: int = Query(DEFAULT_EMBEDDING_DIM, ge=256, le=2048),
    dpi: int = Query(DEFAULT_DPI, ge=50, le=300),
    start_page: int = Query(0, ge=0, description="Starting page (0-indexed)"),
    end_page: int = Query(-1, description="Ending page (exclusive, -1 for auto)")
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "File must be a PDF")

    start = time.time()
    pdf_bytes = await file.read()
    images, total_pages = pdf_to_images(pdf_bytes, dpi, start_page, end_page)
    actual_end = min(start_page + len(images), total_pages)
    embeddings = encode_images(images, batch_size, embedding_dim)
    elapsed = time.time() - start

    return EmbeddingResponse(
        pdf_name=file.filename, num_pages=len(images), total_pages=total_pages,
        start_page=start_page, end_page=actual_end, embedding_dim=embedding_dim,
        embeddings=embeddings.tolist(), processing_time_seconds=round(elapsed, 3),
        pages_per_second=round(len(images)/elapsed, 3) if elapsed > 0 else 0
    )


@app.post("/embed/query", response_model=QueryResult)
async def embed_query_endpoint(request: QueryRequest, embedding_dim: int = Query(DEFAULT_EMBEDDING_DIM)):
    start = time.time()
    embedding = encode_query(request.query, embedding_dim)
    return QueryResult(query=request.query, embedding=embedding[0].tolist(), processing_time_seconds=round(time.time()-start, 3))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
