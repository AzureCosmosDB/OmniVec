"""OmniVec Job Processor

Shared module for processing individual jobs and batches.
Used by both the worker (continuous) and the API retry endpoint.

Batch flow: download content for N jobs → single /embed/batch call → write N vectors.
Falls back to individual processing for binary content or on batch errors.
"""

import os
import asyncio
import base64
import hashlib
import logging
from datetime import datetime, timedelta
from typing import List

import httpx
from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from models import Job, JobStatus, SourceType, Source, Pipeline, Destination, DestinationType
from connectors.cosmosdb_connector import SkipDocument
from store import get_store

logger = logging.getLogger(__name__)

DOCGROK_URL = os.getenv("DOCGROK_URL", "http://docgrok:80")

# Validate DocGrok URL at startup — prevent SSRF via env var manipulation
from urllib.parse import urlparse as _urlparse
_parsed_dg = _urlparse(DOCGROK_URL)
if _parsed_dg.hostname and _parsed_dg.hostname not in (
    "docgrok", "docgrok.omnivec", "docgrok.omnivec.svc",
    "docgrok.omnivec.svc.cluster.local", "localhost", "127.0.0.1",
):
    logger.warning("SECURITY: DocGrok URL '%s' points to non-cluster host — verify this is intentional", DOCGROK_URL)

# Reuse a single async HTTP client (caller must set this or we create one)
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    return _http_client


def set_http_client(client: httpx.AsyncClient):
    global _http_client
    _http_client = client


# ── helpers to convert CosmosDB docs to models ──────────────────────────

