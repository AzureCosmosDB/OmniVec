#!/usr/bin/env python3
"""Live throughput monitor for OmniVec embedding pipelines.

Polls CosmosDB every N seconds, counts documents with embeddings,
and computes real-time throughput (docs/sec).

Usage:
  python3 live_throughput.py <container>                  # poll every 2s
  python3 live_throughput.py <container> --interval 5     # poll every 5s
  python3 live_throughput.py <container> --endpoint URL   # custom endpoint

Examples:
  python3 live_throughput.py bge-small-test-100k
  python3 live_throughput.py throughput-test --interval 1
"""

import sys
import time
import argparse
from datetime import datetime

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

DEFAULT_ENDPOINT = "https://cosmosdb-omnivec-test.documents.azure.com:443/"
DEFAULT_DATABASE = "documents"


def get_container(endpoint, database, container_name):
    client = CosmosClient(endpoint, DefaultAzureCredential())
    return client.get_database_client(database).get_container_client(container_name)


def count_embedded(container):
    result = list(container.query_items(
        "SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)",
        enable_cross_partition_query=True
    ))
    return result[0] if result else 0


def count_total(container):
    result = list(container.query_items(
        "SELECT VALUE COUNT(1) FROM c",
        enable_cross_partition_query=True
    ))
    return result[0] if result else 0


def main():
    parser = argparse.ArgumentParser(description="Live throughput monitor")
    parser.add_argument("container", help="CosmosDB container name")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds (default: 2)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="CosmosDB endpoint URL")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Database name")
    args = parser.parse_args()

    container = get_container(args.endpoint, args.database, args.container)

    print(f"\n  Live Throughput Monitor — {args.container}")
    print(f"  Polling every {args.interval}s  |  Ctrl+C to stop\n")

    # Get initial counts
    total = count_total(container)
    prev_embedded = count_embedded(container)
    start_embedded = prev_embedded
    start_time = time.time()

    print(f"  Total docs: {total:,}  |  Already embedded: {prev_embedded:,}  |  Remaining: {total - prev_embedded:,}")
    print(f"  {'─' * 80}")
    print(f"  {'Time':<12} {'Embedded':>10} {'Remaining':>10} {'Δ':>8} {'Rate (d/s)':>12} {'Avg (d/s)':>12} {'ETA':>10}")
    print(f"  {'─' * 80}")

    try:
        while True:
            time.sleep(args.interval)
            now = time.time()
            current = count_embedded(container)
            delta = current - prev_embedded
            elapsed = now - start_time
            total_delta = current - start_embedded

            # Instantaneous rate (over this interval)
            rate = delta / args.interval if args.interval > 0 else 0
            # Average rate since start
            avg_rate = total_delta / elapsed if elapsed > 0 else 0
            # Remaining
            remaining = total - current
            # ETA
            if avg_rate > 0 and remaining > 0:
                eta_secs = remaining / avg_rate
                if eta_secs < 60:
                    eta = f"{eta_secs:.0f}s"
                elif eta_secs < 3600:
                    eta = f"{eta_secs / 60:.1f}m"
                else:
                    eta = f"{eta_secs / 3600:.1f}h"
            elif remaining <= 0:
                eta = "done"
            else:
                eta = "—"

            ts = datetime.now().strftime("%H:%M:%S")
            pct = (current / total * 100) if total > 0 else 0

            # Color: green if processing, yellow if stalled, cyan if done
            if remaining <= 0:
                marker = "✓"
            elif delta > 0:
                marker = "▶"
            else:
                marker = "·"

            print(f"  {ts:<12} {current:>10,} {remaining:>10,} {delta:>+8,} {rate:>11.1f} {avg_rate:>11.1f} {eta:>10}  {marker}")

            prev_embedded = current

            if remaining <= 0 and delta == 0:
                print(f"\n  All {total:,} docs embedded. Total time: {elapsed:.1f}s, avg {avg_rate:.1f} docs/s")
                break

    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        total_delta = prev_embedded - start_embedded
        avg = total_delta / elapsed if elapsed > 0 else 0
        print(f"\n\n  Stopped. Processed {total_delta:,} docs in {elapsed:.1f}s ({avg:.1f} docs/s avg)")


if __name__ == "__main__":
    main()
