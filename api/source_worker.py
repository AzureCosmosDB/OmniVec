#!/usr/bin/env python3
"""OmniVec Source Worker

Per-source job processor that only handles jobs for a specific source.
Enables independent scaling per source.

Environment Variables:
    SOURCE_ID: Required - The source ID to process jobs for
    COSMOS_ENDPOINT: Required - CosmosDB endpoint
    WORKER_POLL_INTERVAL: Optional - Seconds between polls (default: 0.5)
    BATCH_SIZE: Optional - Max concurrent jobs (default: 50)
"""

import os
import asyncio
import logging
import concurrent.futures
from datetime import datetime, timedelta

from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from models import Job, JobStatus
from store import init_store, get_store
from job_processor import process_job, process_jobs_batch, set_http_client

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [source-worker] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy Azure SDK logging
for _sdk_logger in ("azure.core.pipeline.policies.http_logging_policy",
                     "azure.identity", "azure.core", "urllib3"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

# Configuration
SOURCE_ID = os.environ.get("SOURCE_ID")
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL", "0.5"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))


def _strip_doc(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


def _job_from_doc(doc: dict) -> Job:
    return Job(**_strip_doc(doc))


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


def try_claim(doc: dict) -> Job | None:
    """Attempt to claim a PENDING job via etag.

    Returns the claimed Job if successful, None if another worker got it.
    """
    store = get_store()
    etag = doc.get("_etag")
    job = _job_from_doc(doc)

    if job.status != JobStatus.PENDING:
        return None

    claim_doc = _to_doc(job, "job")
    claim_doc["status"] = JobStatus.PROCESSING.value
    claim_doc["started_at"] = datetime.utcnow().isoformat()

    try:
        store.replace_with_etag(claim_doc, etag)
    except CosmosAccessConditionFailedError:
        return None

    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    logger.debug("Claimed job %s (source_ref=%s)", job.id, job.source_ref)
    return job


async def claim_and_batch_process(docs: list[dict]) -> int:
    """Claim jobs and process them in batches.

    Returns the number of jobs claimed.
    """
    # Claim in parallel
    results = await asyncio.gather(*[
        asyncio.to_thread(try_claim, doc) for doc in docs
    ])
    claimed = [j for j in results if j is not None]

    if not claimed:
        return 0

    # Group by pipeline_id
    by_pipeline: dict[str, list[Job]] = {}
    for job in claimed:
        by_pipeline.setdefault(job.pipeline_id, []).append(job)

    logger.info(
        "Processing %d claimed jobs across %d pipeline(s) for source %s",
        len(claimed), len(by_pipeline), SOURCE_ID
    )

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


async def poll_source_jobs():
    """Poll for PENDING jobs for this source only."""
    store = get_store()
    source_id = SOURCE_ID

    logger.info("Starting job poller for source %s", source_id)

    while True:
        try:
            # Query PENDING jobs for this source
            query = (
                "SELECT TOP 500 * FROM c "
                "WHERE c.doc_type = 'job' "
                "AND c.status = 'pending' "
                "AND c.source_id = @source_id"
            )
            params = [{"name": "@source_id", "value": source_id}]
            docs = store.query(query, params, partition_key="job")

            if docs:
                logger.info("Found %d pending jobs for source %s", len(docs), source_id)
                for i in range(0, len(docs), BATCH_SIZE):
                    batch = docs[i:i + BATCH_SIZE]
                    await claim_and_batch_process(batch)
                # Quick poll after finding jobs
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Job poller cancelled")
            raise
        except Exception as e:
            logger.error("Poll error for source %s: %s", source_id, e)
            await asyncio.sleep(10)


async def reclaim_stuck_jobs():
    """Reclaim stuck PROCESSING jobs for this source."""
    store = get_store()
    source_id = SOURCE_ID

    while True:
        try:
            await asyncio.sleep(30)  # Check every 30s

            cutoff = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
            query = (
                "SELECT TOP 100 * FROM c "
                "WHERE c.doc_type = 'job' "
                "AND c.status = 'processing' "
                "AND c.source_id = @source_id "
                "AND c.started_at < @cutoff"
            )
            params = [
                {"name": "@source_id", "value": source_id},
                {"name": "@cutoff", "value": cutoff},
            ]
            stuck_docs = store.query(query, params, partition_key="job")

            if stuck_docs:
                logger.warning("Reclaiming %d stuck jobs for source %s", len(stuck_docs), source_id)
                for doc in stuck_docs:
                    doc["status"] = "pending"
                    doc["started_at"] = None
                    try:
                        store.upsert(doc)
                    except Exception:
                        pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Reclaim error for source %s: %s", source_id, e)
            await asyncio.sleep(60)


async def watch_change_feed():
    """Watch Change Feed for jobs specific to this source."""
    store = get_store()
    container = store.get_container()
    source_id = SOURCE_ID

    continuation_token = None
    logger.info("Starting Change Feed watcher for source %s", source_id)

    while True:
        try:
            change_feed = container.query_items_change_feed(
                is_start_from_beginning=continuation_token is None,
                continuation=continuation_token,
                max_item_count=500,
            )

            items = []
            response = change_feed.by_page()
            for page in response:
                items.extend(page)

            # Filter for pending jobs for THIS source only
            pending_jobs = [
                item for item in items
                if item.get("doc_type") == "job"
                and item.get("status") == "pending"
                and item.get("source_id") == source_id
            ]

            if pending_jobs:
                logger.info("Change Feed: %d pending jobs for source %s", len(pending_jobs), source_id)
                for i in range(0, len(pending_jobs), BATCH_SIZE):
                    batch = pending_jobs[i:i + BATCH_SIZE]
                    await claim_and_batch_process(batch)

            continuation_token = response.continuation_token

            if not pending_jobs:
                await asyncio.sleep(POLL_INTERVAL)
            else:
                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            logger.info("Change Feed watcher cancelled")
            raise
        except Exception as e:
            logger.error("Change Feed error for source %s: %s", source_id, e)
            await asyncio.sleep(10)


async def main():
    if not SOURCE_ID:
        logger.error("SOURCE_ID environment variable is required")
        return

    logger.info("Source Worker starting for source %s (BATCH_SIZE=%d)", SOURCE_ID, BATCH_SIZE)
    init_store()
    logger.info("CosmosDB store initialized")

    # Thread pool for parallel operations
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=32))

    # HTTP client for job processor
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=10, keepalive_expiry=30),
    )
    set_http_client(client)

    try:
        await asyncio.gather(
            watch_change_feed(),
            poll_source_jobs(),
            reclaim_stuck_jobs(),
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
