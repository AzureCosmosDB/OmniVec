#!/usr/bin/env python3
"""OmniVec Worker

Watches the CosmosDB metadata container's Change Feed for new PENDING jobs.
Claims jobs via etag-based optimistic concurrency, then processes them.
Multiple replicas can run safely — only one worker wins the claim.

Also watches Azure Storage Queue for blob events from Event Grid.
"""

import os
import json
import time
import uuid
import asyncio
import logging
import concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import urlparse

from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from models import Job, JobStatus, Source, Pipeline, PipelineStatus, SourceType, PipelineSource
from store import init_store, get_store
from job_processor import process_job, process_jobs_batch, set_http_client

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy Azure SDK HTTP logging
for _sdk_logger in ("azure.core.pipeline.policies.http_logging_policy",
                     "azure.identity", "azure.core", "urllib3"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL", "0.5"))  # seconds
BATCH_SIZE = 200  # max concurrent job processing
BATCH_WAIT_SEC = 2  # max seconds to wait for a full batch

import hashlib


# -- helpers ----------------------------------------------------------------

def _strip_doc(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


def _job_from_doc(doc: dict) -> Job:
    return Job(**_strip_doc(doc))


def _source_from_doc(doc: dict) -> Source:
    return Source(**_strip_doc(doc))


def _pipeline_from_doc(doc: dict) -> Pipeline:
    return Pipeline(**_strip_doc(doc))


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


# -- claim and process ------------------------------------------------------

def try_claim(doc: dict) -> Job | None:
    """Attempt to claim a PENDING job via etag.

    Returns the claimed Job if successful, None if another worker got it.
    """
    store = get_store()
    etag = doc.get("_etag")
    job = _job_from_doc(doc)

    if job.status != JobStatus.PENDING:
        return None

    # Build the claim document: status -> PROCESSING
    claim_doc = _to_doc(job, "job")
    claim_doc["status"] = JobStatus.PROCESSING.value
    claim_doc["started_at"] = datetime.utcnow().isoformat()

    try:
        store.replace_with_etag(claim_doc, etag)
    except CosmosAccessConditionFailedError:
        # Another worker got it
        return None

    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    logger.debug("Claimed job %s (source_ref=%s)", job.id, job.source_ref)
    return job


async def try_claim_and_process(doc: dict) -> bool:
    """Attempt to claim a PENDING job via etag, then process it.

    Returns True if we claimed and processed (or failed) the job.
    Returns False if another worker claimed it first.
    """
    job = try_claim(doc)
    if job is None:
        return False
    await process_job(job)
    return True


async def claim_and_batch_process(docs: list[dict]) -> int:
    """Claim a batch of jobs and process them together.

    Groups claimed jobs by pipeline_id and uses batch processing
    for each group. Returns the number of jobs claimed.
    Claims are done in PARALLEL via thread pool for throughput.
    """
    # Claim all jobs in PARALLEL using threads
    results = await asyncio.gather(*[
        asyncio.to_thread(try_claim, doc) for doc in docs
    ])
    claimed = [j for j in results if j is not None]

    if not claimed:
        return 0

    # Group by pipeline_id for batch processing
    by_pipeline: dict[str, list[Job]] = {}
    for job in claimed:
        by_pipeline.setdefault(job.pipeline_id, []).append(job)

    logger.info(
        "Batch processing %d claimed jobs across %d pipeline(s)",
        len(claimed), len(by_pipeline),
    )

    # Process pipeline groups in PARALLEL
    async def _process_group(pipeline_id, group):
        try:
            await process_jobs_batch(group)
        except Exception as e:
            logger.error("Batch processing failed for pipeline %s: %s", pipeline_id, e)
            for job in group:
                try:
                    await process_job(job)
                except Exception as je:
                    logger.error("Individual fallback failed for job %s: %s", job.id, je)

    await asyncio.gather(*[
        _process_group(pid, group) for pid, group in by_pipeline.items()
    ])

    return len(claimed)


# -- change feed watcher ----------------------------------------------------

async def watch_change_feed():
    """Main loop: subscribe to Change Feed on the metadata container,
    filtering for doc_type=job with status=pending."""
    store = get_store()
    container = store.get_container()

    continuation_token = None
    logger.info("Starting Change Feed watcher on metadata container")

    while True:
        try:
            change_feed = container.query_items_change_feed(
                is_start_from_beginning=continuation_token is None,
                continuation=continuation_token,
                max_item_count=500,
            )

            # Iterate through pages to get continuation token
            items = []
            response = change_feed.by_page()
            for page in response:
                items.extend(page)

            pending_jobs = [
                item for item in items
                if item.get("doc_type") == "job" and item.get("status") == "pending"
            ]

            if pending_jobs:
                logger.info("Change Feed: %d pending jobs detected", len(pending_jobs))
                for i in range(0, len(pending_jobs), BATCH_SIZE):
                    batch = pending_jobs[i:i + BATCH_SIZE]
                    await claim_and_batch_process(batch)

            continuation_token = response.continuation_token
            # Don't sleep if we found jobs — immediately poll for more
            if not pending_jobs:
                await asyncio.sleep(POLL_INTERVAL)
            else:
                await asyncio.sleep(0.05)  # Brief yield

        except asyncio.CancelledError:
            logger.info("Change Feed watcher cancelled")
            raise
        except Exception as e:
            logger.error("Change Feed error: %s", e)
            await asyncio.sleep(10)


# -- also poll for unclaimed PENDING jobs (catch-up) -------------------------

async def poll_pending_jobs():
    """Periodic sweep for PENDING jobs that the Change Feed may have missed
    (e.g., jobs created before the worker started).
    Also reclaims stuck PROCESSING jobs older than 60s."""
    store = get_store()

    while True:
        try:
            await asyncio.sleep(3)  # Run every 3s

            # Get pending jobs
            query = (
                "SELECT TOP 500 * FROM c WHERE c.doc_type = 'job' AND c.status = 'pending'"
            )
            docs = store.query(query, partition_key="job")

            if docs:
                logger.info("Poll sweep: %d pending jobs found", len(docs))
                for i in range(0, len(docs), BATCH_SIZE):
                    batch = docs[i:i + BATCH_SIZE]
                    await claim_and_batch_process(batch)

            # Reclaim stuck PROCESSING jobs (older than 60s)
            cutoff = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
            stuck_query = (
                "SELECT TOP 200 * FROM c WHERE c.doc_type = 'job' "
                "AND c.status = 'processing' AND c.started_at < @cutoff"
            )
            stuck_docs = store.query(stuck_query, [{"name": "@cutoff", "value": cutoff}], partition_key="job")
            if stuck_docs:
                logger.warning("Reclaiming %d stuck PROCESSING jobs", len(stuck_docs))
                # Reset them to PENDING so they get picked up again
                for doc in stuck_docs:
                    doc["status"] = "pending"
                    doc["started_at"] = None
                    try:
                        store.upsert(doc)
                    except Exception:
                        pass
                # Process reclaimed jobs immediately (force=True bypasses hash partitioning)
                reclaimed = store.query(
                    "SELECT TOP 200 * FROM c WHERE c.doc_type = 'job' AND c.status = 'pending'",
                    partition_key="job",
                )
                if reclaimed:
                    for i in range(0, len(reclaimed), BATCH_SIZE):
                        batch = reclaimed[i:i + BATCH_SIZE]
                        await claim_and_batch_process(batch)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Poll sweep error: %s", e)
            await asyncio.sleep(30)


# -- storage queue listener (Event Grid -> blob events) ----------------------

async def handle_blob_event(event: dict) -> int:
    """Match a blob event to active pipelines and create+process jobs.

    Returns count of jobs created.
    """
    subject = event.get("subject", "")
    data = event.get("data", {})
    event_type = event.get("eventType", "")

    if "BlobCreated" not in event_type:
        return 0

    # Parse subject: /blobServices/default/containers/{container}/blobs/{path}
    parts = subject.split("/blobs/", 1)
    if len(parts) != 2:
        logger.warning("Cannot parse blob subject: %s", subject)
        return 0

    container_part = parts[0]  # /blobServices/default/containers/{container}
    blob_path = parts[1]
    container_name = container_part.rsplit("/", 1)[-1]

    # Extract storage account from URL
    blob_url = data.get("url", "")
    parsed = urlparse(blob_url)
    account_name = parsed.netloc.split(".")[0] if parsed.netloc else ""

    file_ext = os.path.splitext(blob_path)[1].lower().lstrip(".")

    store = get_store()

    # Find blob sources that match
    all_sources = [_source_from_doc(d) for d in store.list("source")]
    all_pipelines = [_pipeline_from_doc(d) for d in store.list("pipeline")]

    created = 0
    for source in all_sources:
        if source.type != SourceType.AZURE_BLOB or not source.enabled:
            continue

        src_account = source.config.get("account_url", "")
        src_container = source.config.get("container", "")
        src_prefix = source.config.get("prefix", "")
        allowed_types = source.config.get(
            "file_types", ["txt", "json", "pdf", "md", "csv"]
        )

        if account_name not in src_account or container_name != src_container:
            continue
        if src_prefix and not blob_path.startswith(src_prefix):
            continue
        if file_ext not in allowed_types:
            logger.debug(
                "Skipping blob %s: type '%s' not in %s",
                blob_path, file_ext, allowed_types,
            )
            continue

        # Find active pipelines using this source
        for pipeline in all_pipelines:
            if pipeline.status != PipelineStatus.ACTIVE:
                continue
            if not any(ps.source_id == source.id for ps in pipeline.sources):
                continue

            # Check if job already exists for this blob
            query = (
                "SELECT c.id FROM c "
                "WHERE c.doc_type = 'job' AND c.pipeline_id = @pip "
                "AND c.source_id = @src AND c.source_ref = @ref"
            )
            params = [
                {"name": "@pip", "value": pipeline.id},
                {"name": "@src", "value": source.id},
                {"name": "@ref", "value": blob_path},
            ]
            existing = store.query(query, params, partition_key="job")
            if existing:
                logger.debug(
                    "Job already exists for blob %s in pipeline %s",
                    blob_path, pipeline.name,
                )
                continue

            job = Job(
                id=f"job-{str(uuid.uuid4())[:12]}",
                pipeline_id=pipeline.id,
                source_id=source.id,
                source_ref=blob_path,
                metadata={"trigger": "event_grid", "file_type": file_ext},
                created_at=datetime.utcnow(),
            )
            doc = _to_doc(job, "job")
            store.upsert(doc)
            logger.info(
                "Created job %s for blob %s (pipeline=%s, type=%s)",
                job.id, blob_path, pipeline.name, file_ext,
            )

            created += 1

    return created


async def watch_storage_queue():
    """Poll Azure Storage Queue for blob events delivered by Event Grid."""
    conn_str = os.getenv("STORAGE_CONN_STRING")
    if not conn_str:
        logger.info("STORAGE_CONN_STRING not set -- storage queue listener disabled")
        return

    from azure.storage.queue import QueueClient
    import base64

    queue_name = os.getenv("BLOB_EVENTS_QUEUE", "blob-events")
    queue_client = QueueClient.from_connection_string(conn_str, queue_name)
    logger.info("Starting storage queue listener on queue '%s'", queue_name)

    while True:
        try:
            # Collect jobs: fill up to BATCH_SIZE or until BATCH_WAIT_SEC elapsed
            created_count = 0
            deadline = time.monotonic() + BATCH_WAIT_SEC

            while created_count < BATCH_SIZE and time.monotonic() < deadline:
                want = min(32, BATCH_SIZE - created_count)
                messages = queue_client.receive_messages(
                    max_messages=want, visibility_timeout=300,
                )
                got_any = False
                for msg in messages:
                    got_any = True
                    try:
                        try:
                            content = base64.b64decode(msg.content).decode("utf-8")
                        except Exception:
                            content = msg.content
                        event = json.loads(content)
                        logger.info(
                            "Queue message: %s %s",
                            event.get("eventType", "?"),
                            event.get("subject", "?"),
                        )
                        created_count += await handle_blob_event(event)
                        queue_client.delete_message(msg)
                    except Exception as e:
                        logger.error("Failed to process queue message: %s", e)

                if not got_any:
                    # No messages right now; wait briefly before checking again
                    await asyncio.sleep(1)

            # Process whatever we collected
            if created_count > 0:
                store = get_store()
                pending = store.query(
                    "SELECT * FROM c WHERE c.doc_type = 'job' AND c.status = 'pending'",
                    partition_key="job",
                )
                if pending:
                    logger.info("Batch processing %d pending jobs (batch_size=%d)", len(pending), BATCH_SIZE)
                    for i in range(0, len(pending), BATCH_SIZE):
                        batch = pending[i:i + BATCH_SIZE]
                        await claim_and_batch_process(batch)
            else:
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info("Storage queue listener cancelled")
            raise
        except Exception as e:
            logger.error("Storage queue error: %s", e)
            await asyncio.sleep(10)


# -- main -------------------------------------------------------------------

async def main():
    logger.info("OmniVec Worker starting (BATCH_SIZE=%d, POLL_INTERVAL=%.1f)", BATCH_SIZE, POLL_INTERVAL)
    init_store()
    logger.info("CosmosDB store initialized")

    # Large thread pool for parallel CosmosDB operations
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=64))

    # Set up HTTP client for job_processor
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30),
    )
    set_http_client(client)

    try:
        # Run Change Feed watcher, polling sweep, and storage queue listener
        await asyncio.gather(
            watch_change_feed(),
            poll_pending_jobs(),
            watch_storage_queue(),
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
