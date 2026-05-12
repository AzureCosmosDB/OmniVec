#!/usr/bin/env python3
"""DocGrok Pipeline Worker — PDF → PaddlePaddle OCR → Text → Embedding Model

Generic pipeline: extracts text from PDFs via OCR, then sends text to any
embedding model reachable via the DocGrok router (native K8s, external API, etc.).
The router URL and model are passed in the request or via env vars.

Memory-frugal mode (env-tunable):
  DOCGROK_PDF_DPI            — rasterization DPI for OCR (default 150)
  DOCGROK_PDF_MAX_PAGES      — refuse PDFs with more pages than this (default 500)
  DOCGROK_PDF_OCR_MIN_CHARS  — if page.get_text() returns >= N chars, skip OCR (default 40)
  DOCGROK_PDF_FORCE_OCR      — "1" to always OCR, ignoring embedded text (default 0)
"""

import os
import asyncio
import gc
import re
import json
import base64
import io
import logging
import tempfile
import time
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager

import fitz  # PyMuPDF
import httpx
import numpy as np
from PIL import Image
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Iterator, Any, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pipeline-worker] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# CR/LF/control-char scrubber on root logger — mitigates py/log-injection by
# preventing user-controlled fields from injecting fake log lines or terminal
# escapes. Runs across record.msg AND record.args so %-formatting is covered.
class _CtrlCharLogFilter(logging.Filter):
    _CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\r\n|\r|\n')

    @classmethod
    def _scrub(cls, value):
        return cls._CTRL_RE.sub(' ', value) if isinstance(value, str) else value

    def filter(self, record):
        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


logging.getLogger().addFilter(_CtrlCharLogFilter())
for _h in logging.getLogger().handlers:
    _h.addFilter(_CtrlCharLogFilter())

# ── Config (defaults — can be overridden per-request) ──────────────────
DOCGROK_ROUTER_URL = os.environ.get("DOCGROK_ROUTER_URL", "http://docgrok:80")
# CLIP image-embedding endpoint (Azure ML managed online endpoint exposing
# OpenAI-CLIP-Image-Text-Embeddings-ViT-Large-Patch14-336). Used by the
# `image_embed` stage to embed image bytes directly into 768-dim vectors.
CLIP_ENDPOINT_URL = os.environ.get("CLIP_ENDPOINT_URL", "")
CLIP_API_KEY = os.environ.get("CLIP_API_KEY", "")
DEFAULT_MODEL_ID = os.environ.get("DEFAULT_MODEL_ID", "")

# Memory-frugal PDF knobs
PDF_DPI = int(os.environ.get("DOCGROK_PDF_DPI", "150"))
PDF_MAX_PAGES = int(os.environ.get("DOCGROK_PDF_MAX_PAGES", "500"))
PDF_OCR_MIN_CHARS = int(os.environ.get("DOCGROK_PDF_OCR_MIN_CHARS", "40"))
PDF_FORCE_OCR = os.environ.get("DOCGROK_PDF_FORCE_OCR", "0") == "1"

app = FastAPI(title="DocGrok Pipeline Worker", version="1.1.0")


# ── Pipeline recipe (explicit step-by-step transformation log) ────────
#
# The worker no longer hides the transformation behind a single endpoint.
# Every request runs through a named sequence of steps. Each step records:
#   - op:            the operation kind (download_blob, extract_pdf_pages, …)
#   - status:        running | ok | skipped | failed
#   - duration_ms
#   - input/output:  shape-level summary (sizes, counts), NOT raw payload
#   - notes:         free-form human-readable notes (which branch was taken)
#
# The full execution log is returned to the caller in `pipeline.steps[]`
# so operators can see exactly what happened, in order, and how long
# each transformation took. A static description of the recipe is also
# available at GET /pipeline/recipe.
PIPELINE_NAME = "docgrok-blob-pdf-default"
PIPELINE_VERSION = "3.3.0"


# === STAGE CATALOG =====================================================
# Reusable, parameterized building blocks. A *transform pipeline* is an
# ordered list of stage instances chosen from this catalog, each with
# its own config block. Every transform pipeline must end with exactly
# one `embed` stage that produces the final vectors.
#
# Note: there is intentionally no `filter` stage here. File-type
# selection is hoisted up to each transform pipeline's `applies_to`
# metadata, so transforms remain atomic single-responsibility units.
STAGE_CATALOG: Dict[str, Any] = {
    "extract": {
        "summary": (
            "Extract text from the source. For PDFs, tries the embedded "
            "text fast-path first and falls back to OCR per page when the "
            "embedded text is too short or `force_ocr` is set."
        ),
        "params": {
            "doctype": {
                "type": "enum[auto|pdf|text]", "default": "auto",
                "description": "How to interpret the input. 'auto' = detect from filename / pipeline hint.",
            },
            "ocr_engine": {
                "type": "enum[paddleocr|none]", "default": "paddleocr",
                "description": "OCR backend used when the embedded-text fast-path doesn't apply. 'none' disables OCR entirely.",
            },
            "ocr_min_chars": {
                "type": "int", "default": PDF_OCR_MIN_CHARS,
                "description": "Per-page threshold: if `page.get_text()` returns >= N characters, OCR is skipped for that page.",
            },
            "force_ocr": {
                "type": "bool", "default": PDF_FORCE_OCR,
                "description": "If true, always rasterize+OCR even when embedded text is available.",
            },
            "dpi": {
                "type": "int", "default": PDF_DPI,
                "description": "Rasterization DPI used when OCR is needed. Higher = sharper but more memory.",
            },
            "max_pages": {
                "type": "int", "default": PDF_MAX_PAGES,
                "description": "Reject PDFs with more than N pages.",
            },
        },
    },
    "caption": {
        "summary": (
            "Generate a short natural-language caption for an image, "
            "produced by a vision-capable LLM. The caption replaces the "
            "extracted text and feeds the chunk/embed stages downstream."
        ),
        "params": {
            "vision_model": {
                "type": "str", "default": "mdl-ext-aoai-gpt-4o-vision",
                "description": "Model id of a vision-capable LLM routed by DocGrok.",
            },
            "prompt": {
                "type": "str",
                "default": "Describe this image in 1-3 sentences for retrieval.",
                "description": "Prompt sent alongside the image to elicit a retrieval-friendly description.",
            },
            "max_tokens": {
                "type": "int", "default": 256,
                "description": "Maximum tokens to allow in the generated caption.",
            },
        },
    },
    "chunk": {
        "summary": (
            "Split extracted text into chunks suitable for embedding. "
            "Different strategies produce different chunk shapes; "
            "`overlap_chars` lets neighbouring chunks share context."
        ),
        "params": {
            "strategy": {
                "type": "enum[fixed|paragraph|sentence|recursive]", "default": "recursive",
                "description": (
                    "fixed = hard char split at exact boundaries; "
                    "paragraph = pack consecutive paragraphs (split on blank line) up to max_chars; "
                    "sentence = pack consecutive sentences up to max_chars; "
                    "recursive = prefer paragraph break, then sentence, then hard split."
                ),
            },
            "max_chars": {
                "type": "int", "default": 2000,
                "description": "Maximum characters per chunk.",
            },
            "overlap_chars": {
                "type": "int", "default": 0,
                "description": "Number of trailing characters of each chunk repeated at the start of the next chunk.",
            },
            "min_chars": {
                "type": "int", "default": 0,
                "description": "Drop chunks shorter than this many characters (after trimming).",
            },
        },
    },
    "embed": {
        "summary": (
            "Generate a vector for each chunk via the model router. "
            "Every transform pipeline must end with exactly one embed stage."
        ),
        "params": {
            "model_id": {
                "type": "str|null", "default": None,
                "description": "Model id (mdl-ext-* or mdl-native-*). null = use the model_id from the request.",
            },
            "router_url": {
                "type": "str|null", "default": None,
                "description": "DocGrok router base URL. null = use DOCGROK_ROUTER_URL or the request override.",
            },
            "batch_size": {
                "type": "int", "default": 1,
                "description": "Reserved — currently sends one chunk at a time.",
            },
        },
    },
    "image_embed": {
        "summary": (
            "Embed image bytes directly into a vector by calling an "
            "external CLIP image-embedding endpoint (Azure ML managed "
            "online endpoint). When fed a single image (from blob or "
            "inline_b64) produces one vector. When preceded by an "
            "`extract_frames` stage, embeds each frame in parallel and "
            "produces one vector per frame. Acts as a terminal stage."
        ),
        "params": {
            "endpoint_url": {
                "type": "str|null", "default": None,
                "description": "CLIP /score endpoint URL. null = use CLIP_ENDPOINT_URL env var on the worker.",
            },
            "api_key": {
                "type": "str|null", "default": None,
                "description": "Bearer token for the CLIP endpoint. null = use CLIP_API_KEY env var on the worker.",
            },
            "max_side_px": {
                "type": "int", "default": 512,
                "description": "If set, downscale the longer image side to this many pixels before embedding (faster, smaller payload).",
            },
            "concurrency": {
                "type": "int", "default": 4,
                "description": "Maximum parallel CLIP requests when embedding a list of frames.",
            },
        },
    },
    "extract_frames": {
        "summary": (
            "Decode a video file and extract sampled frames as image "
            "bytes. Frames are placed in the pipeline context for the "
            "next stage (typically `image_embed`) to consume."
        ),
        "params": {
            "every_nth_frame": {
                "type": "int", "default": 30,
                "description": "Sample every Nth decoded frame. For a 30 fps video, N=30 ≈ 1 frame/sec.",
            },
            "max_frames": {
                "type": "int", "default": 60,
                "description": "Hard cap on the total number of frames extracted (memory + cost guard).",
            },
            "start_seconds": {
                "type": "float", "default": 0.0,
                "description": "Skip the first N seconds before sampling.",
            },
            "end_seconds": {
                "type": "float|null", "default": None,
                "description": "Stop sampling at this many seconds. null = process to end of video.",
            },
            "max_side_px": {
                "type": "int", "default": 512,
                "description": "Downscale each extracted frame so its longer side is at most this many pixels.",
            },
            "image_format": {
                "type": "enum[jpeg|png]", "default": "jpeg",
                "description": "Encoding for extracted frames.",
            },
            "jpeg_quality": {
                "type": "int", "default": 88,
                "description": "JPEG quality (1-100) when image_format=jpeg.",
            },
        },
    },
}


