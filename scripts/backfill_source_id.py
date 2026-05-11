#!/usr/bin/env python3
"""T-VEC-2 — Backfill ``source_id`` on legacy vector documents.

Vectors written by OmniVec destination writers prior to batch 4 don't carry
the ``source_id`` field. The cascade-purge endpoint
(``DELETE /api/sources/{id}/vectors?cascade=true``) consequently has to fall
back to ``delete_chunks_by_prefix("{pipeline_id}-")``, which is
*pipeline-wide* — it removes vectors from **every** source feeding that
pipeline. That blast radius is acceptable for an emergency purge but isn't
what an operator expects from a per-source delete.

This script runs once per destination to backfill ``source_id`` on every
legacy vector. The strategy:

1. Enumerate every pipeline + each ``PipelineSource.source_id`` it uses.
2. For each (pipeline, destination) pair, look at every vector doc.
3. Derive the source by inspecting ``doc_id``: the destination writers use
   ``{pipeline_id}-{source_id}-{...}`` for ``doc_id_pattern`` defaults.
   When the pipeline only has *one* source, every doc unambiguously maps.
4. Patch the doc in place with the resolved ``source_id``.

Multi-source pipelines without a parseable ``doc_id`` fall through to
``--strategy=skip`` (default, safe) or ``--strategy=primary`` (assumes the
first PipelineSource — only run with `--dry-run` first).

Supports both Cosmos and Postgres destinations.

Usage::

    # Dry run (no writes):
    python scripts/backfill_source_id.py --pipeline pip-1 --dry-run

    # Apply:
    python scripts/backfill_source_id.py --pipeline pip-1

    # All pipelines that touch a given destination:
    python scripts/backfill_source_id.py --destination dst-1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Make api/ importable for the connector + store helpers.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [backfill] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _derive_source_id(doc_id: str, pipeline_id: str, candidates: List[str]) -> Optional[str]:
    """Return the source_id that prefixes ``doc_id`` after the pipeline id.

    The default ``doc_id_pattern`` is ``"{pipeline}-{source}-..."``.
    For a single-source pipeline, just return that source.
    """
    if len(candidates) == 1:
        return candidates[0]
    if not doc_id.startswith(f"{pipeline_id}-"):
        return None
    tail = doc_id[len(pipeline_id) + 1:]
    for sid in candidates:
        if tail.startswith(f"{sid}-") or tail == sid:
            return sid
    return None


async def _backfill_cosmos(cfg: Dict[str, Any], pipeline_id: str, candidates: List[str],
                           dry_run: bool, strategy: str) -> Tuple[int, int]:
    from connectors.cosmosdb_vector_connector import get_cosmos_client  # type: ignore
    client = await get_cosmos_client(cfg)
    db = client.get_database_client(cfg["database"])
    container = db.get_container_client(cfg["container"])

    seen = patched = 0
    query = "SELECT * FROM c WHERE NOT IS_DEFINED(c.source_id) OR c.source_id = ''"
    for item in container.query_items(query, enable_cross_partition_query=True):
        seen += 1
        doc_id = item.get("id", "")
        sid = _derive_source_id(doc_id, pipeline_id, candidates)
        if sid is None and strategy == "primary" and candidates:
            sid = candidates[0]
        if sid is None:
            continue
        item["source_id"] = sid
        if dry_run:
            log.info("[dry] would patch %s -> source_id=%s", doc_id, sid)
        else:
            container.upsert_item(item)
            log.info("patched %s -> source_id=%s", doc_id, sid)
        patched += 1
    return seen, patched


async def _backfill_postgres(cfg: Dict[str, Any], pipeline_id: str, candidates: List[str],
                             dry_run: bool, strategy: str) -> Tuple[int, int]:
    import asyncpg  # type: ignore
    from connectors.security_utils import validate_sql_identifier  # type: ignore

    table = validate_sql_identifier(cfg.get("table", "vectors"))
    id_col = validate_sql_identifier(cfg.get("id_column", "id"))

    conn = await asyncpg.connect(
        host=cfg["host"], port=cfg.get("port", 5432),
        user=cfg["user"], password=cfg.get("password", ""),
        database=cfg["database"], ssl=cfg.get("ssl", "require"),
    )
    try:
        rows = await conn.fetch(
            f"SELECT {id_col} FROM {table} WHERE source_id IS NULL OR source_id = ''"
        )
        seen = len(rows)
        patched = 0
        for r in rows:
            doc_id = str(r[id_col])
            sid = _derive_source_id(doc_id, pipeline_id, candidates)
            if sid is None and strategy == "primary" and candidates:
                sid = candidates[0]
            if sid is None:
                continue
            if dry_run:
                log.info("[dry] would patch %s -> source_id=%s", doc_id, sid)
            else:
                await conn.execute(
                    f"UPDATE {table} SET source_id = $1 WHERE {id_col} = $2",
                    sid, doc_id,
                )
                log.info("patched %s -> source_id=%s", doc_id, sid)
            patched += 1
        return seen, patched
    finally:
        await conn.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", help="Backfill only this pipeline id")
    ap.add_argument("--destination", help="Backfill all pipelines that write to this destination")
    ap.add_argument("--dry-run", action="store_true", help="Don't write; log what would change")
    ap.add_argument("--strategy", choices=("skip", "primary"), default="skip",
                    help="When multi-source pipeline doc_id is ambiguous: skip (safe, default) "
                         "or primary (assume first PipelineSource — review dry-run output first)")
    args = ap.parse_args()

    if not (args.pipeline or args.destination):
        ap.error("--pipeline or --destination is required")

    # Lazy import so the script doesn't need full FastAPI bootstrap.
    from store import get_store  # type: ignore
    store = get_store()

    pipelines = store.query(
        "SELECT * FROM c WHERE c.doc_type = 'pipeline'", partition_key="pipeline"
    )
    selected: List[Dict[str, Any]] = []
    for p in pipelines:
        if args.pipeline and p.get("id") != args.pipeline:
            continue
        if args.destination and p.get("destination_id") != args.destination:
            continue
        selected.append(p)

    if not selected:
        log.error("no pipelines matched")
        return 2

    total_seen = total_patched = 0
    for p in selected:
        dest = store.get(p["destination_id"], "destination")
        if not dest:
            log.warning("pipeline %s: destination %s missing — skipped", p["id"], p["destination_id"])
            continue
        cfg = dest.get("config") or {}
        dtype = (dest.get("type") or "").lower()
        candidates = [s.get("source_id") for s in (p.get("sources") or []) if s.get("source_id")]
        log.info("pipeline=%s destination=%s type=%s sources=%s",
                 p["id"], dest["id"], dtype, candidates)

        try:
            if dtype in ("cosmosdb", "cosmos", "cosmosdb_vector", "cosmosdb-vector"):
                seen, patched = await _backfill_cosmos(cfg, p["id"], candidates, args.dry_run, args.strategy)
            elif dtype in ("postgres", "pgvector", "postgresql"):
                seen, patched = await _backfill_postgres(cfg, p["id"], candidates, args.dry_run, args.strategy)
            else:
                log.warning("unsupported destination type=%s — skipped", dtype)
                continue
        except Exception as e:
            log.exception("pipeline %s backfill failed: %s", p["id"], e)
            continue

        log.info("pipeline=%s seen=%d patched=%d", p["id"], seen, patched)
        total_seen += seen
        total_patched += patched

    log.info("DONE  total seen=%d patched=%d (dry_run=%s)", total_seen, total_patched, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
