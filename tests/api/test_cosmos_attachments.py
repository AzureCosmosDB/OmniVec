"""Unit tests for CosmosDB attachment-source extraction.

Mirrors the .NET watcher's filter semantics. Live ingestion runs in the
.NET ChangeFeed worker; the Python helper is used by the API layer for
config validation and ad-hoc preview.
"""

import os
import sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from api.connectors.cosmosdb_connector import (
    _extract_attachments,
    _resolve_blob_location,
    _extract_extension,
)


ACCOUNT = "https://acct.blob.core.windows.net"
CONTAINER = "documents"


def _doc(*atts):
    return {"id": "d1", "attachments": list(atts)}


def _att(name="report.pdf", url=None, content_type="application/pdf"):
    if url is None:
        url = f"{ACCOUNT}/{CONTAINER}/{name}"
    return {"name": name, "url": url, "contentType": content_type}


def test_disabled_when_no_attachments_field():
    cfg = {}
    assert _extract_attachments(_doc(_att()), cfg) == []


def test_basic_match():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT}
    out = _extract_attachments(_doc(_att("a.pdf")), cfg)
    assert len(out) == 1
    assert out[0]["blob_name"] == "a.pdf"
    assert out[0]["blob_container"] == CONTAINER
    assert out[0]["blob_account_url"] == ACCOUNT


def test_filter_by_regex():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT,
           "attachment_name_regex": r"^report-"}
    doc = _doc(_att("report-2024.pdf"), _att("invoice.pdf"))
    out = _extract_attachments(doc, cfg)
    assert [a["name"] for a in out] == ["report-2024.pdf"]


def test_filter_by_file_types():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT,
           "attachment_file_types": ["pdf"]}
    doc = _doc(_att("a.pdf"), _att("b.docx", content_type="application/msword"))
    out = _extract_attachments(doc, cfg)
    assert [a["name"] for a in out] == ["a.pdf"]


def test_filter_by_content_types_csv():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT,
           "attachment_content_types": "application/pdf, text/plain"}
    doc = _doc(_att("a.pdf"), _att("b.bin", content_type="application/octet-stream"))
    out = _extract_attachments(doc, cfg)
    assert [a["name"] for a in out] == ["a.pdf"]


def test_relative_url_uses_source_account():
    cfg = {
        "attachments_field": "attachments",
        "account_url": ACCOUNT,
        "container": CONTAINER,
    }
    doc = _doc({"name": "rel.pdf", "url": "rel.pdf", "contentType": "application/pdf"})
    out = _extract_attachments(doc, cfg)
    assert out[0]["blob_account_url"] == ACCOUNT
    assert out[0]["blob_container"] == CONTAINER
    assert out[0]["blob_name"] == "rel.pdf"


def test_relative_url_prefers_attachment_blob_container():
    # When both keys are present, attachment_blob_container wins so the
    # cosmos container value (still in "container") is not used as the blob
    # container. This is the documented escape hatch for sources that need
    # to also pin a default blob container for relative URLs.
    cfg = {
        "attachments_field": "attachments",
        "account_url": ACCOUNT,
        "container": "cases",  # cosmos container — must NOT be used as blob container
        "attachment_blob_container": CONTAINER,
    }
    doc = _doc({"name": "rel.pdf", "url": "rel.pdf", "contentType": "application/pdf"})
    out = _extract_attachments(doc, cfg)
    assert out[0]["blob_container"] == CONTAINER
    assert out[0]["blob_container"] != "cases"


def test_ssrf_rejects_non_blob_host():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT}
    doc = _doc({"name": "x.pdf", "url": "https://evil.example.com/c/x.pdf",
                "contentType": "application/pdf"})
    assert _extract_attachments(doc, cfg) == []


def test_ssrf_rejects_other_account_when_pinned():
    cfg = {"attachments_field": "attachments", "account_url": ACCOUNT, "container": CONTAINER}
    doc = _doc({"name": "x.pdf", "url": "https://other.blob.core.windows.net/c/x.pdf",
                "contentType": "application/pdf"})
    assert _extract_attachments(doc, cfg) == []


def test_extension_handles_query_string():
    assert _extract_extension("https://x/y/z.pdf?sig=abc") == "pdf"
    assert _extract_extension("noext") is None


def test_resolve_blob_decodes_path():
    acct, ctnr, blob = _resolve_blob_location(
        f"{ACCOUNT}/{CONTAINER}/sub/space%20file.pdf", ACCOUNT, CONTAINER)
    assert acct == ACCOUNT
    assert ctnr == CONTAINER
    assert blob == "sub/space file.pdf"


