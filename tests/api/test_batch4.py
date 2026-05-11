"""Offline tests for batch 4 threat-model fixes.

Covers:
  - T-CON-1: parameterized get_document + result_cap
  - T-API-1 AAD: JWT validation, role mapping, disabled fall-through, JWT-shape filter
  - T-VEC-1: DELETE /api/sources/{id}/vectors auth/404/cascade

Run via `python tests/api/test_batch4.py`. Plain unittest, no pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from typing import Any
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")

import api  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeStore:
    def __init__(self):
        self._docs: dict[tuple[str, str], dict] = {}

    def seed(self, docs):
        for d in docs:
            self._docs[(d["doc_type"], d["id"])] = dict(d)

    def list(self, doc_type: str):
        return [dict(v) for k, v in self._docs.items() if k[0] == doc_type]

    def get(self, doc_id: str, doc_type: str):
        d = self._docs.get((doc_type, doc_id))
        return dict(d) if d else None

    def upsert(self, doc):
        self._docs[(doc["doc_type"], doc["id"])] = dict(doc)

    def delete(self, doc_id: str, doc_type: str):
        self._docs.pop((doc_type, doc_id), None)

    def query(self, *a, **kw):
        return []


class FakeRequest:
    def __init__(self, role: str = "admin"):
        self.state = types.SimpleNamespace(auth={"role": role, "name": "tester"})


# ---------------------------------------------------------------------------
# T-CON-1 — Cosmos source connector hardening
# ---------------------------------------------------------------------------

class CosmosConnectorHardeningTests(unittest.TestCase):
    def test_get_document_uses_parameterized_query(self):
        from connectors import cosmosdb_connector as cdc

        captured: dict[str, Any] = {}

        class FakeContainer:
            def query_items(self, query=None, parameters=None, enable_cross_partition_query=None, **kw):
                captured["query"] = query
                captured["parameters"] = parameters
                return iter([{"id": "doc-1", "content": "ok"}])

        class FakeDB:
            def get_container_client(self, *a, **kw):
                return FakeContainer()

        class FakeClient:
            def get_database_client(self, *a, **kw):
                return FakeDB()

        cfg = {"endpoint": "https://x", "database": "db", "container": "c"}

        async def _fake_client(c):
            return FakeClient()

        with patch.object(cdc, "get_cosmos_client", _fake_client):
            doc = _run(cdc.get_document(cfg, "evil'; DROP TABLE c--"))
        # Concatenated content fields are returned as a string.
        self.assertIn("ok", str(doc))
        self.assertNotIn("DROP TABLE", captured["query"])
        self.assertIn("@id", captured["query"])
        self.assertEqual(captured["parameters"][0]["value"], "evil'; DROP TABLE c--")

    def test_list_documents_honors_result_cap(self):
        from connectors import cosmosdb_connector as cdc

        class FakeContainer:
            def query_items(self, query=None, parameters=None, **kw):
                return iter([{"id": f"d{i}"} for i in range(1000)])

        class FakeDB:
            def get_container_client(self, *a, **kw):
                return FakeContainer()

        class FakeClient:
            def get_database_client(self, *a, **kw):
                return FakeDB()

        cfg = {"endpoint": "https://x", "database": "db", "container": "c", "result_cap": 42}

        async def _fake_client(c):
            return FakeClient()

        with patch.object(cdc, "get_cosmos_client", _fake_client):
            docs = _run(cdc.list_documents(cfg))
        self.assertEqual(len(docs), 42)


# ---------------------------------------------------------------------------
# T-API-1 — AAD JWT bearer
# ---------------------------------------------------------------------------

class AadValidationTests(unittest.TestCase):
    def setUp(self):
        # Save & set AAD env so _aad_enabled() returns True
        self._orig_tenant = api._AAD_TENANT_ID
        self._orig_aud = api._AAD_AUDIENCE
        self._orig_admin_grp = api._AAD_ADMIN_GROUP
        self._orig_op_grp = api._AAD_OPERATOR_GROUP
        api._AAD_TENANT_ID = "tid-test"
        api._AAD_AUDIENCE = "aud-test"
        api._AAD_ADMIN_GROUP = "grp-admin"
        api._AAD_OPERATOR_GROUP = "grp-operator"

    def tearDown(self):
        api._AAD_TENANT_ID = self._orig_tenant
        api._AAD_AUDIENCE = self._orig_aud
        api._AAD_ADMIN_GROUP = self._orig_admin_grp
        api._AAD_OPERATOR_GROUP = self._orig_op_grp
        api._aad_jwks_client = None

    def test_role_mapping_admin(self):
        claims = {"groups": ["grp-admin", "grp-operator"], "oid": "u1"}
        self.assertEqual(api._aad_role_for_claims(claims), "admin")

    def test_role_mapping_operator(self):
        claims = {"groups": ["grp-operator"], "oid": "u2"}
        self.assertEqual(api._aad_role_for_claims(claims), "operator")

    def test_role_mapping_default_viewer(self):
        claims = {"groups": [], "oid": "u3"}
        self.assertEqual(api._aad_role_for_claims(claims), "viewer")

    def test_role_mapping_roles_claim_falls_back(self):
        # If groups missing but app-roles claim present, still resolves.
        claims = {"roles": ["grp-admin"], "oid": "u4"}
        self.assertEqual(api._aad_role_for_claims(claims), "admin")

    def test_validate_aad_disabled_when_env_missing(self):
        api._AAD_TENANT_ID = ""
        self.assertFalse(api._aad_enabled())
        self.assertIsNone(api._validate_aad_token("a.b.c"))

    def test_validate_aad_returns_none_on_decode_failure(self):
        # Stub PyJWKClient + jwt.decode so it raises.
        fake_client = types.SimpleNamespace(
            get_signing_key_from_jwt=lambda t: types.SimpleNamespace(key="k")
        )
        api._aad_jwks_client = fake_client

        fake_jwt = types.ModuleType("jwt")

        class _Err(Exception):
            pass

        fake_jwt.InvalidIssuerError = _Err

        def _decode(*a, **kw):
            raise RuntimeError("bad sig")

        fake_jwt.decode = _decode
        with patch.dict(sys.modules, {"jwt": fake_jwt}):
            result = api._validate_aad_token("a.b.c")
        self.assertIsNone(result)

    def test_validate_aad_happy_path_returns_admin(self):
        fake_client = types.SimpleNamespace(
            get_signing_key_from_jwt=lambda t: types.SimpleNamespace(key="k")
        )
        api._aad_jwks_client = fake_client

        fake_jwt = types.ModuleType("jwt")

        class _IssErr(Exception):
            pass

        fake_jwt.InvalidIssuerError = _IssErr

        claims = {
            "oid": "user-oid-1",
            "tid": "tid-test",
            "preferred_username": "alice@example.com",
            "groups": ["grp-admin"],
            "exp": 0, "iat": 0, "iss": "x", "aud": "aud-test",
        }
        fake_jwt.decode = lambda *a, **kw: claims
        with patch.dict(sys.modules, {"jwt": fake_jwt}):
            result = api._validate_aad_token("a.b.c")
        self.assertIsNotNone(result)
        self.assertEqual(result["role"], "admin")
        self.assertEqual(result["auth_method"], "aad")
        self.assertEqual(result["name"], "alice@example.com")

    def test_validate_token_jwt_shape_filter(self):
        # Non-JWT-shape tokens never invoke the AAD path — they go straight
        # to the legacy admin compare. The test-token env from setUpModule
        # will match here.
        with patch.object(api, "_validate_aad_token", lambda t: self.fail("AAD called")):
            result = api._validate_token("test-token")
        self.assertIsNotNone(result)
        self.assertEqual(result["role"], "admin")


# ---------------------------------------------------------------------------
# T-VEC-1 — DELETE /api/sources/{id}/vectors
# ---------------------------------------------------------------------------

class PurgeSourceVectorsTests(unittest.TestCase):
    def setUp(self):
        self._orig_store = api.get_store
        self.store = FakeStore()
        api.get_store = lambda: self.store

    def tearDown(self):
        api.get_store = self._orig_store

    def test_requires_admin_role(self):
        self.store.seed([{"doc_type": "source", "id": "src-1", "name": "s", "type": "blob", "config": {}}])
        with self.assertRaises(api.HTTPException) as ctx:
            _run(api.purge_source_vectors("src-1", FakeRequest(role="viewer"), cascade=False))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_unknown_source_returns_404(self):
        with self.assertRaises(api.HTTPException) as ctx:
            _run(api.purge_source_vectors("missing", FakeRequest(role="admin"), cascade=False))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_calls_connector_per_pipeline(self):
        self.store.seed([
            {"doc_type": "source", "id": "src-1", "name": "s", "type": "blob", "config": {}},
            {"doc_type": "destination", "id": "dst-1", "name": "d", "type": "cosmosdb-vector",
             "config": {"endpoint": "https://x", "database": "db", "container": "c"}},
            {"doc_type": "pipeline", "id": "pip-1", "name": "p",
             "sources": [{"source_id": "src-1"}], "destination_id": "dst-1",
             "docgrok_pipeline": "default", "vector_index_path": "embedding"},
        ])
        calls: list[tuple[str, str]] = []

        async def fake_delete_by_source_id(cfg, source_id):
            calls.append(("delete", source_id))
            return 7

        async def fake_delete_chunks_by_prefix(cfg, prefix):
            calls.append(("legacy", prefix))
            return 3

        # Inject the fakes into the cosmos vector connector module.
        from connectors import cosmosdb_vector_connector as cvc
        with patch.object(cvc, "delete_by_source_id", fake_delete_by_source_id), \
             patch.object(cvc, "delete_chunks_by_prefix", fake_delete_chunks_by_prefix):
            result = _run(api.purge_source_vectors("src-1", FakeRequest(role="admin"), cascade=True))

        self.assertEqual(result["total_deleted"], 7)
        self.assertEqual(result["legacy_deleted"], 3)
        self.assertEqual(calls[0][0], "delete")
        self.assertEqual(calls[1], ("legacy", "pip-1-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