def _strip_doc(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


def _source_from_doc(doc: dict) -> Source:
    return Source(**_strip_doc(doc))


def _pipeline_from_doc(doc: dict) -> Pipeline:
    return Pipeline(**_strip_doc(doc))


def _destination_from_doc(doc: dict) -> Destination:
    return Destination(**_strip_doc(doc))


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


# ── metrics tracking ───────────────────────────────────────────────────

MAX_METRICS_RETRIES = 8
DAILY_RETENTION_DAYS = 30


def _upsert_ts_bucket(pipeline_id: str, processed: int, failed: int, processing_time_ms: float):
    """Upsert a minute-level timeseries bucket for the given pipeline."""
    if processed == 0 and failed == 0:
        return
    store = get_store()
    now = datetime.utcnow()
    bucket_key = now.strftime("%Y%m%dT%H%M")
    bucket_iso = now.strftime("%Y-%m-%dT%H:%M:00")
    doc_id = f"mts-{pipeline_id}-{bucket_key}"

    try:
        existing = store.get(doc_id, "metric_ts")
        if existing:
            existing["processed"] = existing.get("processed", 0) + processed
            existing["failed"] = existing.get("failed", 0) + failed
            existing["processing_time_ms"] = existing.get("processing_time_ms", 0.0) + processing_time_ms
            store.upsert(existing)
        else:
            store.upsert({
                "id": doc_id,
                "doc_type": "metric_ts",
                "pipeline_id": pipeline_id,
                "bucket": bucket_iso,
                "processed": processed,
                "failed": failed,
                "processing_time_ms": processing_time_ms,
            })
    except Exception as e:
        logger.warning("Failed to upsert TS bucket %s: %s", doc_id, e)


def update_metrics(pipeline_id: str, status: JobStatus, processing_time_ms: float):
    """Increment the global metrics document with etag-based retry (single job)."""
    update_metrics_batch(
        pipeline_id,
        completed=1 if status == JobStatus.COMPLETED else 0,
        failed=1 if status == JobStatus.FAILED else 0,
        total_processing_time_ms=processing_time_ms,
    )


def update_metrics_batch(pipeline_id: str, completed: int, failed: int, total_processing_time_ms: float):
    """Update metrics for an entire batch in one CAS operation. Much less contention."""
    if completed == 0 and failed == 0:
        return

    store = get_store()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cutoff = (datetime.utcnow() - timedelta(days=DAILY_RETENTION_DAYS)).strftime("%Y-%m-%d")

    for attempt in range(MAX_METRICS_RETRIES):
        doc = store.get("global", "metrics")

        if doc is None:
            doc = {
                "id": "global",
                "doc_type": "metrics",
                "events_processed": 0,
                "events_failed": 0,
                "total_processing_time_ms": 0.0,
                "last_updated": None,
                "daily": {},
                "pipelines": {},
            }
            store.upsert(doc)
            doc = store.get("global", "metrics")

        etag = doc.get("_etag")

        doc["events_processed"] = doc.get("events_processed", 0) + completed
        doc["events_failed"] = doc.get("events_failed", 0) + failed
        doc["total_processing_time_ms"] = doc.get("total_processing_time_ms", 0.0) + total_processing_time_ms
        doc["last_updated"] = datetime.utcnow().isoformat()

        # Daily breakdown
        daily = doc.get("daily", {})
        if today not in daily:
            daily[today] = {"processed": 0, "failed": 0, "processing_time_ms": 0.0}
        daily[today]["processed"] += completed
        daily[today]["failed"] += failed
        daily[today]["processing_time_ms"] += total_processing_time_ms
        doc["daily"] = {k: v for k, v in daily.items() if k >= cutoff}

        # Per-pipeline breakdown
        pipelines = doc.get("pipelines", {})
        if pipeline_id not in pipelines:
            pipelines[pipeline_id] = {"processed": 0, "failed": 0, "processing_time_ms": 0.0}
        pipelines[pipeline_id]["processed"] += completed
        pipelines[pipeline_id]["failed"] += failed
        pipelines[pipeline_id]["processing_time_ms"] += total_processing_time_ms
        doc["pipelines"] = pipelines

        try:
            store.replace_with_etag(doc, etag)
            # Also write minute-level timeseries bucket
            _upsert_ts_bucket(pipeline_id, completed, failed, total_processing_time_ms)
            return
        except CosmosAccessConditionFailedError:
            logger.warning("Batch metrics etag conflict (attempt %d/%d)", attempt + 1, MAX_METRICS_RETRIES)
            continue

    logger.error("Failed to update batch metrics after %d retries", MAX_METRICS_RETRIES)


# ── main processing function ────────────────────────────────────────────

JOB_TIMEOUT_SECONDS = 600  # 10 minute wall-clock timeout per job

async def process_job(job: Job) -> None:
    """Process a single job: download content → embed → write vector.

    Updates job status in the store as it progresses.
    Enforces a wall-clock timeout to prevent stuck jobs.
    """
    try:
        async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
            await _process_job_inner(job)
    except asyncio.TimeoutError:
        try:
            store = get_store()
            job.status = JobStatus.FAILED
            job.error = f"Job timed out after {JOB_TIMEOUT_SECONDS}s"
            job.completed_at = datetime.utcnow()
            store.upsert(_to_doc(job, "job"))
            logger.error("Job %s timed out after %ds", job.id, JOB_TIMEOUT_SECONDS)
        except Exception as ue:
            logger.critical("Job %s STUCK in PROCESSING — timeout status update failed: %s", job.id, ue)
        try:
            update_metrics(job.pipeline_id, JobStatus.FAILED, JOB_TIMEOUT_SECONDS * 1000)
        except Exception as me:
            logger.warning("Metrics update failed for timed-out job %s: %s", job.id, me)


async def _process_job_inner(job: Job) -> None:
    """Inner job processing logic (wrapped by timeout in process_job)."""
    store = get_store()
    client = get_http_client()

    try:
        # Mark as PROCESSING
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.utcnow()
        store.upsert(_to_doc(job, "job"))

        # Load pipeline, source, destination
        pipeline_doc = store.get(job.pipeline_id, "pipeline")
        if not pipeline_doc:
            raise ValueError(f"Pipeline '{job.pipeline_id}' not found")
        pipeline = _pipeline_from_doc(pipeline_doc)

        source_doc = store.get(job.source_id, "source")
        if not source_doc:
            raise ValueError(f"Source '{job.source_id}' not found")
        source = _source_from_doc(source_doc)

        # Validate source_ref to prevent path traversal
        if ".." in job.source_ref or job.source_ref.startswith("/"):
            raise ValueError(f"Invalid source_ref '{job.source_ref}': path traversal not allowed")

        dest_doc = store.get(pipeline.destination_id, "destination")
        if not dest_doc:
            raise ValueError(f"Destination '{pipeline.destination_id}' not found")
        destination = _destination_from_doc(dest_doc)

        # Download content from source
        if source.type == SourceType.AZURE_BLOB:
            from connectors.blob_connector import download_blob
            content = await download_blob(source.config, job.source_ref)
        elif source.type == SourceType.COSMOSDB:
            from connectors.cosmosdb_connector import get_document
            content, content_hash = await get_document(source.config, job.source_ref)
        elif source.type == SourceType.POSTGRESQL:
            # PostgreSQL content is already in the job payload
            payload_data = job.metadata.get("payload", {}) if hasattr(job, 'metadata') else {}
            if not payload_data and hasattr(job, 'payload'):
                payload_data = job.payload or {}
            content = payload_data.get("content", "")
            if not content:
                # Fetch from PostgreSQL if not in payload
                from connectors.postgres_connector import get_row_by_id
                row = await get_row_by_id(source.config, job.source_ref)
                content = row.get("_content", "") if row else ""
        else:
            raise ValueError(f"Unsupported source type: {source.type}")

        # Decode text files
        text_extensions = {".txt", ".json", ".xml", ".csv", ".md", ".html", ".htm"}
        file_ext = os.path.splitext(job.source_ref)[1].lower()
        if isinstance(content, bytes) and file_ext in text_extensions:
            content = content.decode("utf-8")

        # Validate content is not empty
        if isinstance(content, str) and not content.strip():
            raise ValueError(f"Empty content for source_ref '{job.source_ref}'")
        if isinstance(content, bytes) and len(content) == 0:
            raise ValueError(f"Empty content for source_ref '{job.source_ref}'")

        # Build DocGrok embed payload
        dgp = pipeline.docgrok_pipeline
        if dgp.startswith("mdl-"):
            payload = {"model_id": dgp, "requestId": job.id}
        else:
            payload = {"pipeline": dgp, "requestId": job.id}
        if isinstance(content, bytes):
            payload["data"] = base64.b64encode(content).decode()
        else:
            payload["text"] = content

        # Call DocGrok with retry on transient errors (429, 500, 502, 503)
        EMBED_MAX_RETRIES = 5
        EMBED_BASE_DELAY = 1.0
        resp = None
        for embed_attempt in range(1, EMBED_MAX_RETRIES + 1):
            resp = await client.post(
                f"{DOCGROK_URL}/embed",
                json=payload,
                timeout=120.0,
            )
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503) and embed_attempt < EMBED_MAX_RETRIES:
                delay = EMBED_BASE_DELAY * (2 ** (embed_attempt - 1))
                if resp.status_code == 429:
                    retry_after = resp.headers.get("retry-after")
                    if retry_after:
                        delay = max(delay, float(retry_after))
                logger.warning("DocGrok %d on attempt %d/%d, retry in %.1fs",
                    resp.status_code, embed_attempt, EMBED_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
            else:
                raise ValueError(f"DocGrok error: {resp.text}")

        result = resp.json()

        # Check content strategy
        content_strategy = getattr(pipeline, 'content_strategy', 'truncate') or 'truncate'

        if content_strategy == 'chunk' and source.type != SourceType.COSMOSDB:
            # ── Chunked processing: split text → embed each chunk → write chunk docs ──
            from chunker import chunk_text, make_chunk_doc_id, make_chunk_prefix
            from connectors.cosmosdb_vector_connector import write_vector_chunks, delete_chunks_by_prefix

            chunk_cfg = pipeline.chunk_config
            if chunk_cfg and hasattr(chunk_cfg, 'model_dump'):
                chunk_cfg = chunk_cfg.model_dump()
            elif not chunk_cfg:
                chunk_cfg = {}
            chunk_size = chunk_cfg.get('chunk_size', 1000)
            chunk_overlap = chunk_cfg.get('chunk_overlap', 200)
            chunk_unit = chunk_cfg.get('chunk_unit', 'chars')
            store_text = chunk_cfg.get('store_text', False)
            text_field = chunk_cfg.get('text_field', 'text')
            doc_id_pattern = chunk_cfg.get('doc_id_pattern', '')

            # Get full text from DocGrok result or original content
            full_text = result.get("text", "")
            if not full_text and isinstance(content, str):
                full_text = content

            # Split into chunks
            chunks = chunk_text(full_text, chunk_size, chunk_overlap, chunk_unit)
            if not chunks:
                raise ValueError(f"No chunks produced for source_ref '{job.source_ref}'")

            # Delete old chunks for this source (handles re-processing)
            prefix = make_chunk_prefix(pipeline.id, job.source_ref, doc_id_pattern)
            await delete_chunks_by_prefix(destination.config, prefix)

            # Embed all chunks via DocGrok /embed/batch
            chunk_texts = [c[0] for c in chunks]
            dgp = pipeline.docgrok_pipeline
            if dgp.startswith("mdl-"):
                batch_payload = {"model_id": dgp, "texts": chunk_texts}
            else:
                batch_payload = {"pipeline": dgp, "texts": chunk_texts}

            batch_resp = await client.post(f"{DOCGROK_URL}/embed/batch", json=batch_payload)
            if batch_resp.status_code != 200:
                raise ValueError(f"DocGrok batch embed error for chunks: {batch_resp.text}")

            batch_result = batch_resp.json()
            chunk_embeddings = batch_result.get("outputs", [])
            if len(chunk_embeddings) != len(chunks):
                raise ValueError(f"Chunk batch mismatch: {len(chunks)} chunks, {len(chunk_embeddings)} embeddings")

            # Build chunk documents
            chunk_docs = []
            for i, (chunk_text_content, chunk_idx) in enumerate(chunks):
                doc_id = make_chunk_doc_id(pipeline.id, job.source_ref, chunk_idx, doc_id_pattern)
                chunk_doc = {
                    "id": doc_id,
                    "embedding": chunk_embeddings[i],
                    "source": source.name,
                    "source_id": source.id,
                    "source_ref": job.source_ref,
                    "pipeline": pipeline.name,
                    "pipeline_id": pipeline.id,
                    "chunk_index": chunk_idx,
                    "total_chunks": len(chunks),
                    **{k: v for k, v in job.metadata.items() if k not in ("content",)},
                }
                if source.type == SourceType.AZURE_BLOB:
                    account_url = source.config.get("account_url", "").rstrip("/")
                    container_name = source.config.get("container", "")
                    chunk_doc["blobUrl"] = f"{account_url}/{container_name}/{job.source_ref}"
                if store_text:
                    chunk_doc[text_field] = chunk_text_content
                chunk_docs.append(chunk_doc)

            await write_vector_chunks(destination.config, chunk_docs)

            # Mark COMPLETED
            job.status = JobStatus.COMPLETED
            # Determine embedding dimensions from first chunk's embedding
            _first_emb = chunk_embeddings[0] if chunk_embeddings else []
            if _first_emb and isinstance(_first_emb[0], list):
                _emb_dims = len(_first_emb[0])
            elif _first_emb:
                _emb_dims = len(_first_emb)
            else:
                _emb_dims = 0
            job.result = {
                "chunks_created": len(chunks),
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "embedding_dims": _emb_dims,
                "content_strategy": "chunk",
            }
            job.completed_at = datetime.utcnow()
            store.upsert(_to_doc(job, "job"))
            logger.info("Job %s completed with %d chunks", job.id, len(chunks))

        else:
            # ── Truncate (default): single vector per document ──
            # Write vector to destination
            if source.type == SourceType.COSMOSDB:
                from connectors.cosmosdb_vector_connector import patch_vector_inplace
                await patch_vector_inplace(
                    destination.config,
                    doc_id=job.source_ref,
                    embedding=result.get("output", []),
                    attrs={"pipeline_id": pipeline.id, "pipeline_name": pipeline.name, "content_hash": content_hash, "pipeline_generation": pipeline.generation},
                )
            else:
                from connectors.cosmosdb_vector_connector import write_vector

                vector_metadata = {
                    "source": source.name,
                    "source_id": source.id,
                    "source_ref": job.source_ref,
                    "pipeline": pipeline.name,
                    **job.metadata,
                }

                if source.type == SourceType.AZURE_BLOB:
                    account_url = source.config.get("account_url", "").rstrip("/")
                    container_name = source.config.get("container", "")
                    vector_metadata["blobUrl"] = f"{account_url}/{container_name}/{job.source_ref}"
                else:
                    vector_metadata["text"] = content if isinstance(content, str) else None

                # Resolve doc ID from pattern or default to job.id
                pid_pattern = getattr(pipeline, 'doc_id_pattern', '') or ''
                if pid_pattern:
                    src_base = os.path.splitext(os.path.basename(job.source_ref))[0]
                    vid = pid_pattern.format(
                        source=src_base,
                        source_ref=job.source_ref.replace("/", "-").replace("\\", "-"),
                        source_hash=hashlib.sha256(job.source_ref.encode()).hexdigest()[:12],
                        pipeline=pipeline.id,
                        job=job.id,
                    )
                else:
                    vid = job.id

                await write_vector(
                    destination.config,
                    doc_id=vid,
                    embedding=result.get("output", []),
                    metadata=vector_metadata,
                )

            # Mark COMPLETED
            job.status = JobStatus.COMPLETED
            job.result = {
                "embedding_dims": len(result.get("output", [[]])[0]) if result.get("output") else 0
            }
            job.completed_at = datetime.utcnow()
            store.upsert(_to_doc(job, "job"))
            logger.info("Job %s completed", job.id)

        try:
            proc_ms = (job.completed_at - job.started_at).total_seconds() * 1000
            update_metrics(job.pipeline_id, JobStatus.COMPLETED, proc_ms)
        except Exception as me:
            logger.warning("Failed to update metrics for job %s: %s", job.id, me)

    except SkipDocument as skip:
        job.status = JobStatus.COMPLETED
        job.result = {"skipped": True, "reason": str(skip), "embedded": False}
        job.completed_at = datetime.utcnow()
        store.upsert(_to_doc(job, "job"))
        logger.warning("Job %s SKIPPED (not embedded): %s — document %s will NOT have an embedding",
            job.id, skip, job.source_ref)

    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)[:2000]
        job.completed_at = datetime.utcnow()
        try:
            store.upsert(_to_doc(job, "job"))
        except Exception as ue:
            logger.critical("Job %s STUCK in PROCESSING — FAILED status update failed: %s (original error: %s)",
                job.id, ue, str(e)[:200])
        logger.error("Job %s failed: %s", job.id, e)

        try:
            proc_ms = (job.completed_at - (job.started_at or job.completed_at)).total_seconds() * 1000
            update_metrics(job.pipeline_id, JobStatus.FAILED, proc_ms)
        except Exception as me:
            logger.warning("Failed to update metrics for job %s: %s", job.id, me)


