#!/usr/bin/env python3
"""Blob Backfill Worker - Per-source historical data processor.

Processes existing blobs in a storage container:
1. Enumerates blobs with pagination and checkpointing
2. Creates jobs for new blobs
3. Processes jobs (embed + write to destination)

Crash-resilient:
- Checkpoint saved every N items
- Jobs claimed via etag (no duplicates)
- Resume from checkpoint on restart
"""

import os
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import httpx
from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from models import Source, SourceType, Pipeline, Job, JobStatus
from store import init_store, get_store
from checkpoint_manager import CheckpointManager
from progress_tracker import ProgressTracker, SourceStatus
from job_processor import process_jobs_batch, set_http_client
from connectors.blob_connector import list_blobs_paginated

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [blob-backfill] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for _logger in ("azure.core", "azure.identity", "urllib3"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

# Configuration
SOURCE_ID = os.environ.get("SOURCE_ID")
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "100"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1000"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
ENUMERATION_INTERVAL = int(os.environ.get("ENUMERATION_INTERVAL", "300"))


def _strip_doc(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


def _job_id(source_id: str, pipeline_id: str, blob_path: str) -> str:
    """Generate deterministic job ID from source, pipeline, and blob path.

    CRITICAL: Must include pipeline_id to avoid collisions when same source
    is used by multiple pipelines.
    """
    return f"job-{hashlib.sha256(f'{source_id}:{pipeline_id}:{blob_path}'.encode()).hexdigest()[:16]}"


class BlobBackfillWorker:
    """Worker for processing historical blob data."""

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.store = None
        self.source: Optional[Source] = None
        self.pipelines: List[Pipeline] = []
        self.checkpoint_manager: Optional[CheckpointManager] = None
        self.progress_tracker: Optional[ProgressTracker] = None
        self._running = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._tasks: List[asyncio.Task] = []  # Track running tasks for graceful shutdown

    async def start(self):
        """Start the worker."""
        logger.info("Blob Backfill Worker starting for source %s", self.source_id)

        init_store()
        self.store = get_store()

        # Load source
        self.source = await self._load_source()
        if not self.source:
            logger.error("Source %s not found", self.source_id)
            return

        # Load pipelines
        self.pipelines = await self._load_pipelines()
        if not self.pipelines:
            logger.warning("No active pipelines for source %s", self.source_id)

        # Initialize managers
        self.checkpoint_manager = CheckpointManager(
            self.source_id, "backfill", self.source.config.get("container", "")
        )
        self.progress_tracker = ProgressTracker(self.source_id)

        # Initialize HTTP client
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=30),
        )
        set_http_client(self._http_client)

        self._running = True

        # Run main loops with task tracking for graceful shutdown
        try:
            self._tasks = [
                asyncio.create_task(self._enumeration_loop(), name="enumeration"),
                asyncio.create_task(self._job_processing_loop(), name="job_processing"),
                asyncio.create_task(self._stuck_job_recovery_loop(), name="stuck_recovery"),
            ]
            await asyncio.gather(*self._tasks)
        finally:
            await self._http_client.aclose()

    async def stop(self):
        """Stop the worker gracefully."""
        logger.info("Blob Backfill Worker stopping")
        self._running = False

        # Cancel all running tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to complete cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def _load_source(self) -> Optional[Source]:
        """Load source from CosmosDB."""
        try:
            doc = self.store.get(self.source_id, partition_key="source")
            return Source(**_strip_doc(doc))
        except Exception as e:
            logger.error("Failed to load source: %s", e)
            return None

    async def _load_pipelines(self) -> List[Pipeline]:
        """Load active pipelines that use this source."""
        query = (
            "SELECT * FROM c WHERE c.doc_type = 'pipeline' "
            "AND c.is_active = true "
            "AND ARRAY_CONTAINS(c.source_ids, @source_id)"
        )
        params = [{"name": "@source_id", "value": self.source_id}]
        docs = self.store.query(query, params, partition_key="pipeline")
        return [Pipeline(**_strip_doc(doc)) for doc in docs]

    async def _enumeration_loop(self):
        """Enumerate blobs and create jobs."""
        while self._running:
            try:
                await self._enumerate_blobs()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Enumeration error: %s", e)
                self.progress_tracker.set_status(
                    SourceStatus.ERROR, f"Enumeration failed: {e}"
                )

            # Wait before next enumeration cycle
            await asyncio.sleep(ENUMERATION_INTERVAL)

    async def _enumerate_blobs(self):
        """Enumerate blobs with checkpointing."""
        if not self.pipelines:
            return

        # Load checkpoint
        checkpoint = self.checkpoint_manager.load()
        continuation_token = self.checkpoint_manager.get_continuation_token()
        items_processed = self.checkpoint_manager.get_items_processed()

        logger.info(
            "Starting enumeration from checkpoint: %d items processed, has_token=%s",
            items_processed, bool(continuation_token)
        )

        # NOTE: We no longer load all refs into memory (caused OOM at 100M docs)
        # Instead, we use upsert which is idempotent - duplicate jobs just update

        items_since_checkpoint = 0
        jobs_created = 0
        last_blob = ""

        while self._running:
            # Fetch page of blobs
            blobs, next_token = await list_blobs_paginated(
                self.source.config,
                page_size=PAGE_SIZE,
                continuation_token=continuation_token,
            )

            if not blobs:
                logger.info("Enumeration complete - no more blobs")
                # Final checkpoint
                self.checkpoint_manager.save(
                    continuation_token=None,
                    last_item=last_blob,
                    items_processed=items_processed,
                    items_since_checkpoint=0,
                    extra_stats={"jobs_created": jobs_created, "status": "completed"},
                )
                self.progress_tracker.set_status(
                    SourceStatus.LIVE, "Backfill enumeration complete"
                )
                break

            # Process blobs in page
            for blob in blobs:
                blob_ref = blob.get("ref", blob.get("name", ""))
                last_blob = blob_ref
                items_processed += 1
                items_since_checkpoint += 1

                # Create job for each pipeline (upsert is idempotent - no need to check existence)
                for pipeline in self.pipelines:
                    job = Job(
                        id=_job_id(self.source_id, pipeline.id, blob_ref),
                        source_id=self.source_id,
                        source_ref=blob_ref,
                        pipeline_id=pipeline.id,
                        status=JobStatus.PENDING,
                        created_at=datetime.utcnow(),
                        payload=blob,
                    )

                    try:
                        self.store.upsert(_to_doc(job, "job"))
                        jobs_created += 1
                    except Exception as e:
                        logger.warning("Failed to create job for %s: %s", blob_ref, e)

                # NOTE: Don't checkpoint mid-page with old continuation_token
                # Checkpoint only after page completion with new token to ensure
                # we don't re-fetch the same page on crash+restart

            # Move to next page BEFORE checkpointing
            # CRITICAL: Save with new token so crash+restart continues from next page
            continuation_token = next_token

            # Save checkpoint after page with NEW continuation token
            self.checkpoint_manager.save(
                continuation_token=continuation_token,
                last_item=last_blob,
                items_processed=items_processed,
                items_since_checkpoint=0,  # Reset after page
                extra_stats={"jobs_created": jobs_created},
            )

            # Update progress after each page
            self.progress_tracker.update_backfill_progress(
                location=self.source.config.get("container", "default"),
                blobs_enumerated=items_processed,
                jobs_created=jobs_created,
                jobs_completed=0,  # Updated by job processing
                jobs_failed=0,
                jobs_pending=jobs_created,
            )

            logger.info(
                "Enumerated page: %d blobs, %d total, %d jobs created",
                len(blobs), items_processed, jobs_created
            )

            if not next_token:
                logger.info("Enumeration complete - reached end")
                self.progress_tracker.set_status(
                    SourceStatus.LIVE, "Backfill enumeration complete"
                )
                break

    async def _get_existing_job_refs(self) -> set:
        """Get set of blob refs that already have jobs."""
        query = (
            "SELECT c.source_ref FROM c "
            "WHERE c.doc_type = 'job' AND c.source_id = @source_id"
        )
        params = [{"name": "@source_id", "value": self.source_id}]

        refs = set()
        try:
            docs = self.store.query(query, params, partition_key="job")
            refs = {doc.get("source_ref") for doc in docs if doc.get("source_ref")}
        except Exception as e:
            logger.warning("Could not load existing refs: %s", e)

        return refs

    async def _job_processing_loop(self):
        """Process pending jobs."""
        while self._running:
            try:
                jobs_processed = await self._process_pending_jobs()
                if jobs_processed == 0:
                    await asyncio.sleep(POLL_INTERVAL)
                else:
                    await asyncio.sleep(0.1)  # Quick poll when busy
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Job processing error: %s", e)
                await asyncio.sleep(5)

    async def _process_pending_jobs(self) -> int:
        """Claim and process pending jobs."""
        # Query pending jobs
        query = (
            "SELECT TOP 500 * FROM c "
            "WHERE c.doc_type = 'job' "
            "AND c.source_id = @source_id "
            "AND c.status = 'pending'"
        )
        params = [{"name": "@source_id", "value": self.source_id}]
        docs = self.store.query(query, params, partition_key="job")

        if not docs:
            return 0

        # Claim jobs (try to update status with etag)
        claimed_jobs = []
        for doc in docs[:BATCH_SIZE]:
            job = await self._try_claim_job(doc)
            if job:
                claimed_jobs.append(job)

        if not claimed_jobs:
            return 0

        logger.info("Processing %d claimed jobs", len(claimed_jobs))

        # Group by pipeline and process
        by_pipeline: Dict[str, List[Job]] = {}
        for job in claimed_jobs:
            by_pipeline.setdefault(job.pipeline_id, []).append(job)

        for pipeline_id, jobs in by_pipeline.items():
            try:
                await process_jobs_batch(jobs)
            except Exception as e:
                logger.error("Batch processing failed for pipeline %s: %s", pipeline_id, e)
                # Immediately reset failed jobs to pending (don't wait for stuck recovery)
                for job in jobs:
                    await self._reset_job_to_pending(job)

        return len(claimed_jobs)

    async def _reset_job_to_pending(self, job: Job):
        """Reset a job back to pending status for retry."""
        try:
            doc = _to_doc(job, "job")
            doc["status"] = JobStatus.PENDING.value
            doc["started_at"] = None
            doc["retry_count"] = getattr(job, "retry_count", 0) + 1
            self.store.upsert(doc)
            logger.debug("Reset job %s to pending", job.id)
        except Exception as e:
            logger.warning("Failed to reset job %s: %s", job.id, e)

    async def _try_claim_job(self, doc: dict) -> Optional[Job]:
        """Try to claim a job using etag."""
        etag = doc.get("_etag")
        job = Job(**_strip_doc(doc))

        if job.status != JobStatus.PENDING:
            return None

        claim_doc = _to_doc(job, "job")
        claim_doc["status"] = JobStatus.PROCESSING.value
        claim_doc["started_at"] = datetime.utcnow().isoformat()

        try:
            self.store.replace_with_etag(claim_doc, etag)
            job.status = JobStatus.PROCESSING
            job.started_at = datetime.utcnow()
            return job
        except CosmosAccessConditionFailedError:
            return None  # Another worker got it

    async def _stuck_job_recovery_loop(self):
        """Recover stuck jobs."""
        while self._running:
            await asyncio.sleep(60)  # Check every minute

            try:
                cutoff = (datetime.utcnow() - timedelta(seconds=300)).isoformat()
                query = (
                    "SELECT TOP 100 * FROM c "
                    "WHERE c.doc_type = 'job' "
                    "AND c.source_id = @source_id "
                    "AND c.status = 'processing' "
                    "AND c.started_at < @cutoff"
                )
                params = [
                    {"name": "@source_id", "value": self.source_id},
                    {"name": "@cutoff", "value": cutoff},
                ]
                stuck = self.store.query(query, params, partition_key="job")

                if stuck:
                    logger.warning("Found %d potentially stuck jobs", len(stuck))
                    reset_count = 0
                    for doc in stuck:
                        # Use etag to ensure job is still in PROCESSING state
                        # This prevents resetting a job that just completed
                        etag = doc.get("_etag")
                        doc["status"] = "pending"
                        doc["started_at"] = None
                        doc["retry_count"] = doc.get("retry_count", 0) + 1

                        try:
                            self.store.replace_with_etag(doc, etag)
                            reset_count += 1
                        except CosmosAccessConditionFailedError:
                            # Job was updated (likely completed) - skip
                            logger.debug("Job %s was updated, skipping reset", doc.get("id"))
                        except Exception as e:
                            logger.warning("Failed to reset job %s: %s", doc.get("id"), e)

                    if reset_count > 0:
                        logger.info("Reset %d stuck jobs", reset_count)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Stuck job recovery error: %s", e)


async def main():
    if not SOURCE_ID:
        logger.error("SOURCE_ID environment variable required")
        return

    worker = BlobBackfillWorker(SOURCE_ID)
    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
