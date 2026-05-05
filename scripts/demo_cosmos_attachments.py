"""
End-to-end demo for the CosmosDB attachment-source mode.

Three modes:

  --dry-run (default)
        Builds a realistic Cosmos document with an `attachments` array, runs
        the same filter/extract logic the .NET watcher uses, and prints the
        EmbeddingMessages that will be emitted. No network calls. Useful for
        validating filter combinations without touching a live cluster.

  --seed
        --dry-run plus: uploads sample PDFs to Azure Blob Storage and inserts
        the document into a Cosmos container so the live changefeed will
        process it on next sync. Requires ``current-values.yaml`` and an
        identity with Storage Blob Data Contributor + Cosmos DB Data
        Contributor on the target account.

  --full
        --seed plus: creates an OmniVec source/destination/pipeline via the
        API, kicks the source sync, polls the destination for embeddings.
        Requires the OmniVec API to be reachable (kubectl port-forward) and
        the omnivec-changefeed image to include attachment-source support.

Examples (PowerShell):
    python scripts/demo_cosmos_attachments.py --dry-run
    python scripts/demo_cosmos_attachments.py --seed --pdfs scripts/samples/blob-pdf
    python scripts/demo_cosmos_attachments.py --full  --api-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Reuse the production helpers — same parsing / filtering / SSRF guard the
# .NET watcher applies (parity is covered by tests/api/test_cosmos_attachments).
from api.connectors.cosmosdb_connector import _extract_attachments  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Sample document
# ──────────────────────────────────────────────────────────────────────────────
def build_sample_doc(account_url: str, container: str, pdf_names: List[str]) -> Dict[str, Any]:
    """Build a Cosmos document where the user filed a case with a few attached
    files. Mixing PDFs and one DOCX exercises the file-type filter."""
    attachments = []
    for name in pdf_names:
        attachments.append({
            "name": name,
            "url": f"{account_url.rstrip('/')}/{container}/{name}",
            "contentType": "application/pdf",
        })
    # Distractor — should be filtered out by attachment_file_types=pdf
    attachments.append({
        "name": "case-notes.docx",
        "url": f"{account_url.rstrip('/')}/{container}/case-notes.docx",
        "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    })
    return {
        "id": "case-2026-001",
        "category": "audit",
        "title": "Q3 mystery audit – evidence package",
        "summary": "Three audit reports plus an internal notes file.",
        "attachments": attachments,
    }


def attachment_source_config(account_url: str, container: str) -> Dict[str, Any]:
    """OmniVec source.config — drop-in for ``omnivec source create``."""
    return {
        "endpoint": "<set when seeding>",
        "database": "demo_attachments",
        "container": "cases",
        "attachments_field": "attachments",
        "attachment_url_field": "url",
        "attachment_name_field": "name",
        "attachment_content_type_field": "contentType",
        "attachment_file_types": ["pdf"],          # only PDFs
        "attachment_name_regex": r".*",              # any name
        "account_url": account_url,                # SSRF pin: only this account
        "container": container,                    # fallback for relative URLs
    }


# ──────────────────────────────────────────────────────────────────────────────
# Dry-run output
# ──────────────────────────────────────────────────────────────────────────────
def dry_run(account_url: str, blob_container: str, pdf_names: List[str]) -> int:
    cfg = attachment_source_config(account_url, blob_container)
    doc = build_sample_doc(account_url, blob_container, pdf_names)

    print("─" * 72)
    print("Source.config:")
    print(json.dumps(cfg, indent=2))
    print("─" * 72)
    print(f"Cosmos doc ({doc['id']}) attachments:")
    for a in doc["attachments"]:
        print(f"  • {a['name']:30s}  {a['contentType']}")
    print("─" * 72)

    matches = _extract_attachments(doc, cfg)
    print(f"Filter result: {len(matches)} attachment(s) match (expect {len(pdf_names)})")
    print("─" * 72)
    print("EmbeddingMessages the .NET watcher will publish (one per match):\n")
    for m in matches:
        msg = {
            "source_ref": f"{doc['id']}::{m['name']}",
            "content_type": "blob_ref",
            "content": "",
            "blob_account_url": m["blob_account_url"],
            "blob_container": m["blob_container"],
            "blob_name": m["blob_name"],
        }
        print(json.dumps(msg, indent=2))
        print()

    distractors = [a["name"] for a in doc["attachments"]
                   if a["name"] not in {m["name"] for m in matches}]
    if distractors:
        print(f"Filtered out (file-type mismatch): {', '.join(distractors)}")

    expected = len(pdf_names)
    if len(matches) != expected:
        print(f"\nFAIL: expected {expected} matches, got {len(matches)}")
        return 1
    print("\nOK — dry-run extraction matches expected filter outcome.")
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# --seed: upload PDFs + insert Cosmos doc
# ──────────────────────────────────────────────────────────────────────────────
def load_cluster_config() -> Dict[str, str]:
    """Pull endpoints from current-values.yaml (deployed-cluster snapshot)."""
    import yaml
    with open(ROOT / "current-values.yaml", "r", encoding="utf-8") as f:
        v = yaml.safe_load(f)
    return {
        "cosmos_endpoint": v["azure"]["cosmos"]["endpoint"],
        "blob_endpoint": v["azure"]["storage"]["blobEndpoint"],
        "client_id": v["azure"]["workloadIdentity"]["clientId"],
        "admin_token": v["api"]["adminToken"],
    }


def seed(account_url: str, blob_container: str, pdf_dir: Path,
         cosmos_endpoint: str) -> Dict[str, Any]:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from azure.cosmos import CosmosClient, PartitionKey

    cred = DefaultAzureCredential()

    # 1) Upload PDFs
    print(f"\n[seed] Uploading PDFs from {pdf_dir} → {account_url}/{blob_container}")
    blob_svc = BlobServiceClient(account_url=account_url, credential=cred)
    try:
        blob_svc.create_container(blob_container)
    except Exception:
        pass  # already exists

    pdf_paths = sorted(pdf_dir.glob("*.pdf"))[:3]  # first 3 PDFs for the demo
    if not pdf_paths:
        raise SystemExit(f"No PDFs found in {pdf_dir}")
    pdf_names = []
    for p in pdf_paths:
        bc = blob_svc.get_blob_client(container=blob_container, blob=p.name)
        with open(p, "rb") as fh:
            bc.upload_blob(fh, overwrite=True, metadata={"demo": "attachment-source"})
        pdf_names.append(p.name)
        print(f"  ↑ {p.name}")

    # 2) Insert Cosmos doc
    print(f"\n[seed] Inserting Cosmos doc into demo_attachments/cases @ {cosmos_endpoint}")
    cosmos = CosmosClient(cosmos_endpoint, credential=cred)
    db = cosmos.create_database_if_not_exists("demo_attachments")
    cont = db.create_container_if_not_exists(
        id="cases",
        partition_key=PartitionKey(path="/category"),
        offer_throughput=400,
    )
    doc = build_sample_doc(account_url, blob_container, pdf_names)
    cont.upsert_item(doc)
    print(f"  ↑ doc {doc['id']} (category={doc['category']}, {len(doc['attachments'])} attachments)")
    return {"doc": doc, "pdf_names": pdf_names}


# ──────────────────────────────────────────────────────────────────────────────
# --full: also create OmniVec source/dest/pipeline + poll embeddings
# ──────────────────────────────────────────────────────────────────────────────
def call_api(api_url: str, token: str, method: str, path: str, body=None) -> Any:
    import requests
    r = requests.request(
        method, api_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps(body) if body is not None else None,
        timeout=60,
    )
    if r.status_code >= 300:
        raise SystemExit(f"API {method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def full(api_url: str, token: str, account_url: str, blob_container: str,
         cosmos_endpoint: str, doc_id: str) -> int:
    print(f"\n[full] Creating OmniVec source via {api_url}")
    src_body = {
        "name": "demo-cosmos-attachments",
        "type": "cosmosdb",
        "config": {
            "endpoint": cosmos_endpoint,
            "database": "demo_attachments",
            "container": "cases",
            "attachments_field": "attachments",
            "attachment_file_types": ["pdf"],
            "account_url": account_url,
            "container": blob_container,
            "auth_type": "managed-identity",
        },
    }
    src = call_api(api_url, token, "POST", "/api/sources", src_body)
    src_id = src.get("source", src).get("id")
    print(f"  ↑ source {src_id}")

    print("\n[full] (Pipeline + destination creation skipped — already covered by e2e-blob-demo.ps1.)")
    print("       Trigger sync and poll your existing destination cosmos container.")
    call_api(api_url, token, "POST", f"/api/sources/{src_id}/sync", {})
    print(f"  ↻ sync triggered for source {src_id}")

    print("\n[full] Tail the changefeed worker logs to confirm attachment messages:")
    print("       kubectl logs -n omnivec deploy/omnivec-changefeed -f --tail=200 | Select-String attachment")
    return 0


# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="(default) Print emitted messages without touching network.")
    ap.add_argument("--seed", action="store_true",
                    help="Upload sample PDFs + insert Cosmos doc against the live cluster.")
    ap.add_argument("--full", action="store_true",
                    help="--seed + create OmniVec source via API + trigger sync.")
    ap.add_argument("--pdfs", default=str(ROOT / "scripts" / "samples" / "blob-pdf"),
                    help="Directory containing sample PDFs (used by --seed/--full).")
    ap.add_argument("--blob-container", default="attachment-demo",
                    help="Blob container to upload sample PDFs into.")
    ap.add_argument("--api-url", default="http://localhost:8000",
                    help="OmniVec API base URL (use kubectl port-forward).")
    args = ap.parse_args()

    if args.full or args.seed:
        cfg = load_cluster_config()
        account_url = cfg["blob_endpoint"].rstrip("/")
        rc = dry_run(account_url, args.blob_container,
                     ["azure-blob-storage.pdf", "azure-cosmos-db.pdf",
                      "azure-kubernetes-service.pdf"])
        if rc != 0:
            return rc
        result = seed(account_url, args.blob_container, Path(args.pdfs),
                      cfg["cosmos_endpoint"])
        if args.full:
            return full(args.api_url, cfg["admin_token"], account_url,
                        args.blob_container, cfg["cosmos_endpoint"],
                        result["doc"]["id"])
        return 0

    # default --dry-run uses a stand-in account URL
    return dry_run(
        account_url="https://acct.blob.core.windows.net",
        blob_container="documents",
        pdf_names=["scorecard-q3.pdf", "scorecard-q4.pdf", "audit-summary.pdf"],
    )


if __name__ == "__main__":
    sys.exit(main())
