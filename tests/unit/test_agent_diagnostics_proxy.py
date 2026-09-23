"""Exercise the real proxy definitions without starting other control-plane services."""
import ast
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import httpx
from pydantic import BaseModel, Field
import pytest


@pytest.fixture
def proxy(monkeypatch):
    app = FastAPI()
    captures = []
    upstream = {"scope": "system", "status": "UNKNOWN", "pipelines": [], "findings": [], "unknown": ["Missing evidence"]}
    behavior = {"status": 200, "data": upstream, "error": None}

    @app.middleware("http")
    async def caller(request, call_next):
        request.state.auth = {"id": "validated-user", "role": "reader"}
        return await call_next(request)

    async def handler(request):
        captures.append(request)
        if behavior["error"]:
            raise behavior["error"]
        return httpx.Response(behavior["status"], json=behavior["data"])

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    source = (Path(__file__).resolve().parents[2] / "api" / "api.py").read_text(encoding="utf-8")
    names = {"_agent_headers", "AgentDiagnosticRequest", "_agent_diagnostics_proxy",
             "agent_system_diagnostics", "agent_pipeline_diagnostics"}
    nodes = [node for node in ast.parse(source).body if getattr(node, "name", None) in names]
    namespace = {"app": app, "Request": Request, "HTTPException": HTTPException,
                 "BaseModel": BaseModel, "Field": Field, "httpx": httpx,
                 "logger": logging.getLogger(__name__), "_AGENT_URL": "http://agent",
                 "_INTERNAL_API_TOKEN": "test-internal"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<agent-proxy>", "exec"), namespace)
    with TestClient(app) as client:
        yield client, behavior, captures, namespace


def test_system_diagnostics_uses_validated_reader_identity(proxy):
    client, _, captures, _ = proxy
    result = client.get("/api/agent/diagnostics/system", headers={"X-Caller-Role": "admin"})
    assert result.status_code == 200
    assert str(captures[0].url) == "http://agent/v1/diagnostics/system"
    assert captures[0].headers["X-Caller-Role"] == "reader"
    assert captures[0].headers["X-Caller-Id"] == "validated-user"


def test_pipeline_diagnostics_forwards_only_validated_scope(proxy):
    client, behavior, captures, _ = proxy
    behavior["data"]["scope"] = "pip-owned"
    result = client.post("/api/agent/diagnostics/pipeline", json={"pipeline_id": "pip-owned", "role": "admin"})
    assert result.status_code == 200
    assert json.loads(captures[0].content) == {"pipeline_id": "pip-owned"}


@pytest.mark.parametrize("identifier", ["../settings", "pip-one/../two", "", "x" * 161])
def test_invalid_pipeline_identifiers_never_reach_agent(proxy, identifier):
    client, _, captures, _ = proxy
    assert client.post("/api/agent/diagnostics/pipeline", json={"pipeline_id": identifier}).status_code == 422
    assert not captures


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_upstream_failures_are_not_successful_empty_snapshots(proxy, status):
    client, behavior, _, _ = proxy
    behavior.update(status=status, data={"detail": "private upstream detail"})
    result = client.get("/api/agent/diagnostics/system")
    assert result.status_code == status
    assert "private" not in result.text


@pytest.mark.parametrize("data", [[], {}, {"scope": "foreign", "status": "HEALTHY"}])
def test_malformed_or_wrong_scope_is_rejected(proxy, data):
    client, behavior, _, _ = proxy
    behavior["data"] = data
    assert client.get("/api/agent/diagnostics/system").status_code == 502


def test_timeout_and_missing_configuration_are_explicit(proxy):
    client, behavior, captures, namespace = proxy
    behavior["error"] = httpx.ReadTimeout("private transport details")
    result = client.get("/api/agent/diagnostics/system")
    assert result.status_code == 504 and "private" not in result.text
    namespace["_INTERNAL_API_TOKEN"] = ""
    assert client.get("/api/agent/diagnostics/system").status_code == 503
    assert len(captures) == 1
