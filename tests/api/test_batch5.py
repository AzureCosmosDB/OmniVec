"""Offline tests for batch 5 threat-model follow-ups.

Covers:
  - T-AAD-1: OMNIVEC_AAD_REQUIRE_GROUP=1 rejects unmapped tokens
  - T-CON-3: list_documents logs WARNING on cap truncation
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")

import api  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class AadRequireGroupTests(unittest.TestCase):
    def setUp(self):
        self._orig_admin = api._AAD_ADMIN_GROUP
        self._orig_op = api._AAD_OPERATOR_GROUP
        self._orig_view = api._AAD_VIEWER_GROUP
        self._orig_strict = api._AAD_REQUIRE_GROUP
        api._AAD_ADMIN_GROUP = "grp-admin"
        api._AAD_OPERATOR_GROUP = "grp-operator"
        api._AAD_VIEWER_GROUP = "grp-viewer"

    def tearDown(self):
        api._AAD_ADMIN_GROUP = self._orig_admin
        api._AAD_OPERATOR_GROUP = self._orig_op
        api._AAD_VIEWER_GROUP = self._orig_view
        api._AAD_REQUIRE_GROUP = self._orig_strict

    def test_strict_returns_none_for_unmapped(self):
        api._AAD_REQUIRE_GROUP = True
        self.assertIsNone(api._aad_role_for_claims({"groups": ["random-group"]}))

    def test_strict_still_maps_admin(self):
        api._AAD_REQUIRE_GROUP = True
        self.assertEqual(api._aad_role_for_claims({"groups": ["grp-admin"]}), "admin")

    def test_lax_default_falls_through_to_viewer(self):
        api._AAD_REQUIRE_GROUP = False
        self.assertEqual(api._aad_role_for_claims({"groups": ["random"]}), "viewer")


class CosmosResultCapWarningTests(unittest.TestCase):
    def test_warning_emitted_when_cap_hit(self):
        from connectors import cosmosdb_connector as cdc

        class FakeContainer:
            def query_items(self, query=None, parameters=None, **kw):
                return iter([{"id": f"d{i}"} for i in range(100)])

        class FakeDB:
            def get_container_client(self, *a, **kw):
                return FakeContainer()

        class FakeClient:
            def get_database_client(self, *a, **kw):
                return FakeDB()

        cfg = {"database": "db", "container": "c", "result_cap": 5}

        async def _fake(c):
            return FakeClient()

        with patch.object(cdc, "get_cosmos_client", _fake):
            with self.assertLogs(cdc.logger, level="WARNING") as ctx:
                docs = _run(cdc.list_documents(cfg))
        self.assertEqual(len(docs), 5)
        self.assertTrue(any("truncated" in m for m in ctx.output))

    def test_no_warning_when_under_cap(self):
        from connectors import cosmosdb_connector as cdc

        class FakeContainer:
            def query_items(self, query=None, parameters=None, **kw):
                return iter([{"id": f"d{i}"} for i in range(3)])

        class FakeDB:
            def get_container_client(self, *a, **kw):
                return FakeContainer()

        class FakeClient:
            def get_database_client(self, *a, **kw):
                return FakeDB()

        cfg = {"database": "db", "container": "c", "result_cap": 5}

        async def _fake(c):
            return FakeClient()

        # Under cap should not warn — assertNoLogs is 3.10+; just check messages.
        cdc.logger.setLevel(logging.WARNING)
        captured: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        h = _Handler()
        cdc.logger.addHandler(h)
        try:
            with patch.object(cdc, "get_cosmos_client", _fake):
                docs = _run(cdc.list_documents(cfg))
        finally:
            cdc.logger.removeHandler(h)
        self.assertEqual(len(docs), 3)
        self.assertFalse(any("truncated" in m for m in captured))


if __name__ == "__main__":
    unittest.main(verbosity=2)
