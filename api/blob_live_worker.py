#!/usr/bin/env python3
"""Blob Live Worker - Per-source incremental change processor.

Processes new/modified blobs via Azure Storage Queue (fed by Event Grid):
1. Reads events from storage queue
2. Creates jobs for new/modified blobs
3. Processes jobs with priority

Crash-resilient:
- Queue messages have visibility timeout
- If not deleted within timeout, message reappears
- Jobs claimed via etag
"""

import os
import asyncio
import logging
import json
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List

import httpx

from models import Source, Pipeline, Job, JobStatus
from store import init_store, get_store
from progress_tracker import ProgressTracker
from job_processor import process_job, set_http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [blob-live] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for _logger in ("azure.core", "azure.identity", "urllib3"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

# Configuration
SOURCE_ID = os.environ.get("SOURCE_ID")
QUEUE_NAME = os.environ.get("QUEUE_NAME", f"blob-events-{SOURCE_ID}")
STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT")
VISIBILITY_TIMEOUT = int(os.environ.get("VISIBILITY_TIMEOUT", "300"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "32"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))


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


class BlobLiveWorker:
    """Worker for processing live blob events."""

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.store = None
        self.source: Optional[Source] = None
        self.pipelines: List[Pipeline] = []
        self.progress_tracker: Optional[ProgressTracker] = None
        self._queue_client = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._running = False

        # Metrics
        self.events_received = 0
        self.events_processed = 0
        self.events_failed = 0

    async def start(self):
        """Start the worker."""
        logger.info("Blob Live Worker starting for source %s", self.source_id)

        init_store()
        self.store = get_store()

        # Load source
        self.source = await self._load_source()
        if not self.source:
            logger.error("Source %s not found", self.source_id)
            return

        # Load pipelines
        self.pipelines = await self._load_pipelines()

        # Initialize queue client
        await self._init_queue_client()

        # Initialize progress tracker
        self.progress_tracker = ProgressTracker(self.source_id)

        # Initialize HTTP client
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=20),
        )
        set_http_client(self._http_client)

        self._running = True

        try:
            await asyncio.gather(
                self._event_processing_loop(),
                self._progress_update_loop(),
            )
        finally:
            await self._http_client.aclose()

    async def stop(self):
        """Stop the worker."""
        logger.info("Blob Live Worker stopping")
        self._running = False

    async def _load_source(self) -> Optional[Source]:
        """Load source from CosmosDB."""
        try:
            doc = self.store.get(self.source_id, partition_key="source")
            return Source(**_strip_doc(doc))
        except Exception as e:
            logger.error("Failed to load source: %s", e)
            return None

    async def _load_pipelines(self) -> List[Pipeline]:
        """Load active pipelines for this source."""
        query = (
            "SELECT * FROM c WHERE c.doc_type = 'pipeline' "
            "AND c.is_active = true "
            "AND ARRAY_CONTAINS(c.source_ids, @source_id)"
        )
        params = [{"name": "@source_id", "value": self.source_id}]
        docs = self.store.query(query, params, partition_key="pipeline")
        return [Pipeline(**_strip_doc(doc)) for doc in docs]

    async def _init_queue_client(self):
        """Initialize Azure Storage Queue client."""
        try:
            from azure.storage.queue.aio import QueueClient
            from azure.identity.aio import DefaultAzureCredential

            # Get storage account from source config or environment
            account = self.source.config.get("account", STORAGE_ACCOUNT)
            if not account:
                logger.warning("No storage account configured for queue")
                return

            queue_url = f"https://{account}.queue.core.windows.net/{QUEUE_NAME}"
            credential = DefaultAzureCredential()
            self._queue_client = QueueClient.from_queue_url(queue_url, credential)
            logger.info("Queue client initialized: %s", queue_url)

        except ImportError:
            logger.warning("azure-storage-queue not installed")
        except Exception as e:
            logger.error("Failed to init queue client: %s", e)

    async def _event_processing_loop(self):
        """Process events from queue."""
        while self._running:
            try:
                events_processed = await self._process_queue_messages()
                if events_processed == 0:
                    await asyncio.sleep(POLL_INTERVAL)
                else:
                    await asyncio.sleep(0.1)  # Quick poll when busy
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Event processing error: %s", e)
                await asyncio.sleep(5)

    async def _process_queue_messages(self) -> int:
        """Process messages from queue."""
        if not self._queue_client:
            return 0

        try:
            messages = await self._queue_client.receive_messages(
                max_messages=BATCH_SIZE,
                visibility_timeout=VISIBILITY_TIMEOUT,
            )

            messages_list = [m async for m in messages]
            if not messages_list:
                return 0

            self.events_received += len(messages_list)
            logger.info("Received %d queue messages", len(messages_list))

            for message in messages_list:
                try:
                    await self._process_message(message)
                    # Delete message after successful processing
                    await self._queue_client.delete_message(message)
                    self.events_processed += 1
                except Exception as e:
                    logger.error("Failed to process message: %s", e)
                    self.events_failed += 1
                    # Message will reappear after visibility timeout

            return len(messages_list)

        except Exception as e:
            logger.error("Queue receive error: %s", e)
            return 0

    async def _process_message(self, message):
        """Process a single queue message (Event Grid event)."""
        # Parse Event Grid event
        content = message.content
        if isinstance(content, bytes):
            content = content.decode("utf-8")

        events = json.loads(content)
        if not isinstance(events, list):
            events = [events]

        for event in events:
            event_type = event.get("eventType", "")
            data = event.get("data", {})

            # Handle blob created/modified events
            if event_type in ("Microsoft.Storage.BlobCreated",
                            "Microsoft.Storage.BlobModified"):
                blob_url = data.get("url", "")
                blob_path = self._extract_blob_path(blob_url)

                if blob_path:
                    await self._process_blob_event(blob_path, event)

    def _extract_blob_path(self, blob_url: str) -> Optional[str]:
        """Extract blob path from URL."""
        # Format: https://account.blob.core.windows.net/container/path/to/blob
        try:
            from urllib.parse import urlparse
            parsed = urlparse(blob_url)
            path = parsed.path.lstrip("/")
            # Remove container name (first segment)
            parts = path.split("/", 1)
            return parts[1] if len(parts) > 1 else path
        except Exception:
            return None

    async def _process_blob_event(self, blob_path: str, event: dict):
        """Process a blob create/modify event."""
        # Create job for each pipeline
        for pipeline in self.pipelines:
            job_id = _job_id(self.source_id, pipeline.id, blob_path)

            job = Job(
                id=job_id,
                source_id=self.source_id,
                source_ref=blob_path,
                pipeline_id=pipeline.id,
                status=JobStatus.PENDING,
                created_at=datetime.utcnow(),
                payload={
                    "ref": blob_path,
                    "event_type": event.get("eventType"),
                    "event_time": event.get("eventTime"),
                    "content_type": event.get("data", {}).get("contentType"),
                    "content_length": event.get("data", {}).get("contentLength"),
                },
            )

            # Upsert job (may already exist from backfill)
            job_doc = _to_doc(job, "job")
            self.store.upsert(job_doc)

            # Process immediately (live events are priority)
            try:
                # Claim and process
                job.status = JobStatus.PROCESSING
                job.started_at = datetime.utcnow()
                job_doc["status"] = JobStatus.PROCESSING.value
                job_doc["started_at"] = datetime.utcnow().isoformat()
                self.store.upsert(job_doc)

                await process_job(job)

            except Exception as e:
                logger.error("Failed to process live job %s: %s", job_id, e)

    async def _progress_update_loop(self):
        """Update progress periodically."""
        while self._running:
            await asyncio.sleep(30)

            try:
                # Calculate throughput
                throughput = 0
                pending = self.events_received - self.events_processed - self.events_failed

                self.progress_tracker.update_live_progress(
                    events_received=self.events_received,
                    events_processed=self.events_processed,
                    events_pending=pending,
                    throughput_per_sec=throughput,
                )
            except Exception as e:
                logger.warning("Progress update failed: %s", e)


async def main():
    if not SOURCE_ID:
        logger.error("SOURCE_ID environment variable required")
        return

    worker = BlobLiveWorker(SOURCE_ID)
    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
