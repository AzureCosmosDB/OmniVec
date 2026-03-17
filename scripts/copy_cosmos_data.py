"""Copy data from source skf-rag-test to destination omnivec-skf-rag-test.

Uses bulk executor for fast writes.
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure.cosmos import CosmosClient
from azure.identity import AzureCliCredential

SOURCE_ENDPOINT = "https://skf-rag-test.documents.azure.com:443/"
DEST_ENDPOINT = "https://omnivec-skf-rag-test.documents.azure.com:443/"
DATABASE = "skf-database"
CONTAINERS = ["skf-structured", "skf-unstructured"]
CONCURRENCY = 50
BATCH_SIZE = 200

credential = AzureCliCredential(process_timeout=30)
src_client = CosmosClient(SOURCE_ENDPOINT, credential=credential, enable_endpoint_discovery=False)
dst_client = CosmosClient(DEST_ENDPOINT, credential=credential, enable_endpoint_discovery=False)

src_db = src_client.get_database_client(DATABASE)
dst_db = dst_client.get_database_client(DATABASE)

count_only = "--count" in sys.argv


def clean_item(item):
    for key in ["_rid", "_self", "_etag", "_attachments", "_ts", "embedding"]:
        item.pop(key, None)
    return item


for container_name in CONTAINERS:
    print(f"\n=== {container_name} ===")
    src_container = src_db.get_container_client(container_name)

    count_result = list(src_container.query_items(
        query="SELECT VALUE COUNT(1) FROM c",
        enable_cross_partition_query=True,
    ))
    doc_count = count_result[0] if count_result else "?"
    print(f"  Source docs: {doc_count}")

    if count_only:
        continue

    try:
        dst_container = dst_db.get_container_client(container_name)
        dst_container.read()
    except Exception:
        print(f"  Destination container '{container_name}' not found — skipping")
        continue

    copied = 0
    errors = 0
    start_time = time.time()

    # Use thread pool for concurrent upserts
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    futures = []

    # Skip embedding field to speed up transfer
    for item in src_container.query_items(
        query="SELECT * FROM c",
        enable_cross_partition_query=True,
        max_item_count=BATCH_SIZE,
    ):
        item = clean_item(item)
        futures.append(pool.submit(dst_container.upsert_item, item))

        # Process completed futures periodically
        if len(futures) >= CONCURRENCY * 2:
            done = [f for f in futures if f.done()]
            for f in done:
                try:
                    f.result()
                    copied += 1
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        print(f"  Error: {e}")
                futures.remove(f)

            if copied > 0 and copied % 1000 == 0:
                elapsed = time.time() - start_time
                rate = copied / elapsed
                eta_min = (doc_count - copied) / rate / 60 if rate > 0 else 0
                print(f"  ... {copied}/{doc_count}  ({rate:.0f} docs/s, ETA ~{eta_min:.0f}m)")

    # Drain remaining futures
    for f in as_completed(futures):
        try:
            f.result()
            copied += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  Error: {e}")

    pool.shutdown(wait=False)
    elapsed = time.time() - start_time
    print(f"  Copied: {copied}, Errors: {errors}, Time: {elapsed:.0f}s ({copied/elapsed:.0f} docs/s)")

print("\nDone!")
