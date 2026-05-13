"""Auth + role-filter tests for the agent service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(agent_app):
    return TestClient(agent_app)


def _hdrs(role="reader", caller="alice", token="test-internal-token"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Caller-Id": caller,
        "X-Caller-Role": role,
    }


class TestAuth:
    def test_health_is_public(self, client):
        r = client.get("/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_tools_requires_bearer(self, client):
        r = client.get("/v1/tools")
        assert r.status_code == 401

    def test_tools_requires_correct_bearer(self, client):
        r = client.get("/v1/tools", headers=_hdrs(token="wrong"))
        assert r.status_code == 401

    def test_tools_requires_caller_id(self, client):
        h = _hdrs()
        h.pop("X-Caller-Id")
        r = client.get("/v1/tools", headers=h)
        assert r.status_code == 401

    def test_tools_ok_for_reader(self, client):
        r = client.get("/v1/tools", headers=_hdrs("reader"))
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "reader"
        assert len(data["tools"]) >= 25

    def test_tools_ok_for_admin(self, client):
        r = client.get("/v1/tools", headers=_hdrs("admin"))
        assert r.status_code == 200
        assert r.json()["role"] == "admin"

    def test_phase1_reader_admin_same_list(self, client):
        rdr = client.get("/v1/tools", headers=_hdrs("reader")).json()["tools"]
        adm = client.get("/v1/tools", headers=_hdrs("admin")).json()["tools"]
        # Phase 1: every tool is reader-visible.
        assert sorted(t["name"] for t in rdr) == sorted(t["name"] for t in adm)

    def test_missing_internal_token_env_returns_503(self, agent_app, monkeypatch):
        import os
        monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
        c = TestClient(agent_app)
        r = c.get("/v1/tools", headers=_hdrs(token="anything"))
        assert r.status_code == 503

    def test_unknown_role_defaults_to_reader(self, client):
        r = client.get("/v1/tools", headers=_hdrs("hacker"))
        assert r.status_code == 200
        assert r.json()["role"] == "reader"