def test_combined_filters_all_must_match():
    cfg = {
        "attachments_field": "attachments",
        "account_url": ACCOUNT,
        "attachment_name_regex": r"\.pdf$",
        "attachment_file_types": ["pdf"],
        "attachment_content_types": ["application/pdf"],
    }
    doc = _doc(
        _att("a.pdf", content_type="application/pdf"),                # match
        _att("b.pdf", content_type="text/plain"),                     # ct fail
        _att("c.txt", content_type="application/pdf"),                # ext fail
    )
    out = _extract_attachments(doc, cfg)
    assert [a["name"] for a in out] == ["a.pdf"]


# ── T-CON-2: SSRF allowlist hardening ─────────────────────────────────

def test_absolute_url_rejected_when_no_account_pin_or_allowlist():
    # Without source_account_url or allowlist, an absolute *.blob.core.windows.net
    # URL must NOT be accepted — defense against SSRF via attacker-crafted
    # attachments pointing at any same-tenant storage account.
    acct, ctnr, blob = _resolve_blob_location(
        f"{ACCOUNT}/{CONTAINER}/x.pdf", source_account_url="", source_container="")
    assert (acct, ctnr, blob) == (None, None, "")


def test_absolute_url_accepted_when_in_allowlist_only():
    # source_account_url is unset but the host appears in the allowlist —
    # accepted because the operator explicitly declared it OK.
    acct, ctnr, blob = _resolve_blob_location(
        f"{ACCOUNT}/{CONTAINER}/x.pdf",
        source_account_url="",
        source_container="",
        allowlist=["acct.blob.core.windows.net"])
    assert acct == ACCOUNT and ctnr == CONTAINER and blob == "x.pdf"


def test_allowlist_rejects_non_listed_host():
    acct, ctnr, blob = _resolve_blob_location(
        "https://other.blob.core.windows.net/c/x.pdf",
        source_account_url="",
        source_container="",
        allowlist=["acct.blob.core.windows.net"])
    assert (acct, ctnr, blob) == (None, None, "")


def test_extract_attachments_honors_allowlist_config():
    cfg = {
        "attachments_field": "attachments",
        "attachment_blob_account_allowlist": [
            "acct.blob.core.windows.net",
            "second.blob.core.windows.net",
        ],
    }
    doc = _doc(
        {"name": "a.pdf", "url": f"{ACCOUNT}/{CONTAINER}/a.pdf", "contentType": "application/pdf"},
        {"name": "b.pdf", "url": "https://second.blob.core.windows.net/c/b.pdf", "contentType": "application/pdf"},
        {"name": "c.pdf", "url": "https://nope.blob.core.windows.net/c/c.pdf", "contentType": "application/pdf"},
    )
    out = _extract_attachments(doc, cfg)
    assert sorted(a["name"] for a in out) == ["a.pdf", "b.pdf"]


# ── T-BLB-1: Blob name sanitization ────────────────────────────────────

@pytest.mark.parametrize("evil_name", [
    "../../etc/passwd",
    "..\\..\\system32\\sam",
    "/abs/path",
    "good/../bad",
    "a//b",                       # empty middle segment
    " leading-space",
    "trailing-space ",
    "ctrl\x01char",
    "",
    "   ",
])
def test_relative_url_rejects_unsafe_blob_names(evil_name):
    acct, ctnr, blob = _resolve_blob_location(
        evil_name, source_account_url=ACCOUNT, source_container=CONTAINER)
    assert (acct, ctnr, blob) == (None, None, ""), f"unexpectedly accepted: {evil_name!r}"


def test_relative_url_accepts_normal_nested_path():
    acct, ctnr, blob = _resolve_blob_location(
        "case-2026-001/exhibit-A.pdf",
        source_account_url=ACCOUNT, source_container=CONTAINER)
    assert acct == ACCOUNT and ctnr == CONTAINER and blob == "case-2026-001/exhibit-A.pdf"


def test_absolute_url_rejects_traversal_in_decoded_path():
    # Percent-encoded "../" survived the prior implementation because
    # unquote() ran *after* the host check; now blocked by _safe_blob_name.
    acct, ctnr, blob = _resolve_blob_location(
        f"{ACCOUNT}/{CONTAINER}/a/..%2F..%2Fsecret.pdf",
        source_account_url=ACCOUNT, source_container=CONTAINER)
    assert (acct, ctnr, blob) == (None, None, "")


def test_extract_attachments_drops_unsafe_relative_url():
    cfg = {
        "attachments_field": "attachments",
        "account_url": ACCOUNT,
        "attachment_blob_container": CONTAINER,
    }
    doc = _doc(
        {"name": "good", "url": "case-1/a.pdf", "contentType": "application/pdf"},
        {"name": "bad",  "url": "../escape.pdf", "contentType": "application/pdf"},
    )
    out = _extract_attachments(doc, cfg)
    assert [a["name"] for a in out] == ["good"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
