#!/usr/bin/env python3
"""Query CosmosDB containers via managed identity.

Usage:
  python3 query_cosmos.py                          # interactive menu
  python3 query_cosmos.py stats                    # embedding stats for all containers
  python3 query_cosmos.py sample <container>       # show 5 sample docs with embeddings
  python3 query_cosmos.py count <container>        # count embedded vs total
  python3 query_cosmos.py query <container> <sql>  # run arbitrary SQL query
  python3 query_cosmos.py verify <container>       # verify embeddings are real (dims, non-zero)
"""

import sys
import json
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

ENDPOINT = "https://cosmosdb-omnivec-test.documents.azure.com:443/"
DATABASE = "documents"

# Known test containers
CONTAINERS = [
    "bge-small-test-100k",
    "bge-test-100k",
    "throughput-test",
    "throughput-test-10",
    "vectors",
]


def get_client():
    return CosmosClient(ENDPOINT, DefaultAzureCredential())


def get_container(name):
    return get_client().get_database_client(DATABASE).get_container_client(name)


def run_query(container_name, sql):
    container = get_container(container_name)
    results = list(container.query_items(sql, enable_cross_partition_query=True))
    return results


def cmd_count(container_name):
    c = get_container(container_name)
    total = list(c.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))[0]
    embedded = list(c.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))[0]
    pct = (embedded / total * 100) if total > 0 else 0
    print(f"\n  Container: {container_name}")
    print(f"  Total docs:    {total:,}")
    print(f"  With embedding: {embedded:,} ({pct:.1f}%)")
    print(f"  Without:        {total - embedded:,}")
    return total, embedded


def cmd_sample(container_name, n=5):
    results = run_query(container_name, f"""
        SELECT TOP {n}
            c.id,
            c.embedded_at,
            c.embedding_dims,
            c.pipeline_name,
            c.pipeline_id,
            c.content_hash,
            ARRAY_LENGTH(c.embedding) as vec_len,
            ARRAY_SLICE(c.embedding, 0, 5) as vec_preview,
            LEFT(c.content, 100) as content_preview
        FROM c
        WHERE IS_DEFINED(c.embedding)
    """)
    print(f"\n  {len(results)} sample docs from {container_name}:\n")
    for d in results:
        print(json.dumps(d, indent=2, default=str))
        print()


def cmd_verify(container_name):
    print(f"\n  Verifying embeddings in {container_name}...")

    # Check dimensions consistency
    dims = run_query(container_name, """
        SELECT DISTINCT VALUE c.embedding_dims
        FROM c WHERE IS_DEFINED(c.embedding)
    """)
    print(f"  Embedding dimensions found: {dims}")

    # Check for zero vectors
    zero_check = run_query(container_name, """
        SELECT VALUE COUNT(1) FROM c
        WHERE IS_DEFINED(c.embedding) AND c.embedding[0] = 0 AND c.embedding[1] = 0 AND c.embedding[2] = 0
    """)
    print(f"  Docs with first 3 dims = 0: {zero_check[0]} (should be ~0)")

    # Check embedded_at timestamps
    ts = run_query(container_name, """
        SELECT
            MIN(c.embedded_at) as earliest,
            MAX(c.embedded_at) as latest
        FROM c WHERE IS_DEFINED(c.embedded_at)
    """)
    if ts:
        print(f"  Earliest embedded_at: {ts[0].get('earliest')}")
        print(f"  Latest embedded_at:   {ts[0].get('latest')}")

    # Check pipeline distribution
    pipelines = run_query(container_name, """
        SELECT c.pipeline_name, COUNT(1) as doc_count
        FROM c WHERE IS_DEFINED(c.pipeline_name)
        GROUP BY c.pipeline_name
    """)
    print(f"  Pipeline distribution:")
    for p in pipelines:
        print(f"    {p.get('pipeline_name', '?')}: {p.get('doc_count', 0):,} docs")

    # Count
    cmd_count(container_name)
    print(f"\n  Verification complete.")


def cmd_stats():
    print("\n  Embedding stats across all containers:\n")
    print(f"  {'Container':<25} {'Total':>10} {'Embedded':>10} {'%':>7}")
    print(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*7}")
    for name in CONTAINERS:
        try:
            total, embedded = cmd_count.__wrapped__(name) if hasattr(cmd_count, '__wrapped__') else _count_raw(name)
        except Exception as e:
            print(f"  {name:<25} {'error':>10} {str(e)[:20]:>10}")
    print()


def _count_raw(name):
    c = get_container(name)
    total = list(c.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))[0]
    embedded = list(c.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))[0]
    pct = (embedded / total * 100) if total > 0 else 0
    print(f"  {name:<25} {total:>10,} {embedded:>10,} {pct:>6.1f}%")
    return total, embedded


def cmd_query(container_name, sql):
    results = run_query(container_name, sql)
    print(f"\n  {len(results)} results:\n")
    for r in results:
        print(json.dumps(r, indent=2, default=str))


def interactive():
    print("\n  OmniVec CosmosDB Query Tool")
    print("  ===========================\n")
    print("  Commands:")
    print("    1) stats    - Embedding stats for all containers")
    print("    2) sample   - Show sample embedded docs")
    print("    3) count    - Count embedded vs total")
    print("    4) verify   - Verify embeddings are real")
    print("    5) query    - Run custom SQL")
    print("    q) quit\n")

    while True:
        choice = input("  > ").strip().lower()
        if choice in ('q', 'quit', 'exit'):
            break
        elif choice in ('1', 'stats'):
            for name in CONTAINERS:
                try:
                    _count_raw(name)
                except Exception as e:
                    print(f"  {name:<25} error: {e}")
        elif choice in ('2', 'sample'):
            name = input("  container name: ").strip() or "bge-small-test-100k"
            cmd_sample(name)
        elif choice in ('3', 'count'):
            name = input("  container name: ").strip() or "bge-small-test-100k"
            cmd_count(name)
        elif choice in ('4', 'verify'):
            name = input("  container name: ").strip() or "bge-small-test-100k"
            cmd_verify(name)
        elif choice in ('5', 'query'):
            name = input("  container name: ").strip() or "bge-small-test-100k"
            sql = input("  SQL: ").strip()
            if sql:
                cmd_query(name, sql)
        else:
            print("  Unknown command. Try 1-5 or q.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        interactive()
    elif sys.argv[1] == "stats":
        for name in CONTAINERS:
            try:
                _count_raw(name)
            except Exception as e:
                print(f"  {name:<25} error: {e}")
    elif sys.argv[1] == "sample":
        cmd_sample(sys.argv[2] if len(sys.argv) > 2 else "bge-small-test-100k")
    elif sys.argv[1] == "count":
        cmd_count(sys.argv[2] if len(sys.argv) > 2 else "bge-small-test-100k")
    elif sys.argv[1] == "verify":
        cmd_verify(sys.argv[2] if len(sys.argv) > 2 else "bge-small-test-100k")
    elif sys.argv[1] == "query":
        if len(sys.argv) < 4:
            print("Usage: query_cosmos.py query <container> <sql>")
        else:
            cmd_query(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        print(__doc__)