# === BUILT-IN TRANSFORM PIPELINES ======================================
# Each transform is an atomic, single-responsibility recipe: one input
# shape (selected by `applies_to`) → terminal `embed` stage → one
# vector per chunk. Multiple transforms can be bound to a single
# ingestion pipeline; the first one whose `applies_to` matches the
# incoming blob wins.
BUILTIN_TRANSFORMS: List[Dict[str, Any]] = [
    {
        "name": "pdf-transform",
        "version": "1.0.0",
        "description": (
            "Extract text from PDFs (embedded fast-path with PaddleOCR "
            "fallback), split into ~2000-char chunks with paragraph-aware "
            "boundaries, embed each chunk."
        ),
        "applies_to": {
            "extensions": [".pdf"],
            "content_types": ["application/pdf"],
        },
        "priority": 10,
        "stages": [
            {
                "type": "extract", "name": "extract_pdf",
                "config": {
                    "doctype": "pdf",
                    "ocr_engine": "paddleocr",
                    "ocr_min_chars": PDF_OCR_MIN_CHARS,
                    "force_ocr": PDF_FORCE_OCR,
                    "dpi": PDF_DPI,
                    "max_pages": PDF_MAX_PAGES,
                },
            },
            {
                "type": "chunk", "name": "chunk_text",
                "config": {"strategy": "recursive", "max_chars": 2000, "overlap_chars": 200, "min_chars": 0},
            },
            {
                "type": "embed", "name": "embed_vectors",
                "config": {"model_id": None, "router_url": None, "batch_size": 1},
            },
        ],
    },
    {
        "name": "text-transform",
        "version": "1.0.0",
        "description": (
            "Decode UTF-8 text, split into paragraph-packed chunks, embed."
        ),
        "applies_to": {
            "extensions": [".txt", ".md", ".markdown", ".json", ".csv",
                           ".tsv", ".log", ".yml", ".yaml", ".xml",
                           ".html", ".htm", ".sql"],
            "content_types": ["text/plain", "text/markdown", "application/json"],
        },
        "priority": 30,
        "stages": [
            {
                "type": "extract", "name": "decode_text",
                "config": {"doctype": "text"},
            },
            {
                "type": "chunk", "name": "chunk_text",
                "config": {"strategy": "paragraph", "max_chars": 1500, "overlap_chars": 100, "min_chars": 0},
            },
            {
                "type": "embed", "name": "embed_vectors",
                "config": {"model_id": None, "router_url": None, "batch_size": 1},
            },
        ],
    },
    {
        "name": "image-transform",
        "version": "1.0.0",
        "output_dimensions": 768,
        "description": (
            "Embed images directly into 768-dim vectors using an external "
            "CLIP image-embedding endpoint. One vector per image."
        ),
        "applies_to": {
            "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"],
            "content_types": ["image/jpeg", "image/png", "image/webp", "image/gif"],
        },
        "priority": 20,
        "stages": [
            {
                "type": "image_embed", "name": "embed_image",
                "config": {"endpoint_url": None, "api_key": None, "max_side_px": 512, "concurrency": 4},
            },
        ],
    },
    {
        "name": "video-transform",
        "version": "1.0.0",
        "output_dimensions": 768,
        "description": (
            "Decode a video, sample frames (every Nth frame, capped), "
            "then embed each frame via CLIP. Produces one 768-dim vector "
            "per sampled frame."
        ),
        "applies_to": {
            "extensions": [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"],
            "content_types": ["video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo"],
        },
        "priority": 15,
        "stages": [
            {
                "type": "extract_frames", "name": "sample_frames",
                "config": {
                    "every_nth_frame": 30,
                    "max_frames": 60,
                    "start_seconds": 0.0,
                    "end_seconds": None,
                    "max_side_px": 512,
                    "image_format": "jpeg",
                    "jpeg_quality": 88,
                },
            },
            {
                "type": "image_embed", "name": "embed_frames",
                "config": {"endpoint_url": None, "api_key": None, "max_side_px": 0, "concurrency": 2},
            },
        ],
    },
]


# Back-compat alias — older code paths still reference DEFAULT_PIPELINE.
DEFAULT_PIPELINE: Dict[str, Any] = BUILTIN_TRANSFORMS[0]


def _validate_transform(t: Dict[str, Any]) -> None:
    """Validate a transform pipeline dict. Raises HTTPException on error.
    Rules:
      - must have a non-empty `name` and a non-empty `stages` list
      - last stage must be of type `embed`
      - every stage type must exist in STAGE_CATALOG
    """
    if not isinstance(t, dict):
        raise HTTPException(status_code=400, detail="transform must be an object")
    name = t.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="transform.name is required")
    stages = t.get("stages")
    if not isinstance(stages, list) or not stages:
        raise HTTPException(status_code=400, detail=f"transform '{name}' must have a non-empty stages[] list")
    for i, st in enumerate(stages):
        stype = (st or {}).get("type")
        if stype not in STAGE_CATALOG:
            raise HTTPException(
                status_code=400,
                detail=f"transform '{name}' stage[{i}] has unknown type '{stype}'. "
                       f"Allowed: {sorted(STAGE_CATALOG.keys())}",
            )
    if stages[-1].get("type") not in ("embed", "image_embed"):
        raise HTTPException(
            status_code=400,
            detail=f"transform '{name}' must end with an 'embed' or 'image_embed' stage (got '{stages[-1].get('type')}')",
        )


_EXT_RE = re.compile(r"\.[A-Za-z0-9]+$")


def _ext_of(name: Optional[str]) -> str:
    if not name:
        return ""
    m = _EXT_RE.search(name)
    return m.group(0).lower() if m else ""


def _transform_matches(t: Dict[str, Any], blob_name: Optional[str],
                       content_type: Optional[str], hint: Optional[str]) -> bool:
    """Decide whether `applies_to` metadata accepts the given input."""
    a = t.get("applies_to") or {}
    exts = [e.lower() for e in (a.get("extensions") or [])]
    cts = [c.lower() for c in (a.get("content_types") or [])]
    hint_match = a.get("match_pipeline_hint")
    ext = _ext_of(blob_name)
    if exts and ext and ext in exts:
        return True
    if cts and content_type and content_type.lower() in cts:
        return True
    if hint_match and hint and hint_match.lower() in hint.lower():
        return True
    # No criteria configured → only match when no other criteria were given either
    if not exts and not cts and not hint_match:
        return True
    return False


