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
            "get_troubleshooting_runbook",
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
        assert "get_troubleshooting_runbook" in reader


class TestTroubleshootingRunbooks:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("query,code", [
        ("InsufficientVCPUQuota SKUNotAvailable", "azure_quota_or_sku_unavailable"),
        ("pending-upgrade pending-install", "helm_pending_release"),
        ("wsarecv unexpected EOF", "deployment_transport_interruption"),
        ("ACR build import denied", "acr_build_or_import_failure"),
        ("client ID principal ID wrong subscription", "permission_identity_or_scope_mismatch"),
        ("serverless autopilot throughput", "cosmos_serverless_throughput_rejected"),
        ("Cosmos RequestRateTooLarge hot partition", "cosmos_request_throttling"),
        ("UNKNOWN sampled logs omit changefeed", "agent_diagnostic_coverage_gap"),
        ("embedding provider Retry-After OpenAI", "model_provider_throttling_or_auth"),
        ("obsolete chunks source shrink", "chunk_update_cleanup_failure"),
        ("index-level errors wrong semantic result", "search_empty_or_wrong_results"),
        ("browser nonexistent chat option", "e2e_fixture_or_assertion_mismatch"),
    ])
    async def test_expanded_catalog_is_discoverable(self, tools_mod, query, code):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(query=query, limit=4))
        assert code in {match["failure_code"] for match in result["matches"]}

    def test_every_runbook_has_complete_recovery_contract(self, tools_mod):
        from agent.tools.runbooks import RUNBOOKS

        for code, entry in RUNBOOKS.items():
            assert entry["component"], code
            for key in ("symptoms", "authoritative_checks", "likely_causes",
                        "safe_actions", "avoid", "recovery_verification"):
                assert isinstance(entry[key], list) and entry[key], (code, key)
                assert all(isinstance(value, str) and value.strip() for value in entry[key]), (code, key)

    @pytest.mark.asyncio
    async def test_snat_search_requires_metrics_and_approval(self, tools_mod):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(query="SNAT Cosmos TCP timeout", limit=4))
        entry = next(item for item in result["matches"] if item["failure_code"] == "aks_snat_exhaustion")
        assert "SnatConnectionCount" in " ".join(entry["authoritative_checks"])
        assert "approval" in " ".join(entry["safe_actions"])
        assert "maximum autoscaled nodes plus surge" in " ".join(entry["safe_actions"])
        assert "missing metric samples as zero" in " ".join(entry["avoid"])
        assert t.readonly

    @pytest.mark.asyncio
    async def test_cosmos_provisioning_is_not_a_data_role_fix(self, tools_mod):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(failure_code="cosmos_entra_container_provisioning"))
        entry = result["matches"][0]
        assert "management APIs" in " ".join(entry["safe_actions"])
        assert "serverless" in " ".join(entry["safe_actions"])
        assert "broader data roles" in " ".join(entry["avoid"])
        assert "not a clean-install pass" in " ".join(entry["recovery_verification"])

    @pytest.mark.asyncio
    async def test_exact_failure_code_returns_safe_recovery_contract(self, tools_mod):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(failure_code="dead_letter_messages"))
        match = result["matches"][0]
        assert match["component"] == "messaging"
        assert any("Never" in value or "purging" in value for value in match["avoid"])
        assert match["authoritative_checks"]
        assert match["recovery_verification"]

    @pytest.mark.asyncio
    async def test_search_finds_sharepoint_and_identity_failures(self, tools_mod):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(query="SharePoint Graph 403 Sites.Selected", limit=4))
        codes = {match["failure_code"] for match in result["matches"]}
        assert "sharepoint_permission_failure" in codes
        assert all("safe_actions" in match and "avoid" in match for match in result["matches"])

    @pytest.mark.asyncio
    async def test_unknown_code_fails_open_as_unknown_not_fake_advice(self, tools_mod):
        t = tools_mod.get_tool("get_troubleshooting_runbook")
        result = await t.callable(t.params(failure_code="unknown_new_failure"))
        assert result["matches"] == []
        assert "No exact runbook" in result["unknown"]


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
    @pytest.mark.parametrize("tool_name,field,route", [
        ("get_source", "source_id", "sources"),
        ("get_destination", "destination_id", "destinations"),
        ("get_pipeline", "pipeline_id", "pipelines"),
        ("get_pipeline_status", "pipeline_id", "pipelines"),
        ("get_pipeline_metrics", "pipeline_id", "pipelines"),
        ("get_job", "job_id", "jobs"),
    ])
    @pytest.mark.parametrize("identifier", [
        "../settings?token=example#fragment",
        "https://example.invalid/path",
        "//example.invalid/path",
        "..\\settings",
        "%2e%2e%2fsettings",
    ])
    async def test_resource_id_cannot_change_request_path(self, omnivec_api_mod, tools_mod, monkeypatch, tool_name, field, route, identifier):
        fake = FakeClient()
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        monkeypatch.setattr(omnivec_api_mod, "OMNIVEC_API_URL", "http://omnivec-api")
        t = tools_mod.get_tool(tool_name)
        with pytest.raises(ValueError, match="Resource identifiers"):
            await t.callable(t.params(**{field: identifier}))
        assert not fake.requests

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identifier", [".", ".."])
    async def test_relative_resource_id_is_rejected_before_request(self, omnivec_api_mod, tools_mod, monkeypatch, identifier):
        fake = FakeClient()
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        t = tools_mod.get_tool("get_source")
        with pytest.raises(ValueError, match="Resource identifiers"):
            await t.callable(t.params(source_id=identifier))
        assert not fake.requests

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource", ["sources", "destinations", "pipelines", "jobs", "docgrok_pipelines"])
    @pytest.mark.parametrize("identifier", ["", ".", "..", "src?x=1", "src#x", "src/name", "src\\name", "src%2f", "src\n", "src-\u00e9", "a" * 513])
    async def test_shared_resource_reader_rejects_invalid_identifiers(self, omnivec_api_mod, monkeypatch, resource, identifier):
        fake = FakeClient()
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        with pytest.raises(ValueError, match="Resource identifiers"):
            await omnivec_api_mod._get_resource(resource, identifier)
        assert not fake.requests

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identifier", ["src-123", "pip_123.v2", "a" * 512])
    async def test_shared_resource_reader_preserves_valid_identifiers(self, omnivec_api_mod, monkeypatch, identifier):
        fake = FakeClient()
        monkeypatch.setattr(omnivec_api_mod, "_HTTP_CLIENT", fake)
        monkeypatch.setattr(omnivec_api_mod, "OMNIVEC_API_URL", "http://omnivec-api")
        await omnivec_api_mod._get_resource("pipelines", identifier)
        assert fake.requests[0][1] == f"http://omnivec-api/api/pipelines/{identifier}"

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
