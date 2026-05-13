"""Tests for Phase 2 — mutating tools + approval flow.

Two surfaces under test:

1. Tool registry: mutating tools registered with ``readonly=False, role='admin'``
   and visible only to admin role.
2. Agent loop approval gate: when an admin proposes a mutating tool, the loop
   parks a ``PendingApproval`` and emits ``approval_required`` instead of
   executing. ``resume_after_approval`` then either runs the tool (approve)
   or feeds a denial back to the LLM (deny).
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest


@pytest.fixture
def agent_loop(agent_app):
    return sys.modules["agent.agent_loop"]


@pytest.fixture
def approvals_mod(agent_app):
    return sys.modules["agent.approvals"]


@pytest.fixture
def audit_mod(agent_app):
    return sys.modules["agent.audit"]


@pytest.fixture
def tools_mod(agent_app):
    return sys.modules["agent.tools"]


@pytest.fixture
def mutations_mod(agent_app):
    return sys.modules["agent.tools.mutations"]


def _llm_returning(seq):
    from agent.llm import LLMResponse
    state = {"i": 0}

    async def fn(messages, tools, model_id):
        i = state["i"]
        state["i"] += 1
        if i >= len(seq):
            return LLMResponse(content="exhausted", finish_reason="stop")
        s = seq[i]
        return LLMResponse(
            content=s.get("content", ""),
            tool_calls=s.get("tool_calls", []),
            finish_reason=s.get("finish_reason", "stop"),
        )
    return fn


async def _drain(queue):
    out = []
    while True:
        e = await queue.get()
        if e is None:
            return out
        out.append(e)


# ---------------------------------------------------------------------------
# Registry surface — mutating tools are admin-only.
# ---------------------------------------------------------------------------
class TestMutationRegistry:
    def test_mutating_tools_registered_with_admin_role(self, tools_mod):
        expected = {
            "restart_pod", "scale_deployment",
            "pause_pipeline", "resume_pipeline", "reset_pipeline_offsets",
            "retry_job", "cancel_job",
            "retry_dlq_messages", "purge_dlq",
        }
        for name in expected:
            t = tools_mod.get_tool(name)
            assert t is not None, f"{name} not registered"
            assert t.role == "admin", f"{name} should be admin-only"
            assert t.readonly is False, f"{name} should be readonly=False"

    def test_reader_role_cannot_see_mutations(self, tools_mod):
        names = {t.name for t in tools_mod.list_tools("reader")}
        assert "restart_pod" not in names
        assert "purge_dlq" not in names
        # And reader still sees read-only tools.
        assert "list_pipelines" in names

    def test_admin_sees_mutations(self, tools_mod):
        names = {t.name for t in tools_mod.list_tools("admin")}
        assert "restart_pod" in names
        assert "purge_dlq" in names

    def test_danger_levels(self, mutations_mod):
        assert mutations_mod.danger_level("purge_dlq") == "high"
        assert mutations_mod.danger_level("reset_pipeline_offsets") == "high"
        assert mutations_mod.danger_level("restart_pod") == "low"
        # Unknown -> medium default.
        assert mutations_mod.danger_level("__nope__") == "medium"


# ---------------------------------------------------------------------------
# Approval gate behaviour.
# ---------------------------------------------------------------------------
class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_admin_mutating_call_parks_and_emits_approval_required(
        self, agent_loop, approvals_mod, tools_mod,
    ):
        tc = {"id": "call_xyz",
              "function": {"name": "restart_pod",
                           "arguments": json.dumps({"pod_name": "x"})}}
        llm = _llm_returning([{"tool_calls": [tc]}])
        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q, user_message="restart x", history=[],
            role="admin", model_id=None,
            caller_id="alice", session_id="sess1",
            llm=llm,
        )
        events = await _drain(q)
        kinds = [e["type"] for e in events]
        assert "approval_required" in kinds
        # No execution => no tool_result for the parked call.
        assert "tool_result" not in kinds
        # And no final answer either — the turn ends at the approval card.
        assert "final" not in kinds

        ev = [e for e in events if e["type"] == "approval_required"][0]
        assert ev["call_id"] == "call_xyz"
        assert ev["tool"] == "restart_pod"
        assert ev["danger_level"] == "low"

        pending = await approvals_mod.get_approvals_store().get("sess1", "call_xyz")
        assert pending is not None
        assert pending.tool_name == "restart_pod"
        assert pending.args == {"pod_name": "x"}
        # The parked history must include the assistant turn with tool_calls so
        # resume can append the tool result correctly.
        last = pending.history[-1]
        assert last["role"] == "assistant"
        assert last.get("tool_calls")

    @pytest.mark.asyncio
    async def test_reader_role_blocks_admin_tool_without_parking(
        self, agent_loop, approvals_mod,
    ):
        tc = {"id": "c1",
              "function": {"name": "restart_pod",
                           "arguments": json.dumps({"pod_name": "x"})}}
        llm = _llm_returning([{"tool_calls": [tc]}, {"content": "ok"}])
        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q, user_message="restart", history=[],
            role="reader", model_id=None,
            caller_id="alice", session_id="sess2",
            llm=llm,
        )
        events = await _drain(q)
        # Reader path emits an error tool_result, never an approval card.
        tr = [e for e in events if e["type"] == "tool_result"][0]
        assert "error" in tr["result"]
        assert not any(e["type"] == "approval_required" for e in events)
        assert await approvals_mod.get_approvals_store().get("sess2", "c1") is None

    @pytest.mark.asyncio
    async def test_resume_approve_executes_and_finalizes(
        self, agent_loop, approvals_mod, audit_mod, tools_mod,
    ):
        # Stub the underlying KubeClient.delete_pod so the tool succeeds.
        from agent.tools import k8s as k8s_mod
        called = {}

        async def fake_delete_pod(namespace, name):
            called["ns"] = namespace
            called["pod"] = name
            return {"deleted": True, "namespace": namespace, "pod": name}

        original = k8s_mod._KUBE.delete_pod
        k8s_mod._KUBE.delete_pod = fake_delete_pod
        try:
            # 1. Initial turn parks the approval.
            tc = {"id": "c1",
                  "function": {"name": "restart_pod",
                               "arguments": json.dumps({"pod_name": "p1"})}}
            llm_initial = _llm_returning([{"tool_calls": [tc]}])
            q1: asyncio.Queue = asyncio.Queue()
            await agent_loop.run_agent(
                queue=q1, user_message="restart p1", history=[],
                role="admin", model_id=None,
                caller_id="alice", session_id="s3",
                llm=llm_initial,
            )
            await _drain(q1)
            pending = await approvals_mod.get_approvals_store().pop("s3", "c1")
            assert pending is not None

            # 2. Resume with approve. The LLM produces a final answer next.
            llm_resume = _llm_returning([{"content": "pod p1 restarted"}])
            q2: asyncio.Queue = asyncio.Queue()
            await agent_loop.resume_after_approval(
                queue=q2, pending=pending,
                decision="approve", comment="ok",
                caller_id="alice", role="admin",
                audit=audit_mod.get_audit_writer(),
                llm=llm_resume,
            )
            events = await _drain(q2)
            kinds = [e["type"] for e in events]
            assert "tool_result" in kinds
            assert kinds[-1] == "final"
            tr = [e for e in events if e["type"] == "tool_result"][0]
            assert tr["result"]["deleted"] is True
            assert called == {"ns": "omnivec", "pod": "p1"}

            audit = await audit_mod.get_audit_writer().list()
            assert any(a["tool_name"] == "restart_pod" for a in audit)
        finally:
            k8s_mod._KUBE.delete_pod = original

    @pytest.mark.asyncio
    async def test_resume_deny_feeds_denial_to_llm(
        self, agent_loop, approvals_mod, audit_mod,
    ):
        tc = {"id": "c1",
              "function": {"name": "purge_dlq",
                           "arguments": json.dumps({"queue": "q1", "confirm": True})}}
        llm_initial = _llm_returning([{"tool_calls": [tc]}])
        q1: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q1, user_message="purge it", history=[],
            role="admin", model_id=None,
            caller_id="alice", session_id="s4",
            llm=llm_initial,
        )
        await _drain(q1)
        pending = await approvals_mod.get_approvals_store().pop("s4", "c1")
        assert pending is not None

        # On resume the LLM sees the denial and produces a different answer.
        seen_messages = {}

        async def llm_resume(messages, tools, model_id):
            seen_messages["msgs"] = messages
            from agent.llm import LLMResponse
            return LLMResponse(content="okay, I won't purge.", finish_reason="stop")

        q2: asyncio.Queue = asyncio.Queue()
        await agent_loop.resume_after_approval(
            queue=q2, pending=pending,
            decision="deny", comment="too risky",
            caller_id="alice", role="admin",
            audit=audit_mod.get_audit_writer(),
            llm=llm_resume,
        )
        events = await _drain(q2)
        tr = [e for e in events if e["type"] == "tool_result"][0]
        assert tr["result"] == {"denied": True, "reason": "too risky"}
        assert events[-1]["type"] == "final"
        # The denial was actually visible to the LLM in the resumed message stack.
        last_tool_msg = [m for m in seen_messages["msgs"] if m.get("role") == "tool"][-1]
        assert "denied" in last_tool_msg["content"]

    @pytest.mark.asyncio
    async def test_resume_invalid_decision_emits_error(
        self, agent_loop, approvals_mod,
    ):
        from agent.approvals import PendingApproval
        p = PendingApproval(
            session_id="s5", call_id="c1", user_id="alice", role="admin",
            tool_name="restart_pod", args={"pod_name": "x"},
            danger_level="low", summary="restart_pod(pod_name='x')",
            history=[{"role": "system", "content": "sys"}],
            tool_call={"id": "c1", "function": {"name": "restart_pod", "arguments": "{}"}},
        )
        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.resume_after_approval(
            queue=q, pending=p, decision="maybe", comment="",
            caller_id="alice", role="admin",
        )
        events = await _drain(q)
        assert events[0]["type"] == "error"


# ---------------------------------------------------------------------------
# Approvals store.
# ---------------------------------------------------------------------------
class TestApprovalsStore:
    @pytest.mark.asyncio
    async def test_put_get_pop(self, approvals_mod):
        from agent.approvals import PendingApproval
        store = approvals_mod.get_approvals_store()
        p = PendingApproval(
            session_id="s", call_id="c", user_id="u", role="admin",
            tool_name="restart_pod", args={}, danger_level="low",
            summary="x", history=[], tool_call={},
        )
        await store.put(p)
        got = await store.get("s", "c")
        assert got is p
        popped = await store.pop("s", "c")
        assert popped is p
        assert await store.get("s", "c") is None

    @pytest.mark.asyncio
    async def test_list_for_session(self, approvals_mod):
        from agent.approvals import PendingApproval
        store = approvals_mod.get_approvals_store()
        for cid in ("a", "b"):
            await store.put(PendingApproval(
                session_id="s", call_id=cid, user_id="u", role="admin",
                tool_name="x", args={}, danger_level="low",
                summary="x", history=[], tool_call={},
            ))
        rows = await store.list_for_session("s")
        assert {r.call_id for r in rows} == {"a", "b"}
        assert await store.list_for_session("other") == []