# ── sync wrappers for parallel writes via asyncio.to_thread ──────────

# Cache for partition key lookups — bounded LRU to prevent OOM
from collections import OrderedDict

_PK_CACHE_MAX = 10000

class _LRUCache(OrderedDict):
    def __init__(self, maxsize):
        super().__init__()
        self.maxsize = maxsize
    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return default
    def put(self, key, value):
        if key in self:
            self.move_to_end(key)
        self[key] = value
        if len(self) > self.maxsize:
            self.popitem(last=False)

_pk_cache = _LRUCache(_PK_CACHE_MAX)


def _sync_patch_vector(dest_config, doc_id, embedding, pipeline_id, pipeline_name, content_hash, pk_hint=None, pipeline_generation=None):
    """Sync wrapper for patch_vector_inplace (runs in thread pool).

    pk_hint: if provided, use as partition key value (avoids cross-partition query).
    Retries on 429 with exponential backoff - never fails due to rate limits.
    """
    import time
    from connectors.cosmosdb_vector_connector import _get_credential
    from datetime import datetime as dt
    from azure.cosmos import CosmosClient
    from azure.cosmos.exceptions import CosmosHttpResponseError

    MAX_RETRIES = 20
    BASE_DELAY_S = 0.1

    endpoint = dest_config["endpoint"]
    from connectors.cosmosdb_vector_connector import _client_cache
    if endpoint not in _client_cache:
        _client_cache[endpoint] = CosmosClient(endpoint, credential=_get_credential())
    client = _client_cache[endpoint]

    database = client.get_database_client(dest_config["database"])
    container = database.get_container_client(dest_config["container"])
    vector_field = dest_config.get("vector_field", "embedding")

    # Get partition key path - query container if not in config
    pk_path = dest_config.get("partition_key_path")
    if not pk_path:
        try:
            props = container.read()
            pk_paths = props.get("partitionKey", {}).get("paths", [])
            pk_path = pk_paths[0] if pk_paths else "/id"
        except Exception:
            pk_path = "/id"  # Safe default - most containers use /id
    pk_field = pk_path.lstrip("/")

    flat = embedding[0] if embedding and isinstance(embedding[0], list) else embedding

    if pk_field == "id":
        pk_value = doc_id
    elif pk_hint:
        pk_value = pk_hint
    else:
        cache_key = f"{endpoint}:{dest_config['database']}:{dest_config['container']}:{doc_id}"
        pk_value = _pk_cache.get(cache_key)
        if pk_value is None:
            rows = list(container.query_items(
                f"SELECT c.id, c.{pk_field} FROM c WHERE c.id = @id",
                parameters=[{"name": "@id", "value": doc_id}],
                enable_cross_partition_query=True,
            ))
            if not rows:
                raise ValueError(f"Document '{doc_id}' not found for in-place patch")
            pk_value = rows[0].get(pk_field)
            _pk_cache.put(cache_key, pk_value)

    ops = [
        {"op": "set", "path": f"/{vector_field}", "value": flat},
        {"op": "set", "path": "/embedded_at", "value": dt.utcnow().isoformat()},
        {"op": "set", "path": "/embedding_dims", "value": len(flat)},
        {"op": "set", "path": "/pipeline_id", "value": pipeline_id},
        {"op": "set", "path": "/pipeline_name", "value": pipeline_name},
    ]
    if content_hash:
        ops.append({"op": "set", "path": "/content_hash", "value": content_hash})
    if pipeline_generation:
        ops.append({"op": "set", "path": "/pipeline_generation", "value": pipeline_generation})

    # Retry on 429/5xx with exponential backoff
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            container.patch_item(item=doc_id, partition_key=pk_value, patch_operations=ops)
            return
        except CosmosHttpResponseError as e:
            last_error = e
            if e.status_code in (429, 408, 503) or e.status_code >= 500:
                retry_after = float(e.headers.get("x-ms-retry-after-ms", BASE_DELAY_S * 1000 * (2 ** attempt))) / 1000.0
                retry_after = min(retry_after, 60.0)
                logger.warning("Patch %d on attempt %d/%d for doc %s, retry in %.1fs",
                    e.status_code, attempt, MAX_RETRIES, doc_id, retry_after)
                time.sleep(retry_after)
            else:
                raise
        except Exception as e:
            last_error = e
            if attempt >= MAX_RETRIES:
                raise
            logger.warning("Patch error on attempt %d/%d for doc %s: %s", attempt, MAX_RETRIES, doc_id, e)
            time.sleep(BASE_DELAY_S * (2 ** attempt))
    # All retries exhausted
    raise RuntimeError(f"Patch failed after {MAX_RETRIES} attempts for doc {doc_id}: {last_error}")


