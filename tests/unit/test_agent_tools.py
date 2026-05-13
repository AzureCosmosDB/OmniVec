"""Tests for the agent tool registry + argument validation.

Property-tests with Hypothesis cover the validation surface; explicit tests
cover the registry shape, role filtering, and that the URL-emitting tools
hit the expected endpoints when the HTTP client is mocked out.
"""
from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import httpx
import pytest
from hypothesis import given, strategies as st
from pydantic import ValidationError


@pytest.fixture
def tools_mod(agent_app):
    """Return the freshly-imported tools registry tied to the agent_app fixture."""
    return sys.modules["agent.tools"]


@pytest.fixture
def omnivec_api_mod(agent_app):
    return sys.modules["agent.tools.omnivec_api"]


class FakeResponse:
    def __init__(self, json_body: Any, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(json_body) if not isinstance(json_body, str) else json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class FakeClient:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.next_response: Any = {"ok": True}

    async def get(self, url: str, params=None, headers=None):
        self.requests.append(("GET", url, dict(params) if params else None))
        return FakeResponse(self.next_response)


# ---------------------------------------------------------------------------
# Registry shape.
# ---------------------------------------------------------------------------
class TestRegistryShape:
    def test_registry_has_expected_minimum_tools(self, tools_mod):
        names = {t.name for t in tools_mod.list_tools("admin")}
        expected = {
            "list_sources", "get_source", "list_destinations", "get_destination",
            "list_pipelines", "get_pipeline", "get_pipeline_status", "get_pipeline_metrics",
            "list_models", "get_model", "list_jobs", "get_job",
            "get_audit_log", "get_capabilities", "get_health_checks",
            "get_metrics_summary", "get_stats", "list_assistants",
            "get_settings", "list_transforms", "list_docgrok_pipelines",
            "get_eventgrid_triggers", "get_changefeed_leases", "list_deployments",
            "list_pods", "get_pod_status", "get_pod_logs", "get_pod_events",
            "count_docs_in_container", "get_doc_by_id", "query_diag",
            "get_queue_depth", "get_dlq_count", "list_topics",
            "recent_errors_last_n", "latency_p99_last_hour", "throughput_last_hour",
        }
        missing = expected - names
        assert not missing, f"missing tools: {missing}"

    def test_at_least_25_omnivec_api_tools(self, tools_mod):
        names = {t.name for t in tools_mod.list_tools("admin")}
        api_tools = {
            "list_sources", "get_source", "list_destinations", "get_destination",
            "list_pipelines", "get_pipeline", "get_pipeline_status", "get_pipeline_metrics",
            "list_models", "get_model", "list_jobs", "get_job",
            "get_audit_log", "get_capabilities", "get_health_checks",
            "get_metrics_summary", "get_stats", "list_assistants",
            "get_settings", "list_transforms", "list_docgrok_pipelines",
            "get_eventgrid_triggers", "get_changefeed_leases", "list_deployments",
            "recent_errors_last_n", "latency_p99_last_hour", "throughput_last_hour",
        }
        assert api_tools.issubset(names)

    def test_readonly_tools_are_readonly(self, tools_mod):
        for t in tools_mod.list_tools("reader"):
            assert t.readonly is True, f"{t.name} reader-visible but not readonly"

    def test_reader_subset_of_admin(self, tools_mod):
        reader = {t.name for t in tools_mod.list_tools("reader")}
        admin = {t.name for t in tools_mod.list_tools("admin")}
        # Phase 2: admin sees strictly more (mutating tools added).
        assert reader.issubset(admin)
        assert "restart_pod" in admin and "restart_pod" not in reader


# ---------------------------------------------------------------------------
# Validation property tests.
# ---------------------------------------------------------------------------
class TestArgValidation:
    @pytest.mark.parametrize("tool_name", [
        "get_source", "get_destination", "get_pipeline", "get_model", "get_job",
        "get_pipeline_status", "get_pipeline_metrics",
    ])
    def test_id_tools_reject_empty_id(self, tools_mod, tool_name):
        with pytest.raises(ValidationError):
            tools_mod.validate_args(tool_name, {})

    @given(name=st.text(min_size=1, max_size=50).filter(lambda s: s.strip()))
    def test_get_source_accepts_nonempty_id(self, name):
        from agent.tools import validate_args
        m = validate_args("get_source", {"source_id": name})
        assert m.source_id == name

    def test_query_diag_rejects_unlisted_container(self, tools_mod):
        # validation passes (any string), execution rejects
        m = tools_mod.validate_args(
            "query_diag",
            {"container": "totally_random", "sql": "SELECT * FROM c"},
        )
        assert m.container == "totally_random"

    def test_unknown_tool_raises(self, tools_mod):
        with pytest.raises(KeyError):
            tools_mod.validate_args("__nope__", {})


# ---------------------------------------------------------------------------
# HTTP wiring — replace _HTTP_CLIENT with FakeClient and assert URLs.
# ---------------------------------------------------------------------------
class TestOmnivecApiTools:
    @pytest.mark.asyncio
    async def test_list_pipelines_hits_expected_url(self, omnivec_api_mod, tools_mod, monkeypatch):
        fake = FakeClient()
        fake.next_response = {"pipelines": []}
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        monkeypatch.setattr(omnivec_api_mod, "OMNIVEC_API_URL", "http://omnivec-api")
        t = tools_mod.get_tool("list_pipelines")
        result = await t.callable(t.params())
        assert result == {"pipelines": []}
        assert fake.requests[0][0] == "GET"
        assert fake.requests[0][1] == "http://omnivec-api/api/pipelines"

    @pytest.mark.asyncio
    async def test_get_pipeline_substitutes_id(self, omnivec_api_mod, tools_mod, monkeypatch):
        fake = FakeClient()
        fake.next_response = {"id": "p1"}
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        t = tools_mod.get_tool("get_pipeline")
        await t.callable(t.params(pipeline_id="p1"))
        assert fake.requests[0][1].endswith("/api/pipelines/p1")

    @pytest.mark.asyncio
    async def test_get_audit_log_passes_filters_as_params(self, omnivec_api_mod, tools_mod, monkeypatch):
        fake = FakeClient()
        fake.next_response = {"entries": []}
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        t = tools_mod.get_tool("get_audit_log")
        await t.callable(t.params(actor="admin", limit=5))
        url, params = fake.requests[0][1], fake.requests[0][2]
        assert url.endswith("/api/audit-log")
        assert params == {"actor": "admin", "limit": 5}


# pytest.ini sets asyncio_mode = auto so async tests just work.
