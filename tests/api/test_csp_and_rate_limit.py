"""Offline tests for batch-3 hardening: CSP header + per-token API rate limit.

Mirrors test_admin_endpoints.py / test_audit_log.py style — no pytest required.

Run:
    python tests/api/test_csp_and_rate_limit.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")

import api  # noqa: E402


class FakeRequest:
    def __init__(self, path="/api/sources", method="GET",
                 auth=None, internal=False, client_host="1.2.3.4"):
        self.method = method
        # Starlette URL has .path
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": client_host})() if client_host else None

        class _State:
            pass
        self.state = _State()
        if auth is not None:
            self.state.auth = auth
        if internal:
            self.state.internal = True


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status
        self.headers: dict = {}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class CSPHeaderTests(unittest.TestCase):
    def test_csp_header_present_on_all_responses(self):
        async def call_next(req):
            return FakeResponse(200)
        resp = _run(api.security_headers(FakeRequest(), call_next))
        self.assertIn("Content-Security-Policy", resp.headers)
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'self'", csp)

    def test_csp_overridable_via_env(self):
        try:
            os.environ["OMNIVEC_CSP"] = "default-src 'none'"

            async def call_next(req):
                return FakeResponse(200)
            resp = _run(api.security_headers(FakeRequest(), call_next))
            self.assertEqual(resp.headers["Content-Security-Policy"], "default-src 'none'")
        finally:
            os.environ.pop("OMNIVEC_CSP", None)

    def test_other_security_headers_still_set(self):
        async def call_next(req):
            return FakeResponse(200)
        resp = _run(api.security_headers(FakeRequest(), call_next))
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Permissions-Policy", resp.headers)


class ApiRateLimitTests(unittest.TestCase):
    def setUp(self):
        # Clean state between tests
        api._api_rate_buckets.clear()
        self._prev_limit = api._API_RATE_LIMIT
        self._prev_window = api._API_RATE_WINDOW
        api._API_RATE_LIMIT = 3
        api._API_RATE_WINDOW = 60.0

    def tearDown(self):
        api._API_RATE_LIMIT = self._prev_limit
        api._API_RATE_WINDOW = self._prev_window
        api._api_rate_buckets.clear()

    def _hit(self, req, status=200):
        async def call_next(r):
            return FakeResponse(status)
        return _run(api.api_rate_limit_middleware(req, call_next))

    def test_per_token_isolation(self):
        a = FakeRequest(path="/api/sources", method="GET",
                        auth={"id": "tok-A", "name": "alice", "role": "operator"})
        b = FakeRequest(path="/api/sources", method="GET",
                        auth={"id": "tok-B", "name": "bob", "role": "operator"})
        for _ in range(3):
            self.assertEqual(self._hit(a).status_code, 200)
        # tok-A is now exhausted
        self.assertEqual(self._hit(a).status_code, 429)
        # tok-B has its own bucket
        self.assertEqual(self._hit(b).status_code, 200)

    def test_429_includes_retry_after(self):
        req = FakeRequest(path="/api/sources",
                          auth={"id": "tok-X", "name": "x", "role": "operator"})
        for _ in range(3):
            self._hit(req)
        resp = self._hit(req)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp.headers)

    def test_unauth_keyed_by_ip(self):
        a = FakeRequest(path="/api/sources", client_host="9.9.9.9")
        b = FakeRequest(path="/api/sources", client_host="8.8.8.8")
        for _ in range(3):
            self.assertEqual(self._hit(a).status_code, 200)
        self.assertEqual(self._hit(a).status_code, 429)
        self.assertEqual(self._hit(b).status_code, 200)

    def test_health_metrics_skipped(self):
        # Even with auth absent, repeated /api/health should never 429
        for _ in range(50):
            r = FakeRequest(path="/api/health", client_host="1.1.1.1")
            self.assertEqual(self._hit(r).status_code, 200)
        for _ in range(50):
            r = FakeRequest(path="/api/metrics", client_host="1.1.1.1")
            self.assertEqual(self._hit(r).status_code, 200)

    def test_internal_traffic_skipped(self):
        for _ in range(50):
            r = FakeRequest(path="/api/sources", internal=True,
                            auth={"id": "tok-I", "name": "i", "role": "admin"})
            self.assertEqual(self._hit(r).status_code, 200)

    def test_non_api_paths_skipped(self):
        for _ in range(50):
            r = FakeRequest(path="/static/app.js", client_host="2.2.2.2")
            self.assertEqual(self._hit(r).status_code, 200)

    def test_disabled_when_limit_zero(self):
        api._API_RATE_LIMIT = 0
        for _ in range(100):
            r = FakeRequest(path="/api/sources",
                            auth={"id": "tok-Z", "name": "z", "role": "operator"})
            self.assertEqual(self._hit(r).status_code, 200)


class RateCheckUnitTests(unittest.TestCase):
    def setUp(self):
        api._api_rate_buckets.clear()
        self._prev_limit = api._API_RATE_LIMIT
        self._prev_window = api._API_RATE_WINDOW
        api._API_RATE_LIMIT = 2
        api._API_RATE_WINDOW = 60.0

    def tearDown(self):
        api._API_RATE_LIMIT = self._prev_limit
        api._API_RATE_WINDOW = self._prev_window
        api._api_rate_buckets.clear()

    def test_window_eviction(self):
        key = "tok:windowed"
        # Pre-seed bucket with old timestamps so the limiter evicts them
        import time as _t
        old = _t.monotonic() - 1000.0  # well outside the 60 s window
        api._api_rate_buckets[key] = [old, old, old]
        # First call should evict and accept
        self.assertTrue(api._api_rate_check(key))
        # Bucket should now contain only the new timestamp
        self.assertEqual(len(api._api_rate_buckets[key]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
