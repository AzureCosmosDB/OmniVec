"""Offline tests for audit-log middleware + per-token last-used tracking
(threat-model item T-API-1).

Validates:
  * Mutating /api/* requests produce one audit_log Cosmos doc with the
    expected actor/method/path/status fields.
  * GET requests are NOT audited.
  * Internal cluster traffic (request.state.internal=True) is NOT audited.
  * Auth-skip prefixes (/api/health, /api/metrics, /api/auth/login) are
    NOT audited even on POST.
  * Persisted Cosmos tokens get last_used_at touched (debounced).
  * /api/audit-log endpoint applies actor / path_prefix / method / since
    filters and gates on admin role.

No pytest required (mirrors test_admin_endpoints.py style). Run via:
    python tests/api/test_audit_log.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from typing import Any, Dict, List

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")

import api  # noqa: E402


class FakeStore:
    """Minimal in-memory stand-in for the Cosmos store wrapper."""

    def __init__(self):
        self.docs: Dict[tuple, Dict[str, Any]] = {}

    def upsert(self, doc):
        self.docs[(doc["id"], doc.get("doc_type", ""))] = dict(doc)

    def get(self, doc_id, doc_type):
        return self.docs.get((doc_id, doc_type))

    def query(self, sql, parameters=None, partition_key=None):
        # Tests only use it for audit_log listing — return all matching docs.
        params = {p["name"]: p["value"] for p in (parameters or [])}
        out: List[Dict[str, Any]] = []
        for (_id, dt), doc in self.docs.items():
            if partition_key and dt != partition_key:
                continue
            if "audit_log" in sql and dt != "audit_log":
                continue
            if "@since" in sql and doc.get("ts", "") < params.get("@since", ""):
                continue
            if "@method" in sql and doc.get("method", "").upper() != params["@method"]:
                continue
            out.append(doc)
        out.sort(key=lambda d: d.get("ts", ""), reverse=True)
        if "@limit" in params:
            out = out[: int(params["@limit"])]
        return out


class FakeRequest:
    def __init__(self, method="POST", path="/api/sources", auth=None, internal=False, host_ip="1.2.3.4"):
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": host_ip})()
        self.headers: Dict[str, str] = {}
        self.state = type("S", (), {})()
        if auth is not None:
            self.state.auth = auth
        self.state.internal = internal


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class AuditMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        api.get_store = lambda: self.store
        # Reset debounce cache so tests don't bleed into each other.
        api._last_used_seen.clear()

    async def _exercise(self, request, response_status=200):
        async def _next(_req):
            return FakeResponse(response_status)
        await api.audit_middleware(request, _next)
        # Drain any background tasks the middleware scheduled.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _audit_docs(self):
        return [d for (_id, dt), d in self.store.docs.items() if dt == "audit_log"]

    def test_post_creates_audit_record(self):
        req = FakeRequest("POST", "/api/sources",
                          auth={"name": "alice", "role": "admin", "id": "tok-1"})
        _run(self._exercise(req, 201))
        docs = self._audit_docs()
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual(d["actor_name"], "alice")
        self.assertEqual(d["actor_role"], "admin")
        self.assertEqual(d["actor_id"], "tok-1")
        self.assertEqual(d["method"], "POST")
        self.assertEqual(d["path"], "/api/sources")
        self.assertEqual(d["status"], 201)
        self.assertEqual(d["ip"], "1.2.3.4")
        self.assertTrue(d["id"].startswith("aud-"))
        self.assertIn("ts", d)

    def test_get_is_not_audited(self):
        req = FakeRequest("GET", "/api/sources",
                          auth={"name": "alice", "role": "admin"})
        _run(self._exercise(req))
        self.assertEqual(self._audit_docs(), [])

    def test_internal_traffic_is_not_audited(self):
        req = FakeRequest("POST", "/api/sources", internal=True)
        _run(self._exercise(req))
        self.assertEqual(self._audit_docs(), [])

    def test_skip_prefixes_not_audited(self):
        for path in ("/api/health/check", "/api/metrics/foo", "/api/auth/login"):
            with self.subTest(path=path):
                self.store.docs.clear()
                req = FakeRequest("POST", path,
                                  auth={"name": "alice", "role": "admin"})
                _run(self._exercise(req))
                self.assertEqual(self._audit_docs(), [])

    def test_anonymous_actor_when_no_auth(self):
        # Edge case — auth middleware would normally 401 first, but if a
        # handler short-circuits we still want a record with anonymous actor.
        req = FakeRequest("DELETE", "/api/sources/abc")
        _run(self._exercise(req, 401))
        docs = self._audit_docs()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["actor_name"], "anonymous")
        self.assertEqual(docs[0]["actor_role"], "none")
        self.assertEqual(docs[0]["status"], 401)

    def test_query_string_stripped_from_path(self):
        req = FakeRequest("PUT", "/api/sources/s1?force=true",
                          auth={"name": "alice", "role": "admin"})
        _run(self._exercise(req))
        docs = self._audit_docs()
        self.assertEqual(docs[0]["path"], "/api/sources/s1")

    def test_audit_write_failure_does_not_break_request(self):
        # Simulate Cosmos outage. The middleware must still return cleanly.
        def _boom(_doc):
            raise RuntimeError("cosmos down")
        self.store.upsert = _boom
        req = FakeRequest("POST", "/api/sources",
                          auth={"name": "alice", "role": "admin"})
        _run(self._exercise(req, 200))  # no exception escapes


class TokenLastUsedTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        api.get_store = lambda: self.store
        api._last_used_seen.clear()
        self.store.upsert({
            "id": "tok-1", "doc_type": "auth_token",
            "name": "alice", "role": "admin", "scope": "admin",
            "token_hash": "x",
        })

    def test_first_call_writes_last_used(self):
        _run(api._touch_token_last_used("tok-1"))
        doc = self.store.get("tok-1", "auth_token")
        self.assertIn("last_used_at", doc)
        self.assertEqual(doc.get("use_count"), 1)

    def test_debounce_suppresses_immediate_second_write(self):
        _run(api._touch_token_last_used("tok-1"))
        first_ts = self.store.get("tok-1", "auth_token")["last_used_at"]
        time.sleep(0.01)
        _run(api._touch_token_last_used("tok-1"))
        # Same record — second call should have been suppressed by debounce.
        doc = self.store.get("tok-1", "auth_token")
        self.assertEqual(doc["last_used_at"], first_ts)
        self.assertEqual(doc["use_count"], 1)

    def test_missing_token_doc_is_silently_ignored(self):
        # No write should happen, no exception should bubble up.
        _run(api._touch_token_last_used("does-not-exist"))


class AuditLogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        api.get_store = lambda: self.store
        # Seed several entries.
        for i, (actor, role, method, path, ts) in enumerate([
            ("alice", "admin", "POST", "/api/sources",   "2026-05-01T10:00:00"),
            ("bob",   "user",  "PUT",  "/api/pipelines/p1", "2026-05-01T11:00:00"),
            ("alice", "admin", "DELETE", "/api/sources/s1", "2026-05-02T09:00:00"),
        ]):
            self.store.upsert({
                "id": f"aud-{i}", "doc_type": "audit_log",
                "ts": ts, "actor_name": actor, "actor_role": role,
                "actor_id": f"tok-{actor}", "method": method, "path": path,
                "status": 200, "ip": "1.2.3.4",
            })

    def _request(self, role="admin"):
        req = FakeRequest()
        req.state.auth = {"name": "tester", "role": role}
        return req

    def test_admin_required(self):
        req = self._request(role="user")
        with self.assertRaises(api.HTTPException) as ctx:
            _run(api.list_audit_log(req))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_returns_all_newest_first(self):
        out = _run(api.list_audit_log(self._request()))
        self.assertEqual(out["count"], 3)
        # Newest first
        self.assertEqual(out["entries"][0]["ts"], "2026-05-02T09:00:00")

    def test_actor_filter(self):
        out = _run(api.list_audit_log(self._request(), actor="alice"))
        actors = {e["actor_name"] for e in out["entries"]}
        self.assertEqual(actors, {"alice"})

    def test_path_prefix_filter(self):
        out = _run(api.list_audit_log(self._request(), path_prefix="/api/pipelines"))
        self.assertEqual([e["path"] for e in out["entries"]], ["/api/pipelines/p1"])

    def test_method_filter(self):
        out = _run(api.list_audit_log(self._request(), method="delete"))
        methods = {e["method"] for e in out["entries"]}
        self.assertEqual(methods, {"DELETE"})

    def test_since_filter(self):
        out = _run(api.list_audit_log(self._request(), since="2026-05-02T00:00:00"))
        self.assertEqual(out["count"], 1)

    def test_limit_capped(self):
        out = _run(api.list_audit_log(self._request(), limit=2))
        self.assertEqual(len(out["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
