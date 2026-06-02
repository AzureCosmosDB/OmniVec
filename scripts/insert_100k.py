"""Insert 100K test documents into throughput-test container."""
import asyncio
import os
import sys
import time
import uuid  # lgtm[py/unused-import]
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

ENDPOINT = os.environ.get("COSMOS_ENDPOINT") or sys.exit("COSMOS_ENDPOINT env var is required")
DATABASE = "documents"
CONTAINER = "throughput-test-10"
TOTAL_DOCS = 100_000
NUM_PARTITIONS = 100  # spread across 100 partitions
BATCH_SIZE = 500  # concurrent upserts per batch
CONTENT_TEMPLATE = (
    "Document {doc_id} in partition {part_id}. "
    "This is a test document for throughput testing of the OmniVec Change Feed Processor. "
    "The vector embedding pipeline will process this content and generate embeddings. "
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat."
)


async def insert_batch(container, docs):
    """Insert a batch of docs concurrently."""
    tasks = []
    for doc in docs:
        tasks.append(container.upsert_item(doc))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    fail = sum(1 for r in results if isinstance(r, Exception))
    return ok, fail


async def main():
    credential = DefaultAzureCredential()
    client = CosmosClient(ENDPOINT, credential)
    db = client.get_database_client(DATABASE)
    container = db.get_container_client(CONTAINER)

    print(f"Inserting {TOTAL_DOCS} documents into {CONTAINER}...")
    start = time.time()
    total_ok = 0
    total_fail = 0

    batch = []
    for i in range(TOTAL_DOCS):
        part_num = i % NUM_PARTITIONS
        doc = {
            "id": f"perf-{i:06d}",
            "partition_id": f"partition-{part_num}",
            "content": CONTENT_TEMPLATE.format(doc_id=f"perf-{i:06d}", part_id=f"partition-{part_num}"),
            "category": f"category-{part_num % 10}",
            "timestamp": time.time(),
        }
        batch.append(doc)

        if len(batch) >= BATCH_SIZE:
            ok, fail = await insert_batch(container, batch)
            total_ok += ok
            total_fail += fail
            elapsed = time.time() - start
            rate = total_ok / elapsed if elapsed > 0 else 0
            print(f"  {total_ok:,}/{TOTAL_DOCS:,} inserted ({rate:.0f} docs/sec, {total_fail} failed)")
            batch = []

    if batch:
        ok, fail = await insert_batch(container, batch)
        total_ok += ok
        total_fail += fail

    elapsed = time.time() - start
    print(f"\nDone: {total_ok:,} inserted, {total_fail} failed in {elapsed:.1f}s ({total_ok/elapsed:.0f} docs/sec)")

    await client.close()
    await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
