#!/usr/bin/env python3
"""PostgreSQL Worker - Polls PostgreSQL for changes and processes them.

Simple polling-based approach:
1. Track last processed timestamp per source
2. Poll for rows with updated_at > last_timestamp
3. Create jobs and process them
4. Update checkpoint

For real-time changes, consider logical replication (future enhancement).
"""

import os
import asyncio
import logging
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List

import httpx

from models import Source, SourceType, Pipeline, Job, JobStatus
from store import init_store, get_store
from checkpoint_manager import CheckpointManager
from progress_tracker import ProgressTracker, SourceStatus
from job_processor import process_jobs_batch, set_http_client
from connectors.postgres_connector import (
    get_rows_since,
    stream_all_rows,
    count_rows,
    close_pool,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [postgres-worker] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for _logger in ("asyncpg", "azure", "azure.core", "azure.cosmos", "azure.identity", "httpx", "httpcore"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

# Configuration
SOURCE_ID = os.environ.get("SOURCE_ID")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "100"))


def _strip_doc(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


def _job_id(source_id: str, pipeline_id: str, row_id: str) -> str:
    """Generate deterministic job ID."""
    return f"job-{hashlib.sha256(f'{source_id}:{pipeline_id}:{row_id}'.encode()).hexdigest()[:16]}"


class PostgresWorker:
    """Worker for processing PostgreSQL source data."""

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.store = None
        self.source: Optional[Source] = None
        self.pipelines: List[Pipeline] = []
        self.checkpoint_manager: Optional[CheckpointManager] = None
        self.progress_tracker: Optional[ProgressTracker] = None
        self._running = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._tasks: List[asyncio.Task] = []

    async def start(self):
        """Start the worker."""
        logger.info("PostgreSQL Worker starting for source %s", self.source_id)

        init_store()
        self.store = get_store()

        # Load source
        self.source = await self._load_source()
        if not self.source:
            logger.error("Source %s not found", self.source_id)
            return

        if self.source.type != SourceType.POSTGRESQL:
            logger.error("Source %s is not PostgreSQL type", self.source_id)
            return

        # Load pipelines
        self.pipelines = await self._load_pipelines()
        if not self.pipelines:
            logger.warning("No active pipelines for source %s", self.source_id)

        # Initialize managers
        self.checkpoint_manager = CheckpointManager(
            self.source_id, "postgres", self.source.config.get("table", "")
        )
        self.progress_tracker = ProgressTracker(self.source_id)

        # Initialize HTTP client
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=20),
        )
        set_http_client(self._http_client)

        self._running = True

        # Run main loops
        try:
            self._tasks = [
                asyncio.create_task(self._poll_loop(), name="poll"),
                asyncio.create_task(self._job_processing_loop(), name="jobs"),
            ]
            await asyncio.gather(*self._tasks)
        finally:
            await self._http_client.aclose()
            await close_pool()

    async def stop(self):
        """Stop the worker gracefully."""
        logger.info("PostgreSQL Worker stopping")
        self._running = False

        for task in self._tasks:
            if not task.done():
                task.cancel()

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
        # Query pipelines where status='active' and any source has matching source_id
        query = (
            "SELECT * FROM c WHERE c.doc_type = 'pipeline' "
            "AND c.status = 'active' "
            "AND EXISTS(SELECT VALUE s FROM s IN c.sources WHERE s.source_id = @source_id)"
        )
        params = [{"name": "@source_id", "value": self.source_id}]
        docs = self.store.query(query, params, partition_key="pipeline")
        return [Pipeline(**_strip_doc(doc)) for doc in docs]

    async def _poll_loop(self):
        """Poll for changes and create jobs."""
        # Initial backfill if no checkpoint
        checkpoint = self.checkpoint_manager.load()
        if not checkpoint:
            logger.info("No checkpoint found, starting initial backfill")
            await self._do_backfill()

        # Poll loop
        while self._running:
            try:
                await self._poll_for_changes()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Poll error: %s", e)
                self.progress_tracker.set_status(
                    SourceStatus.ERROR, f"Poll failed: {e}"
                )

            # Wait before next poll
            poll_interval = self.source.config.get("poll_interval_seconds", POLL_INTERVAL)
            await asyncio.sleep(poll_interval)

    async def _do_backfill(self):
        """Process all existing rows (initial backfill)."""
        if not self.pipelines:
            return

        self.progress_tracker.set_status(SourceStatus.BACKFILLING, "Starting backfill")

        config = self.source.config
        total = await count_rows(config)
        logger.info("Starting backfill of %d rows", total)

        processed = 0
        jobs_created = 0

        async for batch in stream_all_rows(config, batch_size=BATCH_SIZE):
            for row in batch:
                row_id = row["_id"]

                for pipeline in self.pipelines:
                    job = Job(
                        id=_job_id(self.source_id, pipeline.id, row_id),
                        source_id=self.source_id,
                        source_ref=row_id,
                        pipeline_id=pipeline.id,
                        status=JobStatus.PENDING,
                        created_at=datetime.utcnow(),
                        payload={
                            "content": row.get("_content", ""),
                            "row": {k: v for k, v in row.items() if not k.startswith("_")},
                        },
                    )
                    self.store.upsert(_to_doc(job, "job"))
                    jobs_created += 1

                processed += 1

                # Checkpoint periodically
                if processed % CHECKPOINT_INTERVAL == 0:
                    self.checkpoint_manager.save(
                        continuation_token=None,
                        last_item=row_id,
                        items_processed=processed,
                        extra_stats={"jobs_created": jobs_created, "phase": "backfill"},
                    )
                    self.progress_tracker.update_backfill_progress(
                        location=config.get("table", "default"),
                        blobs_enumerated=processed,
                        jobs_created=jobs_created,
                        jobs_completed=0,
                        jobs_failed=0,
                        jobs_pending=jobs_created,
                        total_estimated=total,
                    )

            if not self._running:
                break

        # Final checkpoint with current timestamp
        now = datetime.utcnow()
        self.checkpoint_manager.save(
            continuation_token=now.isoformat(),
            last_item="",
            items_processed=processed,
            extra_stats={
                "jobs_created": jobs_created,
                "phase": "live",
                "last_poll": now.isoformat(),
            },
        )

        self.progress_tracker.set_status(SourceStatus.LIVE, "Backfill complete, polling for changes")
        logger.info("Backfill complete: %d rows, %d jobs", processed, jobs_created)

    async def _poll_for_changes(self):
        """Poll for rows changed since last checkpoint."""
        if not self.pipelines:
            return

        # Get last timestamp from checkpoint
        checkpoint = self.checkpoint_manager.load()
        last_ts_str = self.checkpoint_manager.get_continuation_token()

        if last_ts_str:
            try:
                last_ts = datetime.fromisoformat(last_ts_str)
            except ValueError:
                last_ts = None
        else:
            last_ts = None

        config = self.source.config
        batch_size = config.get("batch_size", BATCH_SIZE)

        # Get changed rows
        rows, max_ts = await get_rows_since(config, since=last_ts, limit=batch_size)

        if not rows:
            return

        logger.info("Found %d changed rows since %s", len(rows), last_ts)

        jobs_created = 0
        for row in rows:
            row_id = row["_id"]

            for pipeline in self.pipelines:
                job = Job(
                    id=_job_id(self.source_id, pipeline.id, row_id),
                    source_id=self.source_id,
                    source_ref=row_id,
                    pipeline_id=pipeline.id,
                    status=JobStatus.PENDING,
                    created_at=datetime.utcnow(),
                    payload={
                        "content": row.get("_content", ""),
                        "row": {k: v for k, v in row.items() if not k.startswith("_")},
                    },
                )
                self.store.upsert(_to_doc(job, "job"))
                jobs_created += 1

        # Update checkpoint with new timestamp
        if max_ts:
            items_processed = self.checkpoint_manager.get_items_processed() + len(rows)
            self.checkpoint_manager.save(
                continuation_token=max_ts.isoformat(),
                last_item=rows[-1]["_id"] if rows else "",
                items_processed=items_processed,
                extra_stats={
                    "jobs_created": jobs_created,
                    "phase": "live",
                    "last_poll": datetime.utcnow().isoformat(),
                },
            )

        self.progress_tracker.update_live_progress(
            events_received=len(rows),
            events_processed=0,
            events_pending=jobs_created,
            throughput_per_sec=0,
        )

    async def _job_processing_loop(self):
        """Process pending jobs."""
        while self._running:
            try:
                jobs_processed = await self._process_pending_jobs()
                if jobs_processed == 0:
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Job processing error: %s", e)
                await asyncio.sleep(5)

    async def _process_pending_jobs(self) -> int:
        """Claim and process pending jobs."""
        query = (
            "SELECT TOP 100 * FROM c "
            "WHERE c.doc_type = 'job' "
            "AND c.source_id = @source_id "
            "AND c.status = 'pending'"
        )
        params = [{"name": "@source_id", "value": self.source_id}]
        docs = self.store.query(query, params, partition_key="job")

        if not docs:
            return 0

        # Claim jobs
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
                # Reset failed jobs
                for job in jobs:
                    await self._reset_job_to_pending(job)

        return len(claimed_jobs)

    async def _try_claim_job(self, doc: dict) -> Optional[Job]:
        """Try to claim a job using etag."""
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

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
            return None

    async def _reset_job_to_pending(self, job: Job):
        """Reset a job back to pending status for retry."""
        try:
            doc = _to_doc(job, "job")
            doc["status"] = JobStatus.PENDING.value
            doc["started_at"] = None
            doc["retry_count"] = getattr(job, "retry_count", 0) + 1
            self.store.upsert(doc)
        except Exception as e:
            logger.warning("Failed to reset job %s: %s", job.id, e)


async def main():
    if not SOURCE_ID:
        logger.error("SOURCE_ID environment variable required")
        return

    worker = PostgresWorker(SOURCE_ID)
    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
