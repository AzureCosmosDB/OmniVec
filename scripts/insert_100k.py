"""Insert a configurable number of test documents into a Cosmos DB container."""
import asyncio
import os
import sys
import time
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

ENDPOINT = os.environ.get("COSMOS_ENDPOINT") or sys.exit("COSMOS_ENDPOINT env var is required")
DATABASE = os.environ.get("COSMOS_DATABASE", "documents")
CONTAINER = os.environ.get("COSMOS_CONTAINER", "throughput-test-10")
TOTAL_DOCS = int(os.environ.get("TOTAL_DOCS", "100000"))
NUM_PARTITIONS = int(os.environ.get("NUM_PARTITIONS", "100"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
ID_PREFIX = os.environ.get("ID_PREFIX", "perf")
ACTION = os.environ.get("ACTION", "insert").lower()
TOMBSTONE = os.environ.get("TOMBSTONE", "false").lower() == "true"
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


async def delete_batch(container, docs):
    """Delete a batch of docs concurrently."""
    tasks = [
        container.delete_item(doc["id"], partition_key=doc["partition_id"])
        for doc in docs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(
        1
        for result in results
        if not isinstance(result, Exception)
        or getattr(result, "status_code", None) == 404
    )
    fail = len(results) - ok
    return ok, fail


async def main():
    if ACTION not in {"insert", "delete"}:
        sys.exit("ACTION must be 'insert' or 'delete'")

    credential = DefaultAzureCredential()
    client = CosmosClient(ENDPOINT, credential)
    db = client.get_database_client(DATABASE)
    container = db.get_container_client(CONTAINER)

    verb = "Inserting" if ACTION == "insert" else "Deleting"
    print(f"{verb} {TOTAL_DOCS} documents in {CONTAINER}...")
    start = time.time()
    total_ok = 0
    total_fail = 0

    batch = []
    for i in range(TOTAL_DOCS):
        part_num = i % NUM_PARTITIONS
        doc = {
            "id": f"{ID_PREFIX}-{i:06d}",
            "partition_id": f"partition-{part_num}",
            "content": "" if TOMBSTONE else CONTENT_TEMPLATE.format(
                doc_id=f"{ID_PREFIX}-{i:06d}",
                part_id=f"partition-{part_num}",
            ),
            "category": f"category-{part_num % 10}",
            "timestamp": time.time(),
        }
        if TOMBSTONE:
            doc["deleted"] = True
        batch.append(doc)

        if len(batch) >= BATCH_SIZE:
            operation = insert_batch if ACTION == "insert" else delete_batch
            ok, fail = await operation(container, batch)
            total_ok += ok
            total_fail += fail
            elapsed = time.time() - start
            rate = total_ok / elapsed if elapsed > 0 else 0
            action = "inserted" if ACTION == "insert" else "deleted"
            print(f"  {total_ok:,}/{TOTAL_DOCS:,} {action} ({rate:.0f} docs/sec, {total_fail} failed)")
            batch = []

    if batch:
        operation = insert_batch if ACTION == "insert" else delete_batch
        ok, fail = await operation(container, batch)
        total_ok += ok
        total_fail += fail

    elapsed = time.time() - start
    action = "inserted" if ACTION == "insert" else "deleted"
    print(f"\nDone: {total_ok:,} {action}, {total_fail} failed in {elapsed:.1f}s ({total_ok/elapsed:.0f} docs/sec)")

    await client.close()
    await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
