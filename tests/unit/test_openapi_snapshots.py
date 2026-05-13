"""OpenAPI snapshot regression tests.

For each FastAPI app we snapshot two things:

1. A *deterministic route list* — ``[(method, path, [param_names], [status])]``
   sorted by ``(path, method)``. This is the small, human-reviewable artifact
   that catches route-shape regressions.
2. The full ``app.openapi()`` schema via syrupy — catches schema drift but is
   noisier; rebuild with ``pytest --snapshot-update``.

We also assert a small set of paranoia checks (known route paths must exist)
so a careless ``--snapshot-update`` cannot mask the removal of a critical
route.
"""
from __future__ import annotations

import json

import pytest


def _route_summary(app):
    """Reduce ``app.openapi()`` to a small, deterministic structure."""
    schema = app.openapi()
    out = []
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, op in sorted(methods.items()):
            if method.lower() not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                continue
            params = sorted([p.get("name", "") for p in op.get("parameters", [])])
            statuses = sorted((op.get("responses") or {}).keys())
            out.append((method.lower(), path, params, statuses))
    return out


def _sanitize_schema(schema: dict) -> dict:
    """Strip non-deterministic / churny fields so the full-schema snapshot
    is stable across environments."""
    schema = json.loads(json.dumps(schema))  # deep copy
    info = schema.get("info") or {}
    info.pop("version", None)
    # FastAPI puts a dated description occasionally; keep title only.
    if isinstance(info.get("description"), str) and any(
        m in info["description"] for m in ("202", "2024", "2025", "2026")
    ):
        info["description"] = "<stripped>"
    return schema


# ---------------------------------------------------------------------------
# Paranoia constants — these MUST exist regardless of snapshot state.
# ---------------------------------------------------------------------------
API_REQUIRED_ROUTES = [
    ("get", "/health"),
    ("get", "/api/pipelines"),
    ("post", "/api/pipelines"),
    ("get", "/api/sources"),
    ("post", "/api/sources"),
    ("get", "/api/destinations"),
]

SEARCH_REQUIRED_ROUTES = [
    ("get", "/health"),
    ("get", "/ready"),
    ("get", "/schema"),
    ("post", "/search"),
    ("post", "/search/explain"),
]

DOCGROK_REQUIRED_ROUTES = [
    ("get", "/health"),
    ("post", "/embed"),
    ("post", "/embed/batch"),
]


AGENT_REQUIRED_ROUTES = [
    ("get", "/v1/health"),
    ("get", "/v1/ready"),
    ("get", "/v1/tools"),
    ("post", "/v1/chat"),
    ("get", "/v1/sessions/{user}"),
    ("get", "/v1/sessions/{user}/{session_id}"),
    ("delete", "/v1/sessions/{user}/{session_id}"),
]


# ===========================================================================
# api/api.py
# ===========================================================================
class TestApiOpenAPI:
    def test_route_list_snapshot(self, api_app, snapshot):
        assert _route_summary(api_app) == snapshot

    def test_full_schema_snapshot(self, api_app, snapshot):
        assert _sanitize_schema(api_app.openapi()) == snapshot

    @pytest.mark.parametrize("method,path", API_REQUIRED_ROUTES)
    def test_required_route_exists(self, api_app, method, path):
        present = {(r[0], r[1]) for r in _route_summary(api_app)}
        assert (method, path) in present, f"required route missing: {method.upper()} {path}"

    def test_has_minimum_route_count(self, api_app):
        # We have ~100 routes; anything < 50 is a strong signal something broke.
        assert len(_route_summary(api_app)) >= 50


# ===========================================================================
# search/main.py
# ===========================================================================
class TestSearchOpenAPI:
    def test_route_list_snapshot(self, search_app, snapshot):
        assert _route_summary(search_app) == snapshot

    def test_full_schema_snapshot(self, search_app, snapshot):
        assert _sanitize_schema(search_app.openapi()) == snapshot

    @pytest.mark.parametrize("method,path", SEARCH_REQUIRED_ROUTES)
    def test_required_route_exists(self, search_app, method, path):
        present = {(r[0], r[1]) for r in _route_summary(search_app)}
        assert (method, path) in present


# ===========================================================================
# docgrok/api.py
# ===========================================================================
class TestDocgrokOpenAPI:
    def test_route_list_snapshot(self, docgrok_app, snapshot):
        assert _route_summary(docgrok_app) == snapshot

    def test_full_schema_snapshot(self, docgrok_app, snapshot):
        assert _sanitize_schema(docgrok_app.openapi()) == snapshot

    @pytest.mark.parametrize("method,path", DOCGROK_REQUIRED_ROUTES)
    def test_required_route_exists(self, docgrok_app, method, path):
        present = {(r[0], r[1]) for r in _route_summary(docgrok_app)}
        assert (method, path) in present


# ===========================================================================
# agent/api.py
# ===========================================================================
class TestAgentOpenAPI:
    def test_route_list_snapshot(self, agent_app, snapshot):
        assert _route_summary(agent_app) == snapshot

    def test_full_schema_snapshot(self, agent_app, snapshot):
        assert _sanitize_schema(agent_app.openapi()) == snapshot

    @pytest.mark.parametrize("method,path", AGENT_REQUIRED_ROUTES)
    def test_required_route_exists(self, agent_app, method, path):
        present = {(r[0], r[1]) for r in _route_summary(agent_app)}
        assert (method, path) in present, f"required route missing: {method.upper()} {path}"
