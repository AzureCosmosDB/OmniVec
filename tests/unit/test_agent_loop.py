"""Tests for ``agent.agent_loop.run_agent``.

Strategy: feed a deterministic fake LLM into the loop and assert the event
stream + iteration cap + error propagation.
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def agent_loop(agent_app):
    import sys
    return sys.modules["agent.agent_loop"]


@pytest.fixture
def audit_mod(agent_app):
    import sys
    return sys.modules["agent.audit"]


@pytest.fixture
def tools_mod(agent_app):
    import sys
    return sys.modules["agent.tools"]


def _llm_returning(seq):
    """Return an async fn that yields the next LLMResponse on each call."""
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


class TestRunAgent:
    @pytest.mark.asyncio
    async def test_no_tool_calls_emits_token_then_final(self, agent_loop):
        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q, user_message="hi", history=[], role="reader", model_id=None,
            caller_id="u1", session_id="s1", llm=_llm_returning([{"content": "hello"}]),
        )
        events = await _drain(q)
        kinds = [e["type"] for e in events]
        assert kinds == ["token", "final"]
        assert events[-1]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_tool_call_then_final(self, agent_loop, audit_mod, tools_mod):
        # Patch one tool so we don't need a real backend
        called = {}

        async def fake_callable(p, **_):
            called["args"] = p.model_dump()
            return {"sources": [{"id": "s1"}]}

        # Replace the registered tool's callable
        t = tools_mod.get_tool("list_sources")
        original = t.callable
        t.callable = fake_callable
        try:
            tc = {
                "id": "call_1",
                "function": {"name": "list_sources", "arguments": "{}"},
            }
            llm = _llm_returning([
                {"tool_calls": [tc]},
                {"content": "you have one source"},
            ])
            q: asyncio.Queue = asyncio.Queue()
            await agent_loop.run_agent(
                queue=q, user_message="how many sources?", history=[],
                role="reader", model_id=None,
                caller_id="u1", session_id="s1",
                llm=llm, audit=audit_mod.get_audit_writer(),
            )
            events = await _drain(q)
            kinds = [e["type"] for e in events]
            assert "tool_call" in kinds
            assert "tool_result" in kinds
            assert kinds[-1] == "final"
            # Audit was recorded.
            entries = await audit_mod.get_audit_writer().list()
            assert len(entries) == 1
            assert entries[0]["tool_name"] == "list_sources"
        finally:
            t.callable = original

    @pytest.mark.asyncio
    async def test_max_iterations_cap(self, agent_loop, tools_mod):
        # LLM keeps requesting a tool forever -> hits cap.
        async def fake(p, **_):
            return {"x": 1}

        t = tools_mod.get_tool("get_stats")
        original = t.callable
        t.callable = fake
        try:
            tc = {"id": "c", "function": {"name": "get_stats", "arguments": "{}"}}
            seq = [{"tool_calls": [tc]}] * 20  # well past the cap
            q: asyncio.Queue = asyncio.Queue()
            await agent_loop.run_agent(
                queue=q, user_message="loop forever", history=[],
                role="reader", model_id=None, caller_id="u1", session_id="s1",
                llm=_llm_returning(seq), max_iterations=3,
            )
            events = await _drain(q)
            errs = [e for e in events if e["type"] == "error"]
            assert errs and "max_iterations" in errs[-1]["detail"]
        finally:
            t.callable = original

    @pytest.mark.asyncio
    async def test_llm_error_emits_error_event(self, agent_loop):
        async def broken(messages, tools, model_id):
            raise RuntimeError("upstream 500")

        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q, user_message="x", history=[], role="reader", model_id=None,
            caller_id="u1", session_id="s1", llm=broken,
        )
        events = await _drain(q)
        assert events[0]["type"] == "error"
        assert events[0]["stage"] == "llm"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_result(self, agent_loop):
        tc = {"id": "c", "function": {"name": "__nope__", "arguments": "{}"}}
        llm = _llm_returning([
            {"tool_calls": [tc]},
            {"content": "done"},
        ])
        q: asyncio.Queue = asyncio.Queue()
        await agent_loop.run_agent(
            queue=q, user_message="x", history=[], role="reader", model_id=None,
            caller_id="u1", session_id="s1", llm=llm,
        )
        events = await _drain(q)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_results and "error" in tool_results[0]["result"]
