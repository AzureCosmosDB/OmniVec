#!/usr/bin/env python3
"""OmniVec Blob Watcher — dedicated deployment for Azure Blob event processing.

Watches Azure Storage Queue for Event Grid blob events, creates jobs,
and processes them. Runs as a standalone deployment (like changefeed processor).
"""

import os
import asyncio
import logging
import concurrent.futures

import httpx

from store import init_store
from job_processor import set_http_client
from worker import watch_storage_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [blob-watcher] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

for _sdk_logger in ("azure.core.pipeline.policies.http_logging_policy",
                     "azure.identity", "azure.core", "urllib3"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)


async def main():
    logger.info("OmniVec Blob Watcher starting")
    init_store()
    logger.info("CosmosDB store initialized")

    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=32))

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30),
    )
    set_http_client(client)

    try:
        await watch_storage_queue()
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
