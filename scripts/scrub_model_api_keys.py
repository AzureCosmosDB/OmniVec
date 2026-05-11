#!/usr/bin/env python3
"""Scrub legacy ``api_key`` fields from ``docgrok_model`` records in the
OmniVec metadata Cosmos container (T-MET-1).

Background
----------
Earlier OmniVec releases stored AOAI ``api_key`` values directly inside the
``docgrok_model`` document in Cosmos when no Azure Key Vault was configured.
The current API now prefers Key Vault and only falls back to Cosmos. Existing
records may still carry the secret.

This script walks every ``docgrok_model`` record. For each record that has
both ``api_key`` populated AND ``api_key_source != "keyvault"``, it:

  1. (default) Pushes the secret to Key Vault via
     :func:`api.keyvault_client.set_model_api_key`.
  2. Replaces the document with ``api_key`` removed and
     ``api_key_source = "keyvault"``.

If Key Vault is not configured, the script refuses to scrub by default (so
you don't lose the only copy of the credential). Pass ``--force-clear`` to
clear the field anyway when you've already validated AAD / workload-identity
auth works for every model and the keys are no longer needed.

Usage::

    # Dry run — list affected docs, no writes.
    python scripts/scrub_model_api_keys.py --dry-run

    # Migrate to Key Vault (default).
    python scripts/scrub_model_api_keys.py

    # Hard-clear (only after AAD auth confirmed for every model).
    python scripts/scrub_model_api_keys.py --force-clear

Required env vars (same as the API): ``COSMOS_ENDPOINT``, ``COSMOS_KEY`` or
managed identity, ``COSMOS_DATABASE`` (default ``omnivec``),
``COSMOS_METADATA_CONTAINER`` (default ``metadata``), and for Key Vault path
``KEYVAULT_NAME`` or ``KEYVAULT_URI``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger("scrub_model_api_keys")


def _build_cosmos_client():
    """Build a Cosmos container client using the same env vars as the API."""
    from azure.cosmos import CosmosClient  # type: ignore
    from azure.identity import DefaultAzureCredential  # type: ignore

    endpoint = os.environ.get("COSMOS_ENDPOINT") or os.environ.get("COSMOS_URI")
    if not endpoint:
        raise SystemExit("COSMOS_ENDPOINT (or COSMOS_URI) must be set")
    key = os.environ.get("COSMOS_KEY")
    if key:
        client = CosmosClient(endpoint, credential=key)
    else:
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
    db_name = os.environ.get("COSMOS_DATABASE", "omnivec")
    ctr_name = os.environ.get("COSMOS_METADATA_CONTAINER", "metadata")
    return client.get_database_client(db_name).get_container_client(ctr_name)


def _scan(container) -> List[dict]:
    """Return every docgrok_model document. Partition key is ``/doc_type``."""
    query = "SELECT * FROM c WHERE c.doc_type = 'docgrok_model'"
    return list(container.query_items(query=query, partition_key="docgrok_model"))


def _classify(doc: dict) -> str:
    if not doc.get("api_key"):
        return "clean"
    if doc.get("api_key_source") == "keyvault":
        # api_key shouldn't coexist with KV source, but if it does treat as
        # legacy and clean up.
        return "redundant"
    return "legacy"


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Don't write — just list affected docs.")
    p.add_argument(
        "--force-clear",
        action="store_true",
        help="Clear api_key without copying to Key Vault. Use only when AAD auth is confirmed.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    container = _build_cosmos_client()
    docs = _scan(container)
    logger.info("scanned %d docgrok_model records", len(docs))

    legacy: List[Tuple[dict, str]] = []
    for d in docs:
        kind = _classify(d)
        if kind != "clean":
            legacy.append((d, kind))

    if not legacy:
        logger.info("no legacy api_key fields found — nothing to do")
        return 0

    logger.info("found %d records with api_key populated:", len(legacy))
    for d, kind in legacy:
        logger.info("  - id=%s name=%s kind=%s",
                    d.get("id"), d.get("name", ""), kind)

    if args.dry_run:
        return 0

    set_kv = None
    if not args.force_clear:
        try:
            from api.keyvault_client import set_model_api_key as set_kv  # type: ignore
        except Exception as e:
            logger.error("Key Vault client unavailable (%s). Re-run with --force-clear "
                         "if you've already migrated auth to AAD.", e)
            return 2

    migrated, cleared, failed = 0, 0, 0
    for d, _kind in legacy:
        model_id = d["id"]
        api_key_value = d.get("api_key", "")
        try:
            if set_kv is not None and api_key_value:
                if not set_kv(model_id, api_key_value):
                    logger.error("Key Vault write failed for %s — leaving doc untouched", model_id)
                    failed += 1
                    continue
                d["api_key_source"] = "keyvault"
                migrated += 1
            else:
                d.pop("api_key_source", None)
                cleared += 1
            d.pop("api_key", None)
            container.replace_item(item=model_id, body=d)
        except Exception as e:
            logger.exception("failed to update %s: %s", model_id, e)
            failed += 1

    logger.info("done: migrated=%d cleared=%d failed=%d", migrated, cleared, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