def _pick_transform(blob_name: Optional[str],
                    content_type: Optional[str] = None,
                    hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Walk BUILTIN_TRANSFORMS sorted by priority and return the first match."""
    for t in sorted(BUILTIN_TRANSFORMS, key=lambda x: x.get("priority", 100)):
        if _transform_matches(t, blob_name, content_type, hint):
            return t
    return None


@dataclass
class StepRecord:
    name: str
    op: str
    status: str = "pending"  # pending | running | ok | skipped | failed
    duration_ms: float = 0.0
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


class PipelineRunner:
    """Records every transformation step explicitly so the recipe is
    visible end-to-end, both in pod logs and in the response body."""

    def __init__(self, name: str = PIPELINE_NAME, request_id: str = ""):
        self.name = name
        self.version = PIPELINE_VERSION
        self.request_id = request_id
        self.steps: List[StepRecord] = []
        self.started_at = time.time()

    @contextmanager
    def step(self, name: str, op: str, **input_summary):
        rec = StepRecord(name=name, op=op, status="running", input=dict(input_summary))
        self.steps.append(rec)
        rid = f"req={self.request_id} " if self.request_id else ""
        logger.info("%spipeline step START name=%s op=%s in=%s", rid, name, op, input_summary or "{}")  # lgtm[py/log-injection]
        t0 = time.time()
        try:
            yield rec
        except Exception as e:
            rec.duration_ms = round((time.time() - t0) * 1000, 1)
            rec.status = "failed"
            rec.error = f"{type(e).__name__}: {e}"
            logger.error("%spipeline step FAIL  name=%s op=%s ms=%.1f err=%s",
                         rid, name, op, rec.duration_ms, rec.error)  # lgtm[py/log-injection]
            raise
        else:
            rec.duration_ms = round((time.time() - t0) * 1000, 1)
            if rec.status == "running":
                rec.status = "ok"
            logger.info("%spipeline step %s    name=%s op=%s ms=%.1f out=%s notes=%s",
                        rid, rec.status.upper().ljust(5),  # lgtm[py/log-injection]
                        name, op, rec.duration_ms,  # lgtm[py/log-injection]
                        rec.output or "{}", rec.notes or [])

    def skip(self, name: str, op: str, reason: str, **input_summary):
        rec = StepRecord(name=name, op=op, status="skipped",
                         input=dict(input_summary), notes=[reason])
        self.steps.append(rec)
        rid = f"req={self.request_id} " if self.request_id else ""
        logger.info("%spipeline step SKIP  name=%s op=%s reason=%s", rid, name, op, reason)  # lgtm[py/log-injection]
        return rec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "total_ms": round((time.time() - self.started_at) * 1000, 1),
            "steps": [asdict(s) for s in self.steps],
        }

# ── OCR model (lazy init) ──────────────────────────────────────────────
_ocr_engine = None


def get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from paddleocr import PaddleOCR
        logger.info("Loading PaddleOCR model...")
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        logger.info("PaddleOCR model loaded.")
    return _ocr_engine


# ── Request ────────────────────────────────────────────────────────────
class ProcessRequest(BaseModel):
    data: Optional[str] = None       # base64-encoded PDF
    text: Optional[str] = None       # pre-extracted text (skip OCR)
    pipeline: Optional[str] = None
    requestId: Optional[str] = ""
    model_id: Optional[str] = None   # embedding model to use (overrides default)
    router_url: Optional[str] = None # DocGrok router URL (overrides default)
    # Blob reference fields — when present, download from blob storage instead of using data
    blob_name: Optional[str] = None
    blob_container: Optional[str] = None
    blob_account_url: Optional[str] = None
    blob_connection_string: Optional[str] = None
    chunk_size: int = 2000           # characters per chunk for blob/PDF processing
    # Optional inline transform pipeline definition. When present, this
    # transform is executed instead of picking a built-in by file type.
    transform: Optional[Dict[str, Any]] = None
    transform_name: Optional[str] = None  # alternative: pick a built-in by name
    # When set, embeddings whose length differs from this value are dropped
    # (along with their chunk text) before returning. Used by the pipeline
    # layer to enforce a destination's vector_index dimension.
    expected_dim: Optional[int] = None


# ── PDF → text (streaming, memory-frugal) ─────────────────────────────
def _ocr_pixmap(pix) -> str:
    """OCR a single PyMuPDF pixmap. Caller is responsible for freeing pix."""
    ocr = get_ocr()
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        arr = np.asarray(img)
        result = ocr.ocr(arr, cls=True)
    finally:
        # Drop large buffers ASAP
        try:
            img.close()
        except Exception:  # lgtm[py/empty-except]
            pass
        del img
    text = ""
    if result and result[0]:
        text = " ".join(line[1][0] for line in result[0])
    return text


def pdf_extract_text_stream(
    pdf_source,
    *,
    dpi: int = PDF_DPI,
    max_pages: int = PDF_MAX_PAGES,
    ocr_min_chars: int = PDF_OCR_MIN_CHARS,
    force_ocr: bool = PDF_FORCE_OCR,
) -> Iterator[str]:
    """Yield one text string per PDF page without holding all page images in RAM.

    Strategy:
      1. If the page has embedded selectable text >= ocr_min_chars and not
         force_ocr, use that directly (zero rasterization).
      2. Else rasterize one page at the given DPI, OCR it, free buffers, GC.

    pdf_source: bytes, path-like, or fitz.Document.
    """
    if isinstance(pdf_source, fitz.Document):
        doc = pdf_source
        owns_doc = False
    elif isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(pdf_source), filetype="pdf")
        owns_doc = True
    else:
        doc = fitz.open(str(pdf_source))
        owns_doc = True

    try:
        n_pages = doc.page_count
        if n_pages > max_pages:
            raise HTTPException(
                status_code=413,
                detail=f"PDF has {n_pages} pages (max {max_pages}); raise DOCGROK_PDF_MAX_PAGES to allow",
            )

        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for i in range(n_pages):
            page = doc.load_page(i)
            text = ""
            try:
                if not force_ocr:
                    try:
                        embedded = page.get_text("text") or ""
                    except Exception:
                        embedded = ""
                    if len(embedded.strip()) >= ocr_min_chars:
                        text = embedded
                if not text:
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    try:
                        text = _ocr_pixmap(pix)
                    finally:
                        # Free C-level pixmap memory before next page
                        pix = None
            finally:
                page = None
                # Aggressive GC on big PDFs keeps RSS bounded
                if (i & 3) == 0:
                    gc.collect()
            yield text
    finally:
        if owns_doc:
            try:
                doc.close()
            except Exception:  # lgtm[py/empty-except]
                pass
        gc.collect()


def pdf_extract_text_list(pdf_source, **kw) -> list[str]:
    """Materialize streaming output into a list (kept for API compatibility)."""
    return list(pdf_extract_text_stream(pdf_source, **kw))


# ── Backwards-compatible wrappers (deprecated; high memory) ────────────
def pdf_to_images(pdf_bytes: bytes, dpi: int = PDF_DPI) -> list[Image.Image]:
    """Deprecated: rasterizes ALL pages into RAM. Use pdf_extract_text_stream."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        images.append(img)
    doc.close()
    return images


# ── OCR ────────────────────────────────────────────────────────────────
def ocr_images(images: list[Image.Image]) -> list[str]:
    """Deprecated: holds all images in RAM. Use pdf_extract_text_stream."""
    ocr = get_ocr()
    texts = []
    for img in images:
        arr = np.array(img)
        result = ocr.ocr(arr, cls=True)
        page_text = ""
        if result and result[0]:
            page_text = " ".join(line[1][0] for line in result[0])
        texts.append(page_text)
    return texts


# ── Blob Download ─────────────────────────────────────────────────────
def _blob_service_client(connection_string: Optional[str] = None,
                         account_url: Optional[str] = None):
    from azure.storage.blob import BlobServiceClient
    from azure.identity import DefaultAzureCredential
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)
    if account_url:
        return BlobServiceClient(account_url, credential=DefaultAzureCredential())
    raise ValueError("Either blob_connection_string or blob_account_url required")


def _download_blob(container: str, blob_name: str,
                   connection_string: Optional[str] = None,
                   account_url: Optional[str] = None) -> bytes:
    """Download blob into memory (small blobs / text path)."""
    client = _blob_service_client(connection_string, account_url)
    return client.get_container_client(container).get_blob_client(blob_name).download_blob().readall()


def _download_blob_to_file(container: str, blob_name: str,
                           connection_string: Optional[str] = None,
                           account_url: Optional[str] = None,
                           suffix: str = ".pdf") -> str:
    """Stream a blob to a temp file on disk and return its path.

    Avoids holding the entire blob payload in RAM — important for large PDFs
    that would otherwise OOM the worker pod.
    """
    client = _blob_service_client(connection_string, account_url)
    blob_client = client.get_container_client(container).get_blob_client(blob_name)
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="docgrok-")
    try:
        with os.fdopen(fd, "wb") as f:
            stream = blob_client.download_blob(max_concurrency=2)
            stream.readinto(f)
    except Exception:
        try:
            os.unlink(path)
        except Exception:  # lgtm[py/empty-except]
            pass
        raise
    return path


# ── Text Chunking ─────────────────────────────────────────────────────
def _chunk_text(text: str, chunk_size: int = 2000) -> List[str]:
    """Split text into chunks, breaking at paragraph or sentence boundaries."""
    if not text.strip():
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunk = text[start:]
            if chunk.strip():
                chunks.append(chunk)
            break

        # Try paragraph boundary
        para_break = text.rfind("\n\n", start, end)
        if para_break > start:
            end = para_break + 2
        else:
            # Try sentence boundary
            for sep in [". ", ".\n", "! ", "? "]:
                sent_break = text.rfind(sep, start, end)
                if sent_break > start:
                    end = sent_break + len(sep)
                    break

        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end

    return chunks


# ── File-type detection ─────────────────────────────────────────────────
_TEXT_EXTS = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log",
              ".yml", ".yaml", ".xml", ".html", ".htm", ".py", ".js",
              ".ts", ".go", ".java", ".c", ".cpp", ".h", ".sh", ".sql"}
_PDF_EXTS = {".pdf"}


