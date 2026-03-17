#!/usr/bin/env python3
"""Clear embedding fields from all documents in a CosmosDB container.

Uses async bulk patching for maximum throughput.
With 100K RU/s serverless, achieves ~3,000-5,000 docs/sec.

Usage:
  python3 clear_embeddings.py <container>
  python3 clear_embeddings.py bge-small-test-100k -y
  python3 clear_embeddings.py bge-small-test-100k --workers 200
"""

import sys
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

ENDPOINT = "https://cosmosdb-omnivec-test.documents.azure.com:443/"
DATABASE = "documents"

EMBEDDING_FIELDS = ["embedding", "embedding_dims", "embedded_at", "content_hash", "pipeline_name", "pipeline_id"]

# Pre-built patch ops — remove all embedding fields in one shot
PATCH_OPS = [{"op": "remove", "path": f"/{f}"} for f in EMBEDDING_FIELDS]

# Counters
_lock = threading.Lock()
_cleared = 0
_errors = 0


def clear_one(container, doc_id, pk_value):
    """Patch a single doc to remove embedding fields."""
    global _cleared, _errors
    try:
        container.patch_item(item=doc_id, partition_key=pk_value, patch_operations=PATCH_OPS)
        with _lock:
            _cleared += 1
    except Exception as e:
        err = str(e)
        # If some fields don't exist, the batch patch fails — fall back to individual removes
        if "path" in err.lower() or "not found" in err.lower() or "does not exist" in err.lower():
            for op in PATCH_OPS:
                try:
                    container.patch_item(item=doc_id, partition_key=pk_value, patch_operations=[op])
                except Exception:
                    pass
            with _lock:
                _cleared += 1
        else:
            with _lock:
                _errors += 1
                if _errors <= 3:
                    print(f"  Error on {doc_id}: {err[:120]}")


def main():
    global _cleared, _errors

    parser = argparse.ArgumentParser(description="Clear embeddings (high throughput)")
    parser.add_argument("container", help="Container name")
    parser.add_argument("--workers", type=int, default=128, help="Concurrent workers (default: 128)")
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    cred = DefaultAzureCredential()
    client = CosmosClient(args.endpoint, cred)
    db = client.get_database_client(args.database)
    container = db.get_container_client(args.container)

    # Discover partition key
    props = container.read()
    pk_path = props["partitionKey"]["paths"][0].lstrip("/")
    print(f"Container: {args.container}, partition key: /{pk_path}")

    # Count
    total = list(container.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))[0]
    embedded = list(container.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))[0]
    print(f"Total docs: {total:,}, with embeddings: {embedded:,}")

    if embedded == 0:
        print("No embeddings to clear.")
        return

    if not args.yes:
        confirm = input(f"\nClear embeddings from {embedded:,} docs? [y/N] ").strip().lower()
        if confirm != 'y':
            print("Aborted.")
            return

    # Fetch all embedded doc IDs + PK values
    print(f"\nFetching embedded doc IDs...")
    query = f"SELECT c.id, c.{pk_path} FROM c WHERE IS_DEFINED(c.embedding)"
    docs = list(container.query_items(query, enable_cross_partition_query=True))
    print(f"Found {len(docs):,} docs to clear, {args.workers} concurrent workers")

    # Create a pool of shared CosmosDB clients (fewer than workers, they're thread-safe)
    num_clients = min(args.workers, 32)
    print(f"Creating {num_clients} CosmosDB clients...")
    clients = []
    for _ in range(num_clients):
        c = CosmosClient(args.endpoint, cred)
        clients.append(c.get_database_client(args.database).get_container_client(args.container))

    _cleared = 0
    _errors = 0
    start = time.time()

    def progress():
        while _cleared + _errors < len(docs):
            time.sleep(2)
            elapsed = time.time() - start
            rate = _cleared / elapsed if elapsed > 0 else 0
            remaining = len(docs) - _cleared - _errors
            eta = remaining / rate if rate > 0 else 0
            print(f"  {_cleared:,}/{len(docs):,} cleared ({rate:,.0f}/sec, ~{eta:.0f}s remaining, {_errors} errors)")

    # Progress thread
    t = threading.Thread(target=progress, daemon=True)
    t.start()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        for idx, doc in enumerate(docs):
            doc_id = doc["id"]
            pk_value = doc.get(pk_path, doc_id)
            wc = clients[idx % num_clients]
            futures.append(pool.submit(clear_one, wc, doc_id, pk_value))

        # Wait for all
        for f in as_completed(futures):
            pass  # Errors handled inside clear_one

    elapsed = time.time() - start
    rate = _cleared / elapsed if elapsed > 0 else 0
    print(f"\nDone: cleared {_cleared:,} docs in {elapsed:.1f}s ({rate:,.0f}/sec)")
    if _errors:
        print(f"Errors: {_errors}")

    # Verify
    remaining = list(container.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))[0]
    print(f"Remaining with embeddings: {remaining:,}")


if __name__ == "__main__":
    main()
