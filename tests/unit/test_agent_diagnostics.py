"""Deterministic diagnostics/recovery regressions. No cluster, Azure or LLM network."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def diag(agent_app):
    from agent.tools import diagnostics
    return diagnostics


@pytest.fixture
def snapshot():
    return {
        "scope": "pip-1", "observed_at": datetime.now(timezone.utc).isoformat(), "unknown": [],
        "cluster": {
            "deployments": {"ok": True, "value": [
                {"name": n, "desired": 1, "ready": 1, "generation": 1, "observed_generation": 1, "flags": {}}
                for n in ("omnivec-api", "omnivec-controller", "docgrok", "omnivec-dotnet-worker",
                          "omnivec-cosmos-changefeed", "omnivec-blob-ingestor")
            ]},
            "pods": {"ok": True, "value": [{"name": "worker-1", "app": "omnivec-dotnet-worker",
                                          "phase": "Running", "ready": True, "restarts": 0}]},
            "signals": [],
        },
        "queues": {"ok": True, "queues": {"embeddings/Subscriptions/worker": {"ok": True, "value": {
            "active_message_count": 0, "dead_letter_message_count": 0,
        }}}},
        "pipelines": [{
            "id": "pip-1", "status": "active", "processing_mode": "queue", "generation": "1",
            "reset_at": None, "destination_id": "dst-1", "docgrok_pipeline": "mdl-1", "model_ids": ["mdl-1"],
            "sources": [{"id": "src-1", "type": "cosmosdb", "enabled": True}],
            "content_strategy": "truncate", "unknown": [], "findings": [],
            "dependencies": [
                {"id": identifier, "kind": kind, "exists": True, "enabled": True,
                 "status": "healthy", "fresh": True, "checks": []}
                for identifier, kind in (("src-1", "sources"), ("dst-1", "destinations"), ("mdl-1", "models"))
            ],
            "stats": {"embedded_count": 10, "source_doc_count": 10, "documents_processed": 10,
                      "jobs": {"pending": 0, "processing": 0, "failed": 0, "completed": 10}},
        }],
    }


def _codes(result):
    return {f["code"] for f in result["findings"]}


def test_active_and_http_success_are_not_processing_health(diag):
    result = diag.evaluate({"scope": "pip-1", "pipelines": [{"id": "pip-1", "status": "active"}]})
    assert result["status"] == "UNKNOWN"
    assert result["processing_verified"] is False


def test_idle_is_not_verified_processing(diag, snapshot):
    result = diag.evaluate(snapshot)
    assert result["status"] == "READY_IDLE"
    assert result["processing_verified"] is False


def test_stopped_workers_with_backlog_offer_bounded_scale(diag, snapshot):
    worker = next(d for d in snapshot["cluster"]["deployments"]["value"] if d["name"] == "omnivec-dotnet-worker")
    worker.update(desired=0, ready=0)
    snapshot["pipelines"][0]["stats"]["jobs"]["pending"] = 8
    result = diag.evaluate(snapshot)
    assert result["status"] == "BLOCKED"
    action = next(f for f in result["findings"] if f["code"] == "workers_stopped")["approved_tool_candidate"]
    assert action == {"tool": "scale_deployment", "args": {"deployment": "omnivec-dotnet-worker", "replicas": 1, "pipeline_id": "pip-1"}}


@pytest.mark.parametrize("change,code", [
    ({"exists": False}, "missing_model"),
    ({"category": "chat"}, "missing_model"),
    ({"status": "unhealthy"}, "dependency_failed"),
    ({"status": "unhealthy", "signals": ["source_permissions"]}, "source_permissions"),
])
def test_model_and_dependency_failures_never_healthy(diag, snapshot, change, code):
    snapshot["pipelines"][0]["dependencies"][-1].update(change)
    result = diag.evaluate(snapshot)
    assert result["status"] == "BLOCKED"
    assert code in _codes(result)


@pytest.mark.parametrize("change", [{"fresh": False}, {"exists": None}, {"status": "unknown"}, {"status": "warning"}])
def test_insufficient_dependency_evidence_is_unknown(diag, snapshot, change):
    snapshot["pipelines"][0]["dependencies"][0].update(change)
    assert diag.evaluate(snapshot)["status"] == "UNKNOWN"


def test_dlq_not_hidden_by_empty_active_queue(diag, snapshot):
    snapshot["queues"]["queues"]["embeddings/Subscriptions/worker"]["value"]["dead_letter_message_count"] = 3
    result = diag.evaluate(snapshot)
    assert result["status"] == "UNHEALTHY"
    assert "dlq" in _codes(result)
    assert "Never purge" in next(f["next_action"] for f in result["findings"] if f["code"] == "dlq")


def test_disabled_blob_consumer(diag, snapshot):
    snapshot["pipelines"][0]["sources"] = [{"id": "src-1", "type": "azure_blob", "triggers": ["event-grid"]}]
    blob = next(d for d in snapshot["cluster"]["deployments"]["value"] if d["name"] == "omnivec-blob-ingestor")
    blob["flags"]["ChangeFeed__BlobEventConsumerEnabled"] = "false"
    assert "blob_consumer_disabled" in _codes(diag.evaluate(snapshot))


@pytest.mark.parametrize("chunk", [
    {"chunk_size": 0, "chunk_overlap": 0, "chunk_unit": "chars"},
    {"chunk_size": 100, "chunk_overlap": 100, "chunk_unit": "chars"},
    {"chunk_size": 100, "chunk_overlap": -1, "chunk_unit": "chars"},
    {"chunk_size": 100, "chunk_overlap": 1, "chunk_unit": "unknown"},
])
def test_bad_chunk_config_blocks(diag, snapshot, chunk):
    snapshot["pipelines"][0].update(content_strategy="chunk", chunk_config=chunk)
    assert "bad_chunk_config" in _codes(diag.evaluate(snapshot))


def test_fresh_progress_with_healthy_dependencies_verifies(diag, snapshot):
    before = copy.deepcopy(snapshot)
    before["pipelines"][0]["stats"]["embedded_count"] = 9
    result = diag.evaluate(snapshot, before)
    assert result["status"] == "HEALTHY"
    assert result["processing_verified"] is True
    assert result["pipelines"][0]["progress"]["embedded_before"] == 9


def test_no_progress_does_not_invent_dead_poller(diag, snapshot):
    snapshot["pipelines"][0]["stats"]["jobs"]["pending"] = 2
    result = diag.evaluate(snapshot, copy.deepcopy(snapshot))
    assert result["status"] == "UNKNOWN"
    f = next(f for f in result["findings"] if f["code"] == "no_progress")
    assert f["confidence"] == "low"
    assert "not proof of a dead poller" in f["next_action"]


@pytest.mark.parametrize("field,value", [("generation", "2"), ("reset_at", "today"), ("docgrok_pipeline", "mdl-new"), ("destination_id", "dst-new")])
def test_reset_or_binding_changes_invalidate_progress(diag, snapshot, field, value):
    before = copy.deepcopy(snapshot)
    before["pipelines"][0]["stats"]["embedded_count"] = 9
    snapshot["pipelines"][0][field] = value
    assert diag.evaluate(snapshot, before)["processing_verified"] is False


def test_destination_regression_cannot_be_green(diag, snapshot):
    before = copy.deepcopy(snapshot)
    before["pipelines"][0]["stats"]["embedded_count"] = 11
    assert diag.evaluate(snapshot, before)["status"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_failed_and_stub_adapters_never_return_zero_health(diag):
    async def failed():
        return {"stub": True, "active_message_count": 0}
    assert not (await diag._observe(failed()))["ok"]
    from agent.tools import cosmos, servicebus
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        await cosmos._COSMOS.count("db", "metadata")
    with pytest.raises(RuntimeError, match="no messages were moved"):
        await servicebus._SB.retry_dlq("", "q", 1)


@pytest.mark.asyncio
async def test_snapshot_correlates_real_api_contract_and_omits_secrets(diag, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    health = {kind: [{"id": identifier, "status": "healthy", "checked_at": now, "checks": []}]
              for identifier, kind in (("src-1", "sources"), ("dst-1", "destinations"), ("mdl-1", "models"))}
    payloads = {
        "/api/pipelines/pip-1": {
            "id": "pip-1", "status": "active", "sources": [{"source_id": "src-1"}],
            "destination_id": "dst-1", "docgrok_pipeline": "trp-1", "generation": "1",
            "stats": {"embedded_count": 2, "source_doc_count": 2},
        },
        "/api/sources/src-1": {"id": "src-1", "type": "cosmosdb", "enabled": True, "config": {"password": "DO-NOT-EXPOSE"}},
        "/api/destinations/dst-1": {"id": "dst-1", "enabled": True, "config": {"connection_string": "DO-NOT-EXPOSE"}},
        "/api/docgrok/pipelines/trp-1": {"steps": [{"model_id": "mdl-1"}], "api_key": "DO-NOT-EXPOSE"},
    }
    calls = []
    async def get(path, params=None):
        calls.append(path)
        return payloads[path]
    monkeypatch.setattr(diag.omnivec_api, "_get", get)
    result = await diag._pipeline_snapshot("pip-1", health, {"ok": True, "value": {
        "models": [{"id": "mdl-1", "model_category": "embedding", "api_key": "DO-NOT-EXPOSE"}],
    }})
    assert result["model_ids"] == ["mdl-1"]
    assert all(d["exists"] for d in result["dependencies"])
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert "/api/models/mdl-1" not in calls  # This GET route does not exist in the actual API.


@pytest.mark.asyncio
async def test_route_404_is_missing_but_transport_failure_is_unknown(diag, monkeypatch):
    async def get(path, params=None):
        if path.startswith("/api/pipelines"):
            return {"id": "pip-1", "sources": [], "destination_id": "dst-1", "docgrok_pipeline": "trp-missing"}
        if path.startswith("/api/docgrok"):
            response = httpx.Response(404, request=httpx.Request("GET", "http://internal"))
            raise httpx.HTTPStatusError("not found", request=response.request, response=response)
        raise httpx.ConnectError("password=DO-NOT-EXPOSE")
    monkeypatch.setattr(diag.omnivec_api, "_get", get)
    result = await diag._pipeline_snapshot("pip-1", {}, {"ok": False})
    assert "missing_model" in {f["code"] for f in result["findings"]}
    assert result["dependencies"][0]["exists"] is None
    assert "DO-NOT-EXPOSE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_error_body_with_http_200_is_unknown(diag, monkeypatch):
    async def get(*args, **kwargs):
        return {"error": "database inaccessible"}
    monkeypatch.setattr(diag.omnivec_api, "_get", get)
    result = await diag._pipeline_snapshot("pip-1", {}, {})
    assert result["unknown"]


def test_kubernetes_scope_enforced(agent_app):
    from agent.tools import k8s, mutations
    with pytest.raises(ValueError, match="OMNIVEC_NAMESPACE"):
        k8s._NS(namespace="other").ns()
    with pytest.raises(ValueError, match="OMNIVEC_NAMESPACE"):
        mutations._ScaleTarget(namespace="other", deployment="omnivec-api", replicas=1).ns()


@pytest.mark.asyncio
async def test_queue_and_subscription_observations(diag, monkeypatch):
    monkeypatch.setenv("AGENT_QUEUE_NAMES", "blob-events")
    monkeypatch.setenv("AGENT_SB_SUBSCRIPTIONS", "embeddings/worker")
    calls = []
    async def queue(fqns, name):
        calls.append(name)
        return {"active_message_count": 1, "dead_letter_message_count": 0}
    async def subscription(fqns, topic, name):
        calls.append(f"{topic}/{name}")
        return {"active_message_count": 9, "dead_letter_message_count": 2}
    monkeypatch.setattr(diag.servicebus._SB, "queue_depth", queue)
    monkeypatch.setattr(diag.servicebus._SB, "subscription_depth", subscription)
    result = await diag._queues_snapshot()
    assert result["ok"]
    assert calls == ["blob-events", "embeddings/worker"]
    assert result["queues"]["embeddings/Subscriptions/worker"]["value"]["dead_letter_message_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action_result,outcome", [
    ({"scaled": True}, "VERIFIED_PROCESSING"), ({"stub": True}, "ACTION_FAILED"),
    ({"success": False}, "ACTION_FAILED"), ({"error": "denied by infrastructure"}, "ACTION_FAILED"),
])
async def test_runtime_executes_then_verifies(diag, snapshot, monkeypatch, action_result, outcome):
    from agent import recovery
    before = copy.deepcopy(snapshot)
    before["pipelines"][0]["stats"]["embedded_count"] = 9
    samples = iter([before, snapshot])
    order = []
    async def collect(scope=None):
        order.append("observe")
        return next(samples)
    async def execute(t, args):
        order.append("execute")
        return action_result
    monkeypatch.setattr(diag, "collect_snapshot", collect)
    monkeypatch.setattr(diag, "SAMPLE_SECONDS", 0)
    state = {}
    result = await recovery.execute_verified(execute, SimpleNamespace(name="scale_deployment"), {"pipeline_id": "pip-1"}, state)
    assert order[:2] == ["observe", "execute"]
    assert result["recovery"]["outcome"] == outcome
    assert state["attempts"] == 1
    if outcome == "VERIFIED_PROCESSING":
        assert order == ["observe", "execute", "observe"]


@pytest.mark.asyncio
async def test_runtime_bounds_verification_when_no_progress(diag, snapshot, monkeypatch):
    from agent import recovery
    snapshot["pipelines"][0]["stats"]["jobs"]["pending"] = 1
    samples = []
    async def collect(scope=None):
        samples.append(scope)
        return copy.deepcopy(snapshot)
    async def execute(t, args):
        return {"scaled": True}
    monkeypatch.setattr(diag, "collect_snapshot", collect)
    monkeypatch.setattr(diag, "SAMPLE_SECONDS", 0)
    result = await recovery.execute_verified(execute, SimpleNamespace(name="scale_deployment"), {}, {})
    assert result["recovery"]["outcome"] == "NOT_VERIFIED"
    assert len(samples) == 1 + recovery.MAX_VERIFICATION_SAMPLES


async def _drain(queue):
    events = []
    while True:
        event = await queue.get()
        if event is None:
            return events
        events.append(event)


def _call(name="resume_pipeline", call_id="c1", args=None):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"pipeline_id": "pip-1"}),
    }}


@pytest.mark.asyncio
async def test_approved_action_cannot_claim_repaired_without_evidence(diag, monkeypatch):
    from agent import agent_loop, approvals
    from agent.llm import LLMResponse
    async def collect(scope=None):
        return {"scope": scope, "pipelines": [], "unknown": ["dependency observations unavailable"]}
    async def execute(t, args):
        return {"success": True}
    async def propose(*args):
        return LLMResponse(tool_calls=[_call()])
    async def lie(*args):
        return LLMResponse(content="Everything is repaired and healthy!")
    monkeypatch.setattr(diag, "collect_snapshot", collect)
    monkeypatch.setattr(diag, "SAMPLE_SECONDS", 0)
    monkeypatch.setattr(agent_loop, "_execute_tool", execute)
    q = asyncio.Queue()
    await agent_loop.run_agent(queue=q, user_message="fix pipeline", history=[], role="admin",
                               model_id=None, caller_id="alice", session_id="s", llm=propose)
    assert any(e["type"] == "approval_required" for e in await _drain(q))
    pending = await approvals.get_approvals_store().pop("s", "c1")
    q = asyncio.Queue()
    await agent_loop.resume_after_approval(queue=q, pending=pending, decision="approve",
                                          caller_id="alice", role="admin", llm=lie)
    events = await _drain(q)
    final = next(e for e in events if e["type"] == "final")
    assert "NOT verified" in final["text"]
    assert "Everything is repaired" not in final["text"]
    assert final["recovery"]["outcome"] == "NOT_VERIFIED"
    assert any(e["type"] == "verification" for e in events)


@pytest.mark.asyncio
async def test_reused_call_id_requires_new_approval_and_parked_history_is_valid(diag):
    from agent import agent_loop, approvals
    from agent.llm import LLMResponse
    async def propose(*args):
        return LLMResponse(tool_calls=[_call(), _call("scale_deployment", "c2", {"deployment": "omnivec-dotnet-worker", "replicas": 1})])
    q = asyncio.Queue()
    await agent_loop.run_agent(queue=q, user_message="repair", history=[], role="admin",
                               model_id=None, caller_id="alice", session_id="s", llm=propose,
                               approved_call_ids={"c1"})
    events = await _drain(q)
    assert any(e["type"] == "approval_required" for e in events)
    pending = await approvals.get_approvals_store().get("s", "c1")
    assert len(pending.history[-1]["tool_calls"]) == 1


@pytest.mark.asyncio
async def test_generic_repair_cannot_select_purge_or_reset(diag):
    from agent import agent_loop
    from agent.llm import LLMResponse
    calls = iter([LLMResponse(tool_calls=[_call("purge_dlq", args={"queue": "blob-events", "confirm": True})]),
                  LLMResponse(content="escalate")])
    async def llm(*args):
        return next(calls)
    q = asyncio.Queue()
    await agent_loop.run_agent(queue=q, user_message="bring back to healthy", history=[], role="admin",
                               model_id=None, caller_id="alice", session_id="s", llm=llm)
    events = await _drain(q)
    assert not any(e["type"] == "approval_required" for e in events)
    assert "explicit user request" in next(e["result"]["error"] for e in events if e["type"] == "tool_result")


@pytest.mark.asyncio
async def test_recovery_chain_action_budget_blocks_endless_restart(diag):
    from agent import agent_loop
    from agent.llm import LLMResponse
    calls = iter([LLMResponse(tool_calls=[_call()]), LLMResponse(content="escalate")])
    async def llm(*args):
        return next(calls)
    q = asyncio.Queue()
    await agent_loop.run_agent(queue=q, user_message="fix", history=[], role="admin",
                               model_id=None, caller_id="alice", session_id="s", llm=llm,
                               recovery_state={"attempts": 2})
    events = await _drain(q)
    assert not any(e["type"] == "approval_required" for e in events)
    assert "budget exhausted" in next(e["result"]["error"] for e in events if e["type"] == "tool_result")


def test_deterministic_endpoint_requires_auth_and_works_without_llm(diag, agent_app, monkeypatch):
    async def snapshot(scope=None):
        return {"scope": scope or "system", "pipelines": [], "unknown": ["offline"]}
    monkeypatch.setattr(diag, "collect_snapshot", snapshot)
    client = TestClient(agent_app)
    assert client.get("/v1/diagnostics/system").status_code == 401
    headers = {"Authorization": "Bearer test-internal-token", "X-Caller-Id": "alice", "X-Caller-Role": "reader"}
    response = client.post("/v1/diagnostics/pipeline", json={"pipeline_id": "pip-1"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "UNKNOWN"
    assert client.post("/v1/diagnostics/pipeline", json={"pipeline_id": "../secrets"}, headers=headers).status_code == 422


@pytest.mark.asyncio
async def test_other_admin_cannot_consume_user_approval(diag, agent_app):
    from agent.approvals import PendingApproval, get_approvals_store
    pending = PendingApproval(session_id="s", call_id="c", user_id="alice", role="admin",
                              tool_name="resume_pipeline", args={"pipeline_id": "pip-1"},
                              danger_level="low", summary="resume")
    await get_approvals_store().put(pending)
    client = TestClient(agent_app)
    headers = {"Authorization": "Bearer test-internal-token", "X-Caller-Id": "bob", "X-Caller-Role": "admin"}
    response = client.post("/v1/chat/approve", json={"session_id": "s", "call_id": "c", "decision": "approve"}, headers=headers)
    assert response.status_code == 403
    assert await get_approvals_store().get("s", "c") is pending


def test_dimension_mismatch_has_concrete_evidence(diag, snapshot):
    snapshot["pipelines"][0]["dependencies"][1]["configured_dimensions"] = 768
    snapshot["pipelines"][0]["dependencies"][2]["embedding_dim"] = 1536
    result = diag.evaluate(snapshot)
    assert result["status"] == "BLOCKED"
    assert "dimension_mismatch" in _codes(result)


def test_health_timestamp_must_be_fresh(diag):
    assert not diag._fresh(None)
    assert not diag._fresh("not a timestamp")
    assert not diag._fresh("2000-01-01T00:00:00Z")
    assert not diag._fresh("2999-01-01T00:00:00Z")
    assert diag._fresh(datetime.now(timezone.utc).isoformat())


def test_tool_results_redact_credentials_but_preserve_model_ids(agent_app):
    from agent.redaction import redact
    result = redact({"model_id": "mdl-ext-1", "config": {"api_key": "SECRET", "connection_string": "SECRET"},
                     "logs": "Authorization: Bearer SECRET https://host/a?sig=SECRET&other=1 AccountKey=SECRET;"})
    assert "SECRET" not in json.dumps(result)
    assert result["model_id"] == "mdl-ext-1"


@pytest.mark.asyncio
async def test_embedding_deployment_is_rejected_before_chat(agent_app, monkeypatch):
    from agent.llm import _LLMBackend
    from agent.tools import omnivec_api
    async def model(_p):
        return {"id": "mdl-1", "model_category": "embedding", "deployment": "text-embedding-3-small"}
    monkeypatch.setattr(omnivec_api, "get_model", model)
    result = await _LLMBackend().chat_completion([], [], "mdl-1")
    assert "not a registered chat model" in result.content


@pytest.mark.asyncio
async def test_scope_coverage_limit_is_explicit(diag, monkeypatch):
    async def cluster():
        return {"unknown": [], "deployments": {"ok": True, "value": []}, "pods": {"ok": True, "value": []}}
    async def queues():
        return {"ok": False}
    async def get(path, params=None):
        if path == "/api/pipelines":
            return {"pipelines": [{"id": f"pip-{n}"} for n in range(20)]}
        return {}
    visited = []
    async def pipeline(pid, health, models):
        visited.append(pid)
        return {"id": pid, "unknown": ["fixture"]}
    monkeypatch.setattr(diag, "_cluster_snapshot", cluster)
    monkeypatch.setattr(diag, "_queues_snapshot", queues)
    monkeypatch.setattr(diag, "_pipeline_snapshot", pipeline)
    monkeypatch.setattr(diag.omnivec_api, "_get", get)
    result = await diag.collect_snapshot()
    assert len(visited) == diag.MAX_PIPELINES
    assert "coverage incomplete" in result["unknown"][0]


@pytest.mark.asyncio
async def test_successful_rollout_without_processing_is_only_idle(diag, snapshot, monkeypatch):
    from agent import recovery
    async def collect(scope=None):
        return copy.deepcopy(snapshot)
    async def execute(t, args):
        return {"scaled": True}
    monkeypatch.setattr(diag, "collect_snapshot", collect)
    monkeypatch.setattr(diag, "SAMPLE_SECONDS", 0)
    state = {}
    result = await recovery.execute_verified(execute, SimpleNamespace(name="scale_deployment"), {}, state)
    assert result["recovery"]["outcome"] == "READY_IDLE"
    text, record = recovery.final_result(state)
    assert "NOT verified" in text
    assert not record["verification"]["processing_verified"]


@pytest.mark.asyncio
async def test_approval_scope_cannot_be_overwritten_or_replayed(diag):
    from agent.approvals import PendingApproval, get_approvals_store
    pending = PendingApproval(session_id="s", call_id="c", user_id="alice", role="admin",
                              tool_name="resume_pipeline", args={"pipeline_id": "pip-1"},
                              danger_level="low", summary="resume")
    store = get_approvals_store()
    await store.put(pending)
    with pytest.raises(ValueError, match="already pending or consumed"):
        await store.put(pending)
    assert await store.pop("s", "c") is pending
    with pytest.raises(ValueError, match="already pending or consumed"):
        await store.put(pending)
    assert await store.pop("s", "c") is None


@pytest.mark.parametrize("identifier", ["..", "../models/mdl-1", "pip-1/reset", "pip-1?reset=true"])
def test_mutation_scope_cannot_escape_pipeline_endpoint(agent_app, identifier):
    from agent.tools.mutations import _PipelineId
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _PipelineId(pipeline_id=identifier)