def _is_text_blob(blob_name: Optional[str], pipeline_hint: Optional[str]) -> bool:
    """Decide whether to treat a blob as plain text (skip OCR).

    Priority:
      1. Pipeline name hint: *-text / text-* → text; *-pdf / pdf-* → PDF.
      2. Blob filename extension: .txt/.md/.json/... → text; .pdf → PDF.
      3. Default: PDF (existing behavior for backward compat).
    """
    if pipeline_hint:
        p = pipeline_hint.lower()
        if "text" in p or p.endswith("-txt") or p.startswith("txt-"):
            return True
        if "pdf" in p:
            return False
    if blob_name:
        name = blob_name.lower()
        for ext in _TEXT_EXTS:
            if name.endswith(ext):
                return True
        for ext in _PDF_EXTS:
            if name.endswith(ext):
                return False
    return False  # default → PDF path (unchanged historical behavior)


# ── Embedding via DocGrok Router ───────────────────────────────────────
async def embed_via_router(texts: list[str], model_id: str, router_url: str) -> list[list[float]]:
    """Send texts to the DocGrok router's /embed endpoint for embedding."""
    url = f"{router_url.rstrip('/')}/embed"
    vectors = []

    async with httpx.AsyncClient(timeout=120) as client:
        for i, text in enumerate(texts):
            if not text.strip():
                text = "[empty page]"
            payload = {"text": text, "model_id": model_id}
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("Router embed error for page %d: %d %s", i, resp.status_code, resp.text[:300])
                raise HTTPException(status_code=502, detail=f"Embedding error for page {i}: {resp.status_code}")
            result = resp.json()
            # Router returns {"embeddings": [[...]], ...} or {"pages": [[...]], ...}
            embedding = None
            if "embeddings" in result and result["embeddings"]:
                embedding = result["embeddings"][0]
            elif "pages" in result and result["pages"]:
                embedding = result["pages"][0]
            if embedding is None:
                raise HTTPException(status_code=502, detail=f"No embedding returned for page {i}")
            vectors.append(embedding)

    return vectors


# ── Endpoints ──────────────────────────────────────────────────────────
def pdf_extract_text_stream_with_stats(
    pdf_source,
    stats: Optional[Dict[str, int]] = None,
    **kw,
) -> Iterator[str]:
    """Wraps pdf_extract_text_stream and counts how many pages used the
    embedded-text fast-path vs. the OCR fallback.

    Mutates `stats` in-place with keys: pages_total, pages_embedded, pages_ocr,
    pages_empty.
    """
    if stats is None:
        stats = {}
    stats.setdefault("pages_total", 0)
    stats.setdefault("pages_embedded", 0)
    stats.setdefault("pages_ocr", 0)
    stats.setdefault("pages_empty", 0)

    force_ocr = kw.get("force_ocr", PDF_FORCE_OCR)
    ocr_min_chars = kw.get("ocr_min_chars", PDF_OCR_MIN_CHARS)

    if isinstance(pdf_source, fitz.Document):
        doc = pdf_source
        owns_doc = False
    elif isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(pdf_source), filetype="pdf")
        owns_doc = True
    else:
        doc = fitz.open(str(pdf_source))
        owns_doc = True

    try:
        n_pages = doc.page_count
        max_pages = kw.get("max_pages", PDF_MAX_PAGES)
        if n_pages > max_pages:
            raise HTTPException(
                status_code=413,
                detail=f"PDF has {n_pages} pages (max {max_pages}); raise DOCGROK_PDF_MAX_PAGES to allow",
            )
        dpi = kw.get("dpi", PDF_DPI)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for i in range(n_pages):
            page = doc.load_page(i)
            stats["pages_total"] += 1
            text = ""
            try:
                if not force_ocr:
                    try:
                        embedded = page.get_text("text") or ""
                    except Exception:
                        embedded = ""
                    if len(embedded.strip()) >= ocr_min_chars:
                        text = embedded
                        stats["pages_embedded"] += 1
                if not text:
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    try:
                        text = _ocr_pixmap(pix)
                    finally:
                        pix = None
                    if text.strip():
                        stats["pages_ocr"] += 1
                    else:
                        stats["pages_empty"] += 1
            finally:
                page = None
                if (i & 3) == 0:
                    gc.collect()
            yield text
    finally:
        if owns_doc:
            try:
                doc.close()
            except Exception:  # lgtm[py/empty-except]
                pass
        gc.collect()


# ── Chunking strategies ───────────────────────────────────────────────
def _add_overlap(chunks: List[str], overlap: int) -> List[str]:
    """Prepend the trailing `overlap` chars of each chunk onto the next one."""
    if overlap <= 0 or len(chunks) < 2:
        return chunks
    out = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_tail = out[-1][-overlap:] if len(out[-1]) > overlap else out[-1]
        out.append(prev_tail + chunks[i])
    return out


def _chunk_fixed(text: str, max_chars: int) -> List[str]:
    """Hard char split at exact boundaries — fastest, ignores semantics."""
    if not text:
        return []
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _pack_pieces(pieces: List[str], max_chars: int, separator: str) -> List[str]:
    """Greedily pack consecutive `pieces` (paragraphs/sentences) into
    chunks no larger than max_chars, joining with `separator`. Pieces
    larger than max_chars are hard-split."""
    chunks: List[str] = []
    cur = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_chunk_fixed(piece, max_chars))
            continue
        candidate = piece if not cur else cur + separator + piece
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def _chunk_paragraph(text: str, max_chars: int) -> List[str]:
    return _pack_pieces([p for p in text.split("\n\n") if p.strip()],
                        max_chars, "\n\n")


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _chunk_sentence(text: str, max_chars: int) -> List[str]:
    return _pack_pieces([s for s in _SENTENCE_SPLIT.split(text) if s.strip()],
                        max_chars, " ")


def chunk_with_strategy(
    text: str,
    *,
    strategy: str = "recursive",
    max_chars: int = 2000,
    overlap_chars: int = 0,
    min_chars: int = 0,
) -> List[str]:
    """Apply a named chunking strategy with optional overlap and min size."""
    if not text:
        return []
    if strategy == "fixed":
        chunks = _chunk_fixed(text, max_chars)
    elif strategy == "paragraph":
        chunks = _chunk_paragraph(text, max_chars)
    elif strategy == "sentence":
        chunks = _chunk_sentence(text, max_chars)
    elif strategy == "recursive":
        # Existing behavior: prefer paragraph break, then sentence, then hard split.
        chunks = _chunk_text(text, max_chars)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown chunk strategy: {strategy}")
    if overlap_chars > 0:
        chunks = _add_overlap(chunks, overlap_chars)
    if min_chars > 0:
        chunks = [c for c in chunks if len(c.strip()) >= min_chars]
    return chunks