def _sync_write_vector(dest_config, doc_id, embedding, metadata):
    """Sync wrapper for write_vector (runs in thread pool).

    Retries on 429 with exponential backoff - never fails due to rate limits.
    """
    import time
    from connectors.cosmosdb_vector_connector import _client_cache, _get_credential
    from azure.cosmos import CosmosClient
    from azure.cosmos.exceptions import CosmosHttpResponseError

    MAX_RETRIES = 20
    BASE_DELAY_S = 0.1

    endpoint = dest_config["endpoint"]
    if endpoint not in _client_cache:
        _client_cache[endpoint] = CosmosClient(endpoint, credential=_get_credential())
    client = _client_cache[endpoint]

    database = client.get_database_client(dest_config["database"])
    container = database.get_container_client(dest_config["container"])
    vector_field = dest_config.get("vector_field", "embedding")

    flat = embedding[0] if embedding and isinstance(embedding[0], list) else embedding
    doc = {"id": doc_id, vector_field: flat, "embedding_dims": len(flat) if flat else 0, **metadata}

    # Retry on 429/5xx with exponential backoff
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            container.upsert_item(doc)
            return
        except CosmosHttpResponseError as e:
            last_error = e
            if e.status_code in (429, 408, 503) or e.status_code >= 500:
                retry_after = float(e.headers.get("x-ms-retry-after-ms", BASE_DELAY_S * 1000 * (2 ** attempt))) / 1000.0
                retry_after = min(retry_after, 60.0)
                logger.warning("Upsert %d on attempt %d/%d for doc %s, retry in %.1fs",
                    e.status_code, attempt, MAX_RETRIES, doc_id, retry_after)
                time.sleep(retry_after)
            else:
                raise
        except Exception as e:
            last_error = e
            if attempt >= MAX_RETRIES:
                raise
            logger.warning("Upsert error on attempt %d/%d for doc %s: %s", attempt, MAX_RETRIES, doc_id, e)
            time.sleep(BASE_DELAY_S * (2 ** attempt))
    raise RuntimeError(f"Upsert failed after {MAX_RETRIES} attempts for doc {doc_id}: {last_error}")


