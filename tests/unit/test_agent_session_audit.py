"""Session-store + audit-writer tests."""
from __future__ import annotations

import pytest


@pytest.fixture
def store(agent_app):
    from agent.session_store import get_session_store
    return get_session_store()


@pytest.fixture
def audit(agent_app):
    from agent.audit import get_audit_writer
    return get_audit_writer()


class TestSessionStore:
    @pytest.mark.asyncio
    async def test_create_get_list(self, store):
        s = await store.create_session("alice", title="diag")
        assert s["id"] and s["user_id"] == "alice"
        got = await store.get("alice", s["id"])
        assert got["id"] == s["id"]
        rows = await store.list_for_user("alice")
        assert any(r["id"] == s["id"] for r in rows)
        assert rows[0]["message_count"] == 0

    @pytest.mark.asyncio
    async def test_append_messages_and_delete(self, store):
        s = await store.create_session("bob")
        await store.append_message("bob", s["id"], {"role": "user", "content": "hi"})
        await store.append_message("bob", s["id"], {"role": "assistant", "content": "hello"})
        doc = await store.get("bob", s["id"])
        assert [m["role"] for m in doc["messages"]] == ["user", "assistant"]
        rows = await store.list_for_user("bob")
        assert rows[0]["message_count"] == 2
        assert await store.delete("bob", s["id"]) is True
        assert await store.get("bob", s["id"]) is None

    @pytest.mark.asyncio
    async def test_users_are_isolated(self, store):
        await store.create_session("alice")
        await store.create_session("bob")
        a = await store.list_for_user("alice")
        b = await store.list_for_user("bob")
        assert len(a) == 1 and len(b) == 1
        assert a[0]["id"] != b[0]["id"]


class TestAuditWriter:
    @pytest.mark.asyncio
    async def test_record_shape(self, audit):
        doc = await audit.record(
            session_id="s1", user="alice", role="reader",
            tool_name="list_pipelines", args={"x": 1}, result_summary="ok",
        )
        assert doc["doc_type"] == "agent_audit"
        for k in ("id", "session_id", "user_id", "role", "tool_name", "args",
                  "result_summary", "ts", "reasoning_trace_snippet"):
            assert k in doc

    @pytest.mark.asyncio
    async def test_record_filterable_by_session(self, audit):
        await audit.record(session_id="s1", user="u", role="reader", tool_name="t1", args={}, result_summary="")
        await audit.record(session_id="s2", user="u", role="reader", tool_name="t2", args={}, result_summary="")
        s1 = await audit.list(session_id="s1")
        assert {e["tool_name"] for e in s1} == {"t1"}
