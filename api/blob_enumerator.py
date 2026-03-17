#!/usr/bin/env python3
"""OmniVec Blob Enumerator

Per-source process that enumerates blobs and creates PENDING jobs.
Uses checkpointing for resumable enumeration across restarts.

Environment Variables:
    SOURCE_ID: Required - The source ID to enumerate
    COSMOS_ENDPOINT: Required - CosmosDB endpoint
    ENUMERATION_INTERVAL: Optional - Seconds between enumerations (default: 60)
    PAGE_SIZE: Optional - Blobs per page (default: 1000)
"""

import os
import uuid
import asyncio
import logging
from datetime import datetime
from typing import Optional

from store import init_store, get_store
from models import Source, Pipeline, Job, PipelineStatus, SourceType
from connectors.blob_connector import list_blobs_paginated

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [blob-enumerator] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy Azure SDK logging
for _sdk_logger in ("azure.core.pipeline.policies.http_logging_policy",
                     "azure.identity", "azure.core", "urllib3"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

# Configuration
SOURCE_ID = os.environ.get("SOURCE_ID")
ENUMERATION_INTERVAL = int(os.environ.get("ENUMERATION_INTERVAL", "60"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1000"))


def _strip_doc(doc: dict) -> dict:
    """Remove CosmosDB internal fields."""
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


def _source_from_doc(doc: dict) -> Source:
    return Source(**_strip_doc(doc))


def _pipeline_from_doc(doc: dict) -> Pipeline:
    return Pipeline(**_strip_doc(doc))


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


# ── Checkpoint Management ─────────────────────────────────────────────────


def get_checkpoint_id(source_id: str) -> str:
    """Generate checkpoint document ID for a source."""
    return f"blob-enum-{source_id}"


def load_checkpoint(source_id: str) -> Optional[dict]:
    """Load checkpoint from CosmosDB."""
    store = get_store()
    checkpoint_id = get_checkpoint_id(source_id)
    return store.get(checkpoint_id, "checkpoint")


def save_checkpoint(
    source_id: str,
    continuation_token: Optional[str],
    last_blob: Optional[str],
    blobs_seen: int,
    jobs_created: int,
    status: str = "in_progress"
) -> None:
    """Save checkpoint to CosmosDB."""
    store = get_store()
    checkpoint_id = get_checkpoint_id(source_id)

    existing = store.get(checkpoint_id, "checkpoint") or {}

    checkpoint = {
        "id": checkpoint_id,
        "doc_type": "checkpoint",
        "checkpoint_type": "blob_enumerator",
        "source_id": source_id,
        "continuation_token": continuation_token,
        "last_blob_processed": last_blob,
        "total_blobs_seen": existing.get("total_blobs_seen", 0) + blobs_seen,
        "total_jobs_created": existing.get("total_jobs_created", 0) + jobs_created,
        "last_enumeration_at": datetime.utcnow().isoformat(),
        "status": status,
    }

    store.upsert(checkpoint)
    logger.debug("Checkpoint saved: token=%s, blobs=%d, jobs=%d",
                 bool(continuation_token), blobs_seen, jobs_created)


def clear_checkpoint(source_id: str) -> None:
    """Clear checkpoint for full re-enumeration."""
    store = get_store()
    checkpoint_id = get_checkpoint_id(source_id)
    try:
        store.delete(checkpoint_id, "checkpoint")
        logger.info("Checkpoint cleared for source %s", source_id)
    except Exception:
        pass


# ── Source and Pipeline Helpers ───────────────────────────────────────────


def get_source(source_id: str) -> Optional[Source]:
    """Get source by ID."""
    store = get_store()
    doc = store.get(source_id, "source")
    return _source_from_doc(doc) if doc else None


def get_active_pipelines_for_source(source_id: str) -> list[Pipeline]:
    """Get active pipelines that use this source."""
    store = get_store()
    pipelines = []
    for doc in store.list("pipeline"):
        if doc.get("status") != PipelineStatus.ACTIVE.value:
            continue
        pipeline = _pipeline_from_doc(doc)
        if any(ps.source_id == source_id for ps in pipeline.sources):
            pipelines.append(pipeline)
    return pipelines


def get_existing_job_refs(pipeline_id: str, source_id: str) -> set[str]:
    """Return set of source_refs that already have a job."""
    store = get_store()
    query = (
        "SELECT c.source_ref FROM c "
        "WHERE c.doc_type = 'job' AND c.pipeline_id = @pip AND c.source_id = @src"
    )
    params = [
        {"name": "@pip", "value": pipeline_id},
        {"name": "@src", "value": source_id},
    ]
    rows = store.query(query, params, partition_key="job")
    return {r["source_ref"] for r in rows}


# ── Enumeration Logic ─────────────────────────────────────────────────────


async def enumerate_source_with_checkpoint(source: Source) -> dict:
    """Enumerate blobs with pagination and checkpointing.

    Returns:
        dict with enumeration statistics
    """
    store = get_store()
    source_id = source.id

    # Get active pipelines for this source
    pipelines = get_active_pipelines_for_source(source_id)
    if not pipelines:
        logger.debug("No active pipelines for source %s", source_id)
        return {"blobs_seen": 0, "jobs_created": 0, "status": "no_pipelines"}

    # Load checkpoint
    checkpoint = load_checkpoint(source_id)
    continuation_token = checkpoint.get("continuation_token") if checkpoint else None

    if continuation_token:
        logger.info("Resuming enumeration from checkpoint for source %s", source_id)
    else:
        logger.info("Starting fresh enumeration for source %s", source_id)

    total_blobs_seen = 0
    total_jobs_created = 0
    last_blob = None

    # Build existing refs cache per pipeline
    existing_refs_cache = {}
    for pipeline in pipelines:
        existing_refs_cache[pipeline.id] = get_existing_job_refs(pipeline.id, source_id)

    # Paginated enumeration
    while True:
        try:
            blobs, next_token = await list_blobs_paginated(
                source.config,
                page_size=PAGE_SIZE,
                continuation_token=continuation_token
            )
        except Exception as e:
            logger.error("Blob listing failed for source %s: %s", source_id, e)
            save_checkpoint(source_id, continuation_token, last_blob,
                            total_blobs_seen, total_jobs_created, status="error")
            raise

        page_blobs = len(blobs)
        page_jobs = 0

        for blob_doc in blobs:
            ref = blob_doc["ref"]
            last_blob = ref

            # Create job for each pipeline that doesn't have one
            for pipeline in pipelines:
                if ref in existing_refs_cache[pipeline.id]:
                    continue

                job = Job(
                    id=f"job-{str(uuid.uuid4())[:12]}",
                    pipeline_id=pipeline.id,
                    source_id=source_id,
                    source_ref=ref,
                    metadata={"trigger": "blob_enumerator", **blob_doc.get("metadata", {})},
                    created_at=datetime.utcnow(),
                )
                store.upsert(_to_doc(job, "job"))
                existing_refs_cache[pipeline.id].add(ref)
                page_jobs += 1

        total_blobs_seen += page_blobs
        total_jobs_created += page_jobs

        # Save checkpoint after each page
        save_checkpoint(
            source_id,
            next_token,
            last_blob,
            page_blobs,
            page_jobs,
            status="in_progress" if next_token else "completed"
        )

        if page_jobs > 0:
            logger.info(
                "Source %s: page processed - %d blobs, %d new jobs created",
                source_id, page_blobs, page_jobs
            )

        # Exit if no more pages
        if not next_token:
            break

        continuation_token = next_token

    logger.info(
        "Enumeration complete for source %s: %d blobs seen, %d jobs created",
        source_id, total_blobs_seen, total_jobs_created
    )

    return {
        "blobs_seen": total_blobs_seen,
        "jobs_created": total_jobs_created,
        "status": "completed"
    }


# ── Main Loop ─────────────────────────────────────────────────────────────


async def main():
    if not SOURCE_ID:
        logger.error("SOURCE_ID environment variable is required")
        return

    logger.info("Blob Enumerator starting for source %s", SOURCE_ID)
    init_store()
    logger.info("CosmosDB store initialized")

    while True:
        try:
            source = get_source(SOURCE_ID)
            if not source:
                logger.error("Source %s not found", SOURCE_ID)
                await asyncio.sleep(ENUMERATION_INTERVAL)
                continue

            if not source.enabled:
                logger.info("Source %s is disabled, skipping", SOURCE_ID)
                await asyncio.sleep(ENUMERATION_INTERVAL)
                continue

            if source.type != SourceType.AZURE_BLOB:
                logger.error("Source %s is not a blob source (type=%s)", SOURCE_ID, source.type)
                await asyncio.sleep(ENUMERATION_INTERVAL)
                continue

            result = await enumerate_source_with_checkpoint(source)
            logger.info("Enumeration result: %s", result)

        except Exception as e:
            logger.error("Enumeration error for source %s: %s", SOURCE_ID, e)

        await asyncio.sleep(ENUMERATION_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