async def _async_write_pgvector(dest_config, doc_id, embedding, metadata):
    """Write vector to pgvector destination."""
    from connectors.postgres_connector import upsert_vectors

    flat = embedding[0] if embedding and isinstance(embedding[0], list) else embedding
    doc = {
        "id": doc_id,
        "embedding": flat,
        "content": metadata.get("content", ""),
        "metadata": metadata,
    }
    await upsert_vectors(dest_config, [doc])


# ── batch processing function ─────────────────────────────────────────

async def process_jobs_batch(jobs: List[Job]) -> None:
    """Process a batch of jobs sharing the same pipeline.

    Downloads content for all jobs, sends a single batch embed request to
    DocGrok /embed/batch, then writes vectors for each job individually.
    Falls back to individual process_job() for binary content or errors.
    """
    if not jobs:
        return

    store = get_store()
    client = get_http_client()
    batch_start = datetime.utcnow()

    # All jobs must share the same pipeline
    pipeline_id = jobs[0].pipeline_id
    pipeline_doc = store.get(pipeline_id, "pipeline")
    if not pipeline_doc:
        logger.error("Pipeline '%s' not found, falling back to individual", pipeline_id)
        for job in jobs:
            await process_job(job)
        return

    pipeline = _pipeline_from_doc(pipeline_doc)

    # If pipeline uses chunking, fall back to individual processing
    # (each job needs its own chunk + embed cycle)
    content_strategy = getattr(pipeline, 'content_strategy', 'truncate') or 'truncate'
    if content_strategy == 'chunk':
        logger.info("Pipeline uses chunk strategy, processing %d jobs individually", len(jobs))
        for job in jobs:
            await process_job(job)
        return

    dest_doc = store.get(pipeline.destination_id, "destination")
    if not dest_doc:
        logger.error("Destination '%s' not found, falling back to individual", pipeline.destination_id)
        for job in jobs:
            await process_job(job)
        return
    destination = _destination_from_doc(dest_doc)

    # Phase 1: Download content for each job
    text_jobs: list[Job] = []            # jobs with text content (batchable)
    text_contents: list[str] = []        # text content in same order as text_jobs
    text_hashes: list[str | None] = []   # content hash for CosmosDB sources
    binary_jobs: list[Job] = []          # jobs with binary content (individual processing)

    for job in jobs:
        try:
            # Check if content was pre-fetched by the connector (e.g. .NET CFP)
            prefetched_content = job.metadata.get("content")
            prefetched_hash = job.metadata.get("content_hash")

            if prefetched_content:
                content = prefetched_content
                content_hash = prefetched_hash
            else:
                source_doc = store.get(job.source_id, "source")
                if not source_doc:
                    raise ValueError(f"Source '{job.source_id}' not found")
                source = _source_from_doc(source_doc)

                if source.type == SourceType.AZURE_BLOB:
                    from connectors.blob_connector import download_blob
                    content = await download_blob(source.config, job.source_ref)
                    content_hash = None

                    # Decode text files
                    text_extensions = {".txt", ".json", ".xml", ".csv", ".md", ".html", ".htm"}
                    file_ext = os.path.splitext(job.source_ref)[1].lower()
                    if isinstance(content, bytes) and file_ext in text_extensions:
                        content = content.decode("utf-8")

                elif source.type == SourceType.COSMOSDB:
                    from connectors.cosmosdb_connector import get_document
                    content, content_hash = await get_document(source.config, job.source_ref)

                elif source.type == SourceType.POSTGRESQL:
                    # PostgreSQL content is already in the job payload
                    payload_data = job.metadata.get("payload", {}) if hasattr(job, 'metadata') else {}
                    if not payload_data and hasattr(job, 'payload'):
                        payload_data = job.payload or {}
                    content = payload_data.get("content", "")
                    content_hash = None
                    if not content:
                        from connectors.postgres_connector import get_row_by_id
                        row = await get_row_by_id(source.config, job.source_ref)
                        content = row.get("_content", "") if row else ""
                else:
                    raise ValueError(f"Unsupported source type: {source.type}")

            # Validate content
            if isinstance(content, str) and not content.strip():
                raise ValueError(f"Empty content for source_ref '{job.source_ref}'")
            if isinstance(content, bytes) and len(content) == 0:
                raise ValueError(f"Empty content for source_ref '{job.source_ref}'")

            if isinstance(content, str):
                text_jobs.append(job)
                text_contents.append(content)
                text_hashes.append(content_hash)
            else:
                binary_jobs.append(job)

        except SkipDocument as skip:
            job.status = JobStatus.COMPLETED
            job.result = {"skipped": True, "reason": str(skip)}
            job.completed_at = datetime.utcnow()
            store.upsert(_to_doc(job, "job"))
            logger.info("Job %s skipped: %s", job.id, skip)

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.completed_at = datetime.utcnow()
            store.upsert(_to_doc(job, "job"))
            logger.error("Job %s failed during download: %s", job.id, e)
            try:
                update_metrics(pipeline_id, JobStatus.FAILED, 0)
            except Exception as me:
                logger.warning("Metrics update failed for batch job: %s", me)

    # Phase 2: Batch embed text jobs
    if text_jobs:
        try:
            dgp = pipeline.docgrok_pipeline
            if dgp.startswith("mdl-"):
                batch_payload = {"model_id": dgp, "texts": text_contents}
            else:
                batch_payload = {"pipeline": dgp, "texts": text_contents}
            resp = await client.post(
                f"{DOCGROK_URL}/embed/batch",
                json=batch_payload,
            )
            if resp.status_code != 200:
                raise ValueError(f"DocGrok batch error: {resp.text}")

            result = resp.json()
            outputs = result.get("outputs", [])

            if len(outputs) != len(text_jobs):
                raise ValueError(
                    f"Batch size mismatch: sent {len(text_jobs)} texts, got {len(outputs)} outputs"
                )

            logger.info(
                "Batch embed: %d texts via pipeline=%s in one call",
                len(text_jobs), pipeline.docgrok_pipeline,
            )

            # Phase 3: Write vectors in PARALLEL (CosmosDB SDK is sync → use threads)
            # Pre-populate source cache (avoid sync calls during parallel phase)
            _source_cache = {}
            for job in text_jobs:
                if job.source_id not in _source_cache:
                    source_doc = store.get(job.source_id, "source")
                    if source_doc:
                        _source_cache[job.source_id] = _source_from_doc(source_doc)

            async def _write_vector(i, job, embedding):
                """Write vector only, return (index, success, error)."""
                try:
                    source = _source_cache.get(job.source_id)
                    if not source:
                        raise ValueError(f"Source '{job.source_id}' not found")

                    vector_metadata = {
                        "source": source.name,
                        "source_id": source.id,
                        "source_ref": job.source_ref,
                        "pipeline": pipeline.name,
                        "pipeline_id": pipeline.id,
                        "pipeline_name": pipeline.name,
                        "embedded_at": datetime.utcnow().isoformat(),
                        "content_hash": text_hashes[i] if i < len(text_hashes) else "",
                    }

                    # Check destination type to determine how to write vectors
                    if destination.type == DestinationType.PGVECTOR:
                        # Write to pgvector - include content text
                        vector_metadata["content"] = text_contents[i]
                        await _async_write_pgvector(
                            destination.config,
                            job.id, embedding, vector_metadata,
                        )
                    elif source.type == SourceType.COSMOSDB and destination.type == DestinationType.COSMOSDB_VECTOR:
                        # Check if source and destination are the same container (patch in-place)
                        same_container = (
                            source.config.get("endpoint") == destination.config.get("endpoint")
                            and source.config.get("database") == destination.config.get("database")
                            and source.config.get("container") == destination.config.get("container")
                        )
                        if same_container:
                            # Patch in-place for same container
                            pk_hint = job.metadata.get("_pk_value") or None
                            await asyncio.to_thread(
                                _sync_patch_vector, destination.config,
                                job.source_ref, embedding, pipeline.id,
                                pipeline.name, text_hashes[i], pk_hint,
                                pipeline.generation,
                            )
                        else:
                            # Upsert to separate destination container
                            await asyncio.to_thread(
                                _sync_write_vector, destination.config,
                                job.source_ref, embedding, vector_metadata,
                            )
                    else:
                        # Write to CosmosDB vector destination
                        if source.type == SourceType.AZURE_BLOB:
                            account_url = source.config.get("account_url", "").rstrip("/")
                            container_name = source.config.get("container", "")
                            vector_metadata["blobUrl"] = f"{account_url}/{container_name}/{job.source_ref}"
                        await asyncio.to_thread(
                            _sync_write_vector, destination.config,
                            job.id, embedding, vector_metadata,
                        )

                    job.status = JobStatus.COMPLETED
                    job.result = {
                        "embedding_dims": len(embedding[0]) if embedding and isinstance(embedding[0], list) else len(embedding) if embedding else 0,
                        "batch": True,
                    }
                    job.completed_at = datetime.utcnow()
                    return (i, True, None)

                except Exception as e:
                    job.status = JobStatus.FAILED
                    job.error = str(e)
                    job.completed_at = datetime.utcnow()
                    logger.error("Job %s failed during vector write: %s", job.id, e)
                    return (i, False, str(e))

            # Fire all vector writes in parallel
            write_results = await asyncio.gather(*[
                _write_vector(i, job, outputs[i])
                for i, job in enumerate(text_jobs)
            ])

            # Phase 4: Batch update job statuses in PARALLEL threads
            await asyncio.gather(*[
                asyncio.to_thread(store.upsert, _to_doc(text_jobs[i], "job"))
                for i, _, _ in write_results
            ])

            # Phase 5: Single batch metrics update (instead of per-job)
            batch_completed = sum(1 for _, ok, _ in write_results if ok)
            batch_failed = sum(1 for _, ok, _ in write_results if not ok)
            batch_ms = sum(
                (text_jobs[i].completed_at - text_jobs[i].started_at).total_seconds() * 1000
                for i, ok, _ in write_results
                if ok and text_jobs[i].started_at
            )
            if batch_completed > 0 or batch_failed > 0:
                try:
                    await asyncio.to_thread(
                        update_metrics_batch, pipeline_id,
                        batch_completed, batch_failed, batch_ms,
                    )
                except Exception as me:
                    logger.warning("Failed to update batch metrics: %s", me)

        except Exception as e:
            # Batch embed failed — fall back to individual processing
            logger.warning("Batch embed failed (%s), falling back to individual for %d jobs", e, len(text_jobs))
            for job in text_jobs:
                await process_job(job)

    # Phase 4: Process binary jobs individually (can't batch binary/PDF content)
    if binary_jobs:
        logger.info("Processing %d binary jobs individually", len(binary_jobs))
        await asyncio.gather(*[process_job(job) for job in binary_jobs])

    elapsed = (datetime.utcnow() - batch_start).total_seconds()
    logger.info(
        "Batch complete: %d text + %d binary jobs in %.1fs",
        len(text_jobs), len(binary_jobs), elapsed,
    )