# ── Stage handlers ────────────────────────────────────────────────────
# Each stage handler receives the StepRecord (to write notes/output into)
# and a mutable `ctx` carrying state across stages. Handlers return
# nothing — they mutate `ctx` (and may set ctx["_skip_pipeline"] = True).
async def _stage_filter(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    blob_name = (ctx.get("blob_name") or "").lower()
    pipeline_hint = (ctx.get("pipeline_hint") or "").lower()
    incl = [e.lower() for e in cfg.get("include_extensions") or []]
    excl = [e.lower() for e in cfg.get("exclude_extensions") or []]
    hint_must = (cfg.get("match_pipeline_hint") or "").lower() or None

    matched = True
    reasons: List[str] = []
    if incl and blob_name:
        if not any(blob_name.endswith(e) for e in incl):
            matched = False
            reasons.append(f"blob does not match include_extensions={incl}")
    if matched and excl and blob_name:
        if any(blob_name.endswith(e) for e in excl):
            matched = False
            reasons.append(f"blob matches exclude_extensions={excl}")
    if matched and hint_must:
        if hint_must not in pipeline_hint:
            matched = False
            reasons.append(f"pipeline hint '{pipeline_hint}' missing required substring '{hint_must}'")

    srec.output["matched"] = matched
    if matched:
        if blob_name:
            srec.notes.append(f"blob '{blob_name}' passes filter")
        else:
            srec.notes.append("no blob name to filter on — admitted by default")
    else:
        ctx["_skip_pipeline"] = True
        ctx["_skip_reason"] = "; ".join(reasons)
        srec.notes.extend(reasons)


async def _stage_extract(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    source_kind = ctx["source_kind"]
    blob_name = ctx.get("blob_name")
    pipeline_hint = ctx.get("pipeline_hint")

    # Resolve doctype
    requested = (cfg.get("doctype") or "auto").lower()
    if requested == "auto":
        if source_kind == "inline_text":
            doctype = "text"
        elif source_kind == "inline_b64":
            doctype = "pdf"
        else:
            doctype = "text" if _is_text_blob(blob_name, pipeline_hint) else "pdf"
        srec.notes.append(f"doctype auto-detected as '{doctype}'")
    else:
        doctype = requested
        srec.notes.append(f"doctype forced to '{doctype}' by config")
    ctx["doctype"] = doctype
    srec.output["doctype"] = doctype

    if doctype == "text":
        if source_kind == "blob":
            blob_bytes = _download_blob(
                ctx["blob_container"], blob_name,
                ctx.get("blob_connection_string"), ctx.get("blob_account_url"),
            )
            full_text = blob_bytes.decode("utf-8", errors="replace")
            srec.notes.append(f"downloaded {len(blob_bytes)} bytes and decoded as utf-8")
        elif source_kind == "inline_text":
            full_text = ctx.get("inline_text") or ""
            srec.notes.append("used pre-extracted text from request")
        else:
            raise HTTPException(status_code=400, detail="text doctype requires text input")
        ctx["page_texts"] = [full_text]
        srec.output["chars"] = len(full_text)
        srec.output["pages_total"] = 1
        return

    # doctype == "pdf"
    pdf_path = ctx.get("pdf_path")
    if not pdf_path:
        if source_kind == "blob":
            pdf_path = _download_blob_to_file(
                ctx["blob_container"], blob_name,
                ctx.get("blob_connection_string"), ctx.get("blob_account_url"),
            )
            srec.notes.append("streamed blob to temp file (no full-payload buffering)")
        elif source_kind == "inline_b64":
            try:
                raw = base64.b64decode(ctx.get("inline_b64") or "")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid base64 data: {e}")
            fd, pdf_path = tempfile.mkstemp(suffix=".pdf", prefix="docgrok-")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            del raw
            gc.collect()
            srec.notes.append("decoded base64 input to temp file")
        else:
            raise HTTPException(status_code=400, detail="pdf doctype requires PDF input")
        ctx["pdf_path"] = pdf_path

    try:
        size = os.path.getsize(pdf_path)
    except Exception:
        size = -1
    srec.output["bytes"] = size

    ocr_engine = (cfg.get("ocr_engine") or "paddleocr").lower()
    force_ocr = bool(cfg.get("force_ocr", PDF_FORCE_OCR))
    ocr_min_chars = int(cfg.get("ocr_min_chars", PDF_OCR_MIN_CHARS))
    dpi = int(cfg.get("dpi", PDF_DPI))
    max_pages = int(cfg.get("max_pages", PDF_MAX_PAGES))

    if ocr_engine == "none":
        # Disable OCR: only embedded text counts; rasterization is suppressed
        # by setting an unreachably high min-chars threshold + force_ocr=False.
        ocr_min_chars = 0
        force_ocr = False
        srec.notes.append("ocr_engine=none — relying on embedded text only")

    stats: Dict[str, int] = {}
    page_texts: List[str] = []
    try:
        for page_text in pdf_extract_text_stream_with_stats(
            pdf_path, stats, dpi=dpi, max_pages=max_pages,
            ocr_min_chars=ocr_min_chars, force_ocr=force_ocr,
        ):
            page_texts.append(page_text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse PDF: {e}")
    ctx["page_texts"] = page_texts
    srec.output.update(stats)
    srec.notes.append(
        f"{stats.get('pages_embedded', 0)} pages embedded fast-path, "
        f"{stats.get('pages_ocr', 0)} pages OCR, "
        f"{stats.get('pages_empty', 0)} pages empty"
    )


async def _stage_chunk(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    page_texts: List[str] = ctx.get("page_texts") or []
    full_text = "\n\n".join(page_texts)
    ctx["full_text"] = full_text

    strategy = (cfg.get("strategy") or "recursive").lower()
    max_chars = int(cfg.get("max_chars", 2000))
    overlap = int(cfg.get("overlap_chars", 0))
    min_chars = int(cfg.get("min_chars", 0))

    chunks = chunk_with_strategy(
        full_text, strategy=strategy, max_chars=max_chars,
        overlap_chars=overlap, min_chars=min_chars,
    )
    if not chunks:
        chunks = [full_text] if full_text.strip() else ["[empty document]"]
        srec.notes.append("no chunks produced — emitted single fallback chunk")

    ctx["chunks"] = chunks
    srec.output["chunk_count"] = len(chunks)
    srec.output["source_chars"] = len(full_text)
    srec.output["avg_chunk_chars"] = round(sum(len(c) for c in chunks) / len(chunks), 1)
    srec.output["max_chunk_chars"] = max(len(c) for c in chunks)
    srec.notes.append(
        f"strategy={strategy} max_chars={max_chars} overlap={overlap} min={min_chars}"
    )


async def _stage_embed(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    chunks: List[str] = ctx.get("chunks") or []
    model_id = cfg.get("model_id") or ctx["model_id"]
    router_url = cfg.get("router_url") or ctx["router_url"]
    if not model_id:
        raise HTTPException(status_code=400, detail="No model_id available for embed stage")
    embeddings = await embed_via_router(chunks, model_id, router_url)
    ctx["embeddings"] = embeddings
    srec.output["vector_count"] = len(embeddings)
    srec.output["dim"] = len(embeddings[0]) if embeddings else 0
    srec.output["model_id"] = model_id


async def _stage_caption(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    """Generate a caption for an image input via a vision-capable LLM.

    Stub MVP: tries the DocGrok router's /caption endpoint with the
    image bytes (base64) and a prompt; on any failure falls back to a
    deterministic placeholder caption derived from the blob name so the
    rest of the pipeline (chunk/embed) still produces a vector.
    """
    source_kind = ctx["source_kind"]
    blob_name = ctx.get("blob_name") or ""
    vision_model = cfg.get("vision_model") or "mdl-ext-aoai-gpt-4o-vision"
    prompt = cfg.get("prompt") or "Describe this image in 1-3 sentences for retrieval."
    max_tokens = int(cfg.get("max_tokens") or 256)

    # Acquire image bytes
    img_bytes: Optional[bytes] = None
    if source_kind == "blob":
        try:
            img_bytes = _download_blob(
                ctx["blob_container"], blob_name,
                ctx.get("blob_connection_string"), ctx.get("blob_account_url"),
            )
            srec.notes.append(f"downloaded {len(img_bytes)} image bytes from blob")
        except Exception as e:
            srec.notes.append(f"blob download failed: {e!r}")
    elif source_kind == "inline_b64":
        try:
            img_bytes = base64.b64decode(ctx.get("inline_b64") or "")
            srec.notes.append(f"decoded {len(img_bytes)} inline image bytes")
        except Exception as e:
            srec.notes.append(f"inline_b64 decode failed: {e!r}")

    caption_text: Optional[str] = None
    if img_bytes:
        router_url = ctx.get("router_url") or DOCGROK_ROUTER_URL
        try:
            url = f"{router_url.rstrip('/')}/caption"
            payload = {
                "model_id": vision_model,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "image_b64": base64.b64encode(img_bytes).decode("ascii"),
            }
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    j = resp.json()
                    caption_text = (j.get("caption") or j.get("text") or "").strip() or None
                    srec.notes.append(f"vision model '{vision_model}' returned {len(caption_text or '')} chars")
                else:
                    srec.notes.append(f"router /caption returned HTTP {resp.status_code}")
        except Exception as e:
            srec.notes.append(f"vision call failed, falling back: {e!r}")

    if not caption_text:
        # Deterministic placeholder so downstream stages still produce a vector.
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", blob_name.rsplit("/", 1)[-1] or "image")
        stem_words = re.sub(r"[\W_]+", " ", stem).strip() or "image"
        caption_text = f"Image titled '{stem_words}'."
        srec.notes.append("used filename-derived placeholder caption (vision route unavailable)")

    ctx["page_texts"] = [caption_text]
    ctx["chunks"] = [caption_text]
    srec.output["caption_chars"] = len(caption_text)
    srec.output["vision_model"] = vision_model


def _load_image_bytes(srec: StepRecord, ctx: Dict[str, Any]) -> Optional[bytes]:
    """Acquire raw image bytes from blob or inline_b64 source. Shared by
    `caption` and `image_embed` stages."""
    source_kind = ctx.get("source_kind")
    blob_name = ctx.get("blob_name") or ""
    if source_kind == "blob":
        try:
            data = _download_blob(
                ctx["blob_container"], blob_name,
                ctx.get("blob_connection_string"), ctx.get("blob_account_url"),
            )
            srec.notes.append(f"downloaded {len(data)} image bytes from blob '{blob_name}'")
            return data
        except Exception as e:
            srec.notes.append(f"blob download failed: {e!r}")
            return None
    if source_kind == "inline_b64":
        try:
            data = base64.b64decode(ctx.get("inline_b64") or "")
            srec.notes.append(f"decoded {len(data)} inline image bytes")
            return data
        except Exception as e:
            srec.notes.append(f"inline_b64 decode failed: {e!r}")
            return None
    srec.notes.append(f"unsupported source_kind '{source_kind}' for image input")
    return None


def _maybe_resize_image(data: bytes, max_side_px: int) -> bytes:
    """Downscale the longer image side to `max_side_px` if larger.
    Re-encodes as JPEG (or PNG if alpha)."""
    if not max_side_px or max_side_px <= 0:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
        long_side = max(w, h)
        if long_side <= max_side_px:
            return data
        scale = max_side_px / long_side
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        resized = img.resize(new_size, Image.LANCZOS)
        out = io.BytesIO()
        if resized.mode in ("RGBA", "LA", "P"):
            resized.convert("RGBA").save(out, format="PNG", optimize=True)
        else:
            resized.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception:
        # If anything goes wrong, fall back to original bytes — CLIP can
        # still process them, the resize is an optimization.
        return data


async def _clip_embed_one(
    client: httpx.AsyncClient,
    endpoint_url: str,
    headers: Dict[str, str],
    img_bytes: bytes,
    max_side_px: int,
    max_retries: int = 4,
) -> List[float]:
    """Send one image to the CLIP endpoint and return its image_features
    vector. Resizes first when max_side_px > 0. Retries with exponential
    backoff on HTTP 429 (rate limit) and 5xx responses."""
    if max_side_px and max_side_px > 0:
        img_bytes = _maybe_resize_image(img_bytes, max_side_px)
    b64 = base64.b64encode(img_bytes).decode("ascii")
    payload = {
        "input_data": {
            "columns": ["image", "text"],
            "index": [0],
            "data": [[b64, ""]],
        }
    }
    last_err = ""
    for attempt in range(max_retries + 1):
        resp = await client.post(endpoint_url, json=payload, headers=headers)
        if resp.status_code == 200:
            out = resp.json()
            if not isinstance(out, list) or not out:
                raise HTTPException(status_code=502, detail=f"image_embed: unexpected CLIP response shape: {str(out)[:300]}")
            vec = out[0].get("image_features")
            if not isinstance(vec, list) or not vec:
                raise HTTPException(status_code=502, detail=f"image_embed: missing image_features in CLIP response: {str(out[0])[:300]}")
            return vec
        last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        # Retry on rate-limit / transient server errors only.
        if resp.status_code not in (429, 500, 502, 503, 504) or attempt == max_retries:
            break
        # Honor Retry-After if present, else exponential backoff capped at 16s.
        retry_after = resp.headers.get("Retry-After")
        try:
            wait_s = float(retry_after) if retry_after else min(16.0, 1.0 * (2 ** attempt))
        except ValueError:
            wait_s = min(16.0, 1.0 * (2 ** attempt))
        await asyncio.sleep(wait_s)
    raise HTTPException(status_code=502, detail=f"image_embed: CLIP endpoint failed after retries: {last_err}")


async def _clip_text_embed_one(
    client: httpx.AsyncClient,
    endpoint_url: str,
    headers: Dict[str, str],
    text: str,
    max_retries: int = 4,
) -> List[float]:
    """Send one text string to the CLIP endpoint and return its
    text_features vector. Used to embed a search query against an
    image-modality index (CLIP is multi-modal: same vector space)."""
    payload = {
        "input_data": {
            "columns": ["image", "text"],
            "index": [0],
            "data": [["", text]],
        }
    }
    last_err = ""
    for attempt in range(max_retries + 1):
        resp = await client.post(endpoint_url, json=payload, headers=headers)
        if resp.status_code == 200:
            out = resp.json()
            if not isinstance(out, list) or not out:
                raise HTTPException(status_code=502, detail=f"image_embed(text): unexpected CLIP response shape: {str(out)[:300]}")
            vec = out[0].get("text_features") or out[0].get("image_features")
            if not isinstance(vec, list) or not vec:
                raise HTTPException(status_code=502, detail=f"image_embed(text): missing text_features in CLIP response: {str(out[0])[:300]}")
            return vec
        last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code not in (429, 500, 502, 503, 504) or attempt == max_retries:
            break
        retry_after = resp.headers.get("Retry-After")
        try:
            wait_s = float(retry_after) if retry_after else min(16.0, 1.0 * (2 ** attempt))
        except ValueError:
            wait_s = min(16.0, 1.0 * (2 ** attempt))
        await asyncio.sleep(wait_s)
    raise HTTPException(status_code=502, detail=f"image_embed(text): CLIP endpoint failed after retries: {last_err}")


async def _stage_image_embed(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    """Embed image bytes via the external CLIP endpoint.

    Two modes:
      * Single image: pulled from blob/inline_b64 source. Produces 1 vector.
      * Multi-frame:  ctx['frames'] is a list of dicts {bytes, label}, set
        by the upstream `extract_frames` stage. Embeds each frame in
        parallel and produces one vector per frame.
    """
    endpoint_url = (cfg.get("endpoint_url") or CLIP_ENDPOINT_URL or "").strip()
    api_key = (cfg.get("api_key") or CLIP_API_KEY or "").strip()
    max_side_px = int(cfg.get("max_side_px") or 0)
    concurrency = max(1, int(cfg.get("concurrency") or 4))
    if not endpoint_url:
        raise HTTPException(
            status_code=500,
            detail="image_embed: no CLIP endpoint configured (set CLIP_ENDPOINT_URL env or stage endpoint_url)",
        )
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="image_embed: no CLIP API key configured (set CLIP_API_KEY env or stage api_key)",
        )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    frames: Optional[List[Dict[str, Any]]] = ctx.get("frames")
    text_query = (ctx.get("inline_text") or "").strip()

    async with httpx.AsyncClient(timeout=120) as client:
        # Text-only mode (search-time query embedding for an image index).
        # CLIP is multi-modal — embedding a text query into the same
        # vector space lets us do text-to-image similarity search.
        if text_query and not frames and not ctx.get("inline_b64") and not ctx.get("blob_name"):
            vec = await _clip_text_embed_one(client, endpoint_url, headers, text_query)
            marker = f"<text:{text_query[:60]}>"
            ctx["chunks"] = [marker]
            ctx["embeddings"] = [vec]
            srec.output["vector_count"] = 1
            srec.output["dim"] = len(vec)
            srec.output["mode"] = "text"
            srec.output["model"] = "openai-clip-vit-large-patch14-336"
            srec.notes.append(f"embedded query text via CLIP text encoder ({len(vec)}-dim)")
            return

        if frames:
            # Multi-frame mode (video).
            sem = asyncio.Semaphore(concurrency)

            async def _one(frame: Dict[str, Any]) -> List[float]:
                async with sem:
                    return await _clip_embed_one(client, endpoint_url, headers, frame["bytes"], max_side_px)

            tasks = [_one(f) for f in frames]
            embeddings = await asyncio.gather(*tasks)
            chunks = [f.get("label") or f"<frame:{i}>" for i, f in enumerate(frames)]
            ctx["chunks"] = chunks
            ctx["embeddings"] = list(embeddings)
            srec.output["vector_count"] = len(embeddings)
            srec.output["dim"] = len(embeddings[0]) if embeddings else 0
            srec.output["mode"] = "frames"
            srec.output["concurrency"] = concurrency
            srec.output["model"] = "openai-clip-vit-large-patch14-336"
            srec.notes.append(f"embedded {len(embeddings)} frames at concurrency {concurrency}")
            return

        # Single-image mode.
        img_bytes = _load_image_bytes(srec, ctx)
        if not img_bytes:
            raise HTTPException(status_code=400, detail="image_embed: no image bytes available from source")
        vec = await _clip_embed_one(client, endpoint_url, headers, img_bytes, max_side_px)

    blob_name = ctx.get("blob_name") or "inline-image"
    marker = f"<image:{blob_name}>"
    ctx["chunks"] = [marker]
    ctx["embeddings"] = [vec]
    srec.output["vector_count"] = 1
    srec.output["dim"] = len(vec)
    srec.output["mode"] = "single"
    srec.output["model"] = "openai-clip-vit-large-patch14-336"
    srec.output["image_bytes"] = len(img_bytes)


def _load_video_bytes(srec: StepRecord, ctx: Dict[str, Any]) -> Optional[bytes]:
    """Acquire raw video bytes from blob or inline_b64. (Video shares the
    same source-fetch logic as images; named separately for clarity.)"""
    return _load_image_bytes(srec, ctx)


def _decode_video_frames(
    video_bytes: bytes,
    every_nth_frame: int,
    max_frames: int,
    start_seconds: float,
    end_seconds: Optional[float],
    max_side_px: int,
    image_format: str,
    jpeg_quality: int,
) -> List[Dict[str, Any]]:
    """Decode video bytes with OpenCV and return a list of sampled
    frames as encoded JPEG/PNG byte strings with metadata.

    Returns: [{"bytes": <encoded>, "label": "<frame:00 t=1.23s>", "index": int, "t": float}, ...]
    """
    import cv2  # local import keeps cold-start lean

    # OpenCV's VideoCapture only takes file paths, so write to a temp file.
    tmp_dir = tempfile.mkdtemp(prefix="vid_")
    tmp_path = os.path.join(tmp_dir, "input.bin")
    with open(tmp_path, "wb") as f:
        f.write(video_bytes)

    frames: List[Dict[str, Any]] = []
    cap = None
    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            raise HTTPException(status_code=400, detail="extract_frames: failed to open video (unsupported codec or corrupt file)")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0  # lgtm[py/unused-local-variable]
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)  # lgtm[py/unused-local-variable]

        if start_seconds and start_seconds > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, float(start_seconds) * 1000.0)

        end_ms: Optional[float] = None
        if end_seconds is not None and end_seconds > 0:
            end_ms = float(end_seconds) * 1000.0

        idx = 0
        kept = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            t_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if end_ms is not None and t_ms > end_ms:
                break
            if idx % max(1, int(every_nth_frame)) == 0:
                # Optional resize.
                if max_side_px and max_side_px > 0:
                    h, w = frame.shape[:2]
                    long_side = max(h, w)
                    if long_side > max_side_px:
                        scale = max_side_px / long_side
                        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                # Encode.
                if (image_format or "jpeg").lower() == "png":
                    ok2, buf = cv2.imencode(".png", frame)
                else:
                    ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
                if ok2:
                    t_s = round(t_ms / 1000.0, 3)
                    frames.append({
                        "bytes": bytes(buf),
                        "label": f"<frame:{kept:03d} t={t_s}s idx={idx}>",
                        "index": idx,
                        "t": t_s,
                    })
                    kept += 1
                    if kept >= max_frames:
                        break
            idx += 1
    finally:
        if cap is not None:
            cap.release()
        try:
            os.remove(tmp_path)
            os.rmdir(tmp_dir)
        except Exception:  # lgtm[py/empty-except]
            pass

    return frames


async def _stage_extract_frames(srec: StepRecord, ctx: Dict[str, Any], cfg: Dict[str, Any]):
    """Decode video bytes and place sampled frames in ctx['frames']."""
    every_nth_frame = max(1, int(cfg.get("every_nth_frame") or 30))
    max_frames = max(1, int(cfg.get("max_frames") or 60))
    start_seconds = float(cfg.get("start_seconds") or 0.0)
    end_seconds_raw = cfg.get("end_seconds")
    end_seconds = float(end_seconds_raw) if end_seconds_raw is not None else None
    max_side_px = int(cfg.get("max_side_px") or 0)
    image_format = (cfg.get("image_format") or "jpeg").lower()
    jpeg_quality = int(cfg.get("jpeg_quality") or 88)

    video_bytes = _load_video_bytes(srec, ctx)
    if not video_bytes:
        raise HTTPException(status_code=400, detail="extract_frames: no video bytes available from source")

    # Decode in a thread to keep the event loop responsive.
    frames = await asyncio.to_thread(
        _decode_video_frames,
        video_bytes, every_nth_frame, max_frames, start_seconds, end_seconds,
        max_side_px, image_format, jpeg_quality,
    )

    if not frames:
        raise HTTPException(status_code=400, detail="extract_frames: no frames extracted (empty/invalid video?)")

    ctx["frames"] = frames
    srec.output["frame_count"] = len(frames)
    srec.output["every_nth_frame"] = every_nth_frame
    srec.output["max_frames"] = max_frames
    srec.output["video_bytes"] = len(video_bytes)
    srec.output["first_frame_t"] = frames[0]["t"]
    srec.output["last_frame_t"] = frames[-1]["t"]
    srec.notes.append(
        f"extracted {len(frames)} frames "
        f"(every {every_nth_frame}th, capped at {max_frames}, t={frames[0]['t']}s-{frames[-1]['t']}s)"
    )


STAGE_HANDLERS = {
    "extract": _stage_extract,
    "caption": _stage_caption,
    "chunk": _stage_chunk,
    "embed": _stage_embed,
    "image_embed": _stage_image_embed,
    "extract_frames": _stage_extract_frames,
}


# ── Declarative pipeline executor ─────────────────────────────────────
async def _execute_pipeline(
    pr: PipelineRunner,
    pipeline_def: Dict[str, Any],
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Walk through pipeline_def['stages'] in order. Each stage runs its
    handler which mutates `ctx`. A filter stage may set `_skip_pipeline`
    to short-circuit. Returns the final assembled response dict."""
    pdf_path_to_clean = None
    try:
        for stage in pipeline_def.get("stages", []):
            stype = stage.get("type")
            sname = stage.get("name") or stype
            cfg = stage.get("config") or {}
            handler = STAGE_HANDLERS.get(stype)
            if handler is None:
                raise HTTPException(status_code=400, detail=f"Unknown stage type: {stype}")

            if ctx.get("_skip_pipeline"):
                pr.skip(sname, stype, f"pipeline short-circuited: {ctx.get('_skip_reason', '')}")
                continue

            with pr.step(sname, stype, **cfg) as srec:
                await handler(srec, ctx, cfg)
                # capture pdf_path for cleanup once known
                if not pdf_path_to_clean and ctx.get("pdf_path"):
                    pdf_path_to_clean = ctx["pdf_path"]

        if ctx.get("_skip_pipeline"):
            return {
                "chunks": [],
                "blob_name": ctx.get("blob_name"),
                "page_count": 0,
                "chunk_count": 0,
                "model_id": ctx.get("model_id"),
                "skipped": True,
                "skip_reason": ctx.get("_skip_reason"),
            }

        chunks = ctx.get("chunks") or []
        embeddings = ctx.get("embeddings") or []

        # Dimension enforcement.
        # 1. The transform definition may declare `output_dimensions` (e.g.
        #    image-transform / video-transform always emit 768-dim CLIP vectors).
        # 2. The caller may pass `expected_dim` to enforce the destination's
        #    vector_index dimension.
        # If the actual embedding length doesn't match either, drop that chunk
        # so we never write a mis-sized vector to the destination.
        declared_dim = pipeline_def.get("output_dimensions")
        expected_dim = ctx.get("expected_dim")
        enforce_dim = expected_dim or declared_dim
        skipped_count = 0
        if enforce_dim and embeddings:
            kept_chunks: List[str] = []
            kept_embs: List[List[float]] = []
            for text, emb in zip(chunks, embeddings):
                if emb is None or len(emb) != int(enforce_dim):
                    skipped_count += 1
                    actual = "None" if emb is None else str(len(emb))
                    logger.warning(
                        "Dim mismatch: dropping chunk (expected=%s, actual=%s, blob=%s)",
                        enforce_dim, actual, ctx.get("blob_name"),  # lgtm[py/log-injection]
                    )
                    continue
                kept_chunks.append(text)
                kept_embs.append(emb)
            chunks = kept_chunks
            embeddings = kept_embs
            ctx["chunks"] = chunks
            ctx["embeddings"] = embeddings

        return {
            "chunks": [
                {"text": text, "embedding": emb}
                for text, emb in zip(chunks, embeddings)
            ],
            "blob_name": ctx.get("blob_name"),
            "page_count": len(ctx.get("page_texts") or []),
            "chunk_count": len(chunks),
            "model_id": ctx.get("model_id"),
            "dim_skipped": skipped_count,
            "embedding_dim": (len(embeddings[0]) if embeddings else None),
        }
    finally:
        if pdf_path_to_clean:
            # Containment check — only unlink files that resolve to a
            # path inside the system temp dir (mitigates py/path-injection).
            try:
                import os as _os
                import tempfile as _tempfile
                resolved = _os.path.realpath(pdf_path_to_clean)
                tmp_root = _os.path.realpath(_tempfile.gettempdir())
                if _os.path.commonpath([resolved, tmp_root]) == tmp_root:
                    _os.unlink(resolved)  # lgtm[py/path-injection]
            except Exception:  # lgtm[py/empty-except]
                pass


# ── Convenience entry point used by both endpoints ────────────────────
async def _run_default_pipeline(
    *,
    pr: PipelineRunner,
    blob_container: Optional[str],
    blob_name: Optional[str],
    blob_account_url: Optional[str],
    blob_connection_string: Optional[str],
    inline_b64: Optional[str],
    inline_text: Optional[str],
    pipeline_hint: Optional[str],
    model_id: str,
    router_url: str,
    chunk_size: int,
    pipeline_def: Optional[Dict[str, Any]] = None,
    expected_dim: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a request context and execute either the supplied
    declarative pipeline or the default. The legacy `chunk_size`
    parameter, if present, overrides the chunk stage's `max_chars`."""
    if blob_container and blob_name:
        source_kind = "blob"
    elif inline_b64:
        source_kind = "inline_b64"
    elif inline_text is not None:
        source_kind = "inline_text"
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide 'blob_name'+'blob_container', 'data' (base64 PDF), or 'text'",
        )

    ctx: Dict[str, Any] = {
        "source_kind": source_kind,
        "blob_container": blob_container,
        "blob_name": blob_name,
        "blob_account_url": blob_account_url,
        "blob_connection_string": blob_connection_string,
        "inline_b64": inline_b64,
        "inline_text": inline_text,
        "pipeline_hint": pipeline_hint,
        "model_id": model_id,
        "router_url": router_url,
        "expected_dim": expected_dim,
    }

    if pipeline_def is not None:
        pdef = pipeline_def
    else:
        # Pick the first built-in transform whose applies_to matches.
        # Inline text input has no extension; default to text-transform.
        pick_name = blob_name if source_kind == "blob" else None
        pdef = _pick_transform(pick_name, hint=pipeline_hint)
        if pdef is None and source_kind in ("inline_b64", "inline_text"):
            # Inline payloads → fall back by source kind
            target = "text-transform" if source_kind == "inline_text" else "pdf-transform"
            pdef = next((t for t in BUILTIN_TRANSFORMS if t["name"] == target), BUILTIN_TRANSFORMS[0])
        if pdef is None:
            pdef = BUILTIN_TRANSFORMS[0]

    # Honour legacy chunk_size override on the first chunk stage.
    if pipeline_def is None and chunk_size and chunk_size != 2000:
        pdef = json.loads(json.dumps(pdef))  # deep copy
        for stage in pdef.get("stages", []):
            if stage.get("type") == "chunk":
                stage.setdefault("config", {})["max_chars"] = chunk_size
                break

    return await _execute_pipeline(pr, pdef, ctx)


@app.post("/process")
async def process(req: ProcessRequest):
    """Pipeline: PDF (base64) | blob | pre-extracted text → chunks + embeddings.

    Runs the canonical recipe explicitly; every step is recorded in the
    response under `pipeline.steps[]`.
    """
    t0 = time.time()
    model_id = req.model_id or DEFAULT_MODEL_ID
    router_url = req.router_url or DOCGROK_ROUTER_URL
    # NOTE: model_id is only required for transforms with a text `embed`
    # stage. Image-only transforms (terminal `image_embed` stage) embed
    # via the CLIP endpoint and don't need a router model_id. Validation
    # is deferred to `_stage_embed` itself.

    pr = PipelineRunner(request_id=req.requestId or "")
    is_blob = bool(req.blob_name and req.blob_container)

    # Resolve an explicit transform if requested.
    explicit_transform: Optional[Dict[str, Any]] = None
    if req.transform:
        _validate_transform(req.transform)
        explicit_transform = req.transform
    elif req.transform_name:
        explicit_transform = next((t for t in BUILTIN_TRANSFORMS if t["name"] == req.transform_name), None)
        if explicit_transform is None:
            raise HTTPException(status_code=404, detail=f"Unknown transform_name: {req.transform_name}")

    if is_blob or req.data:
        result = await _run_default_pipeline(
            pr=pr,
            blob_container=req.blob_container,
            blob_name=req.blob_name,
            blob_account_url=req.blob_account_url,
            blob_connection_string=req.blob_connection_string,
            inline_b64=req.data,
            inline_text=None,
            pipeline_hint=req.pipeline,
            model_id=model_id,
            router_url=router_url,
            chunk_size=req.chunk_size,
            pipeline_def=explicit_transform,
            expected_dim=req.expected_dim,
        )
        result.update({
            "requestId": req.requestId,
            "timing": {"total_seconds": round(time.time() - t0, 2)},
            "pipeline": pr.to_dict(),
        })
        return result

    # Legacy whole-document embedding path (text input → single vector).
    if req.text:
        result = await _run_default_pipeline(
            pr=pr,
            blob_container=None,
            blob_name=None,
            blob_account_url=None,
            blob_connection_string=None,
            inline_b64=None,
            inline_text=req.text,
            pipeline_hint=req.pipeline,
            model_id=model_id,
            router_url=router_url,
            chunk_size=max(req.chunk_size, len(req.text) + 1),  # force single chunk
            pipeline_def=explicit_transform,
            expected_dim=req.expected_dim,
        )
        # Reshape for legacy callers that expect `output` (single doc vector).
        single_vec = result["chunks"][0]["embedding"] if result["chunks"] else []
        return {
            "output": [single_vec],
            "text": req.text,
            "page_texts": [req.text],
            "model_id": model_id,
            "requestId": req.requestId,
            "timing": {"total_seconds": round(time.time() - t0, 2)},
            "pipeline": pr.to_dict(),
        }

    raise HTTPException(
        status_code=400,
        detail="Provide 'blob_name'+'blob_container', 'data' (base64 PDF), or 'text'",
    )


# ── Blob Processing ───────────────────────────────────────────────────

class BlobProcessRequest(BaseModel):
    blob_account_url: Optional[str] = None
    blob_connection_string: Optional[str] = None
    blob_container: str
    blob_name: str
    model_id: Optional[str] = None
    pipeline: Optional[str] = None
    router_url: Optional[str] = None
    chunk_size: int = 2000  # characters per chunk
    transform: Optional[Dict[str, Any]] = None
    transform_name: Optional[str] = None


def download_blob(req: BlobProcessRequest) -> bytes:
    """Download blob content from Azure Blob Storage. (Legacy helper —
    kept for backward compatibility; runtime path uses streaming via
    _download_blob_to_file.)"""
    from azure.storage.blob import BlobServiceClient
    from azure.identity import DefaultAzureCredential

    if req.blob_connection_string:
        client = BlobServiceClient.from_connection_string(req.blob_connection_string)
    elif req.blob_account_url:
        client = BlobServiceClient(req.blob_account_url, credential=DefaultAzureCredential())
    else:
        raise HTTPException(status_code=400, detail="Either blob_connection_string or blob_account_url required")

    container = client.get_container_client(req.blob_container)
    blob = container.get_blob_client(req.blob_name)
    return blob.download_blob().readall()


def chunk_text(text: str, chunk_size: int = 2000) -> List[str]:
    """Public alias of the internal chunker (kept for backwards compat)."""
    return _chunk_text(text, chunk_size)


@app.post("/process/blob")
async def process_blob(req: BlobProcessRequest):
    """Stream blob → per-page text (embedded text or PaddleOCR) → chunk → embed.

    Same canonical recipe as /process; every step is recorded in
    `pipeline.steps[]` for observability.
    """
    t0 = time.time()
    model_id = req.model_id or req.pipeline or DEFAULT_MODEL_ID
    router_url = req.router_url or DOCGROK_ROUTER_URL
    # NOTE: model_id requirement is deferred to the embed stage. See
    # `/process` for rationale (image-only transforms don't need one).

    pr = PipelineRunner(request_id=req.blob_name or "")

    explicit_transform: Optional[Dict[str, Any]] = None
    if req.transform:
        _validate_transform(req.transform)
        explicit_transform = req.transform
    elif req.transform_name:
        explicit_transform = next((t for t in BUILTIN_TRANSFORMS if t["name"] == req.transform_name), None)
        if explicit_transform is None:
            raise HTTPException(status_code=404, detail=f"Unknown transform_name: {req.transform_name}")

    result = await _run_default_pipeline(
        pr=pr,
        blob_container=req.blob_container,
        blob_name=req.blob_name,
        blob_account_url=req.blob_account_url,
        blob_connection_string=req.blob_connection_string,
        inline_b64=None,
        inline_text=None,
        pipeline_hint=req.pipeline,
        model_id=model_id,
        router_url=router_url,
        chunk_size=req.chunk_size,
        pipeline_def=explicit_transform,
        expected_dim=req.expected_dim,
    )
    result.update({
        "timing": {"total_seconds": round(time.time() - t0, 2)},
        "pipeline": pr.to_dict(),
    })
    return result


@app.get("/pipeline/stages/catalog")
async def pipeline_stage_catalog():
    """Return the catalog of reusable, parameterized stage types.

    Each entry describes a stage type (filter, extract, chunk, embed,
    …), its summary, and the schema of its config params (type, default,
    description). Pipelines are composed by ordering instances of these
    stages with concrete config blocks.
    """
    return {"stages": STAGE_CATALOG}


@app.get("/pipeline/recipe")
async def pipeline_recipe():
    """Return the first built-in transform pipeline (back-compat).

    Prefer /transforms for the full list of built-in transforms.
    """
    return DEFAULT_PIPELINE


@app.get("/transforms")
async def list_transforms():
    """Return all built-in transform pipelines.

    Each transform is atomic — it accepts a single input shape (selected
    by `applies_to`) and ends with an `embed` stage that produces the
    final vectors. An ingestion pipeline can bind multiple transforms;
    the dispatcher picks the first whose `applies_to` matches the input.
    """
    return {"transforms": BUILTIN_TRANSFORMS}


@app.get("/transforms/{name}")
async def get_transform(name: str):
    t = next((t for t in BUILTIN_TRANSFORMS if t.get("name") == name), None)
    if not t:
        raise HTTPException(status_code=404, detail=f"Unknown transform: {name}")
    return t


@app.post("/transforms/validate")
async def validate_transform(t: Dict[str, Any]):
    """Validate a candidate transform pipeline definition.

    Returns 200 + {"valid": true, "transform": …} on success, or 400 with
    the failure reason. Used by the API layer when persisting a
    user-defined transform.
    """
    _validate_transform(t)
    return {"valid": True, "transform": t}


@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "healthy",
        "ocr_loaded": _ocr_engine is not None,
        "router_url": DOCGROK_ROUTER_URL,
        "default_model": DEFAULT_MODEL_ID or "(none — must be set per-request)",
        "pipeline": PIPELINE_NAME,
        "pipeline_version": PIPELINE_VERSION,
        "builtin_transforms": [t["name"] for t in BUILTIN_TRANSFORMS],
    }


if __name__ == "__main__":
    import uvicorn
    get_ocr()  # pre-load OCR model
    uvicorn.run(app, host="0.0.0.0", port=8080)
