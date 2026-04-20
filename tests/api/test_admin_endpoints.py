"""Offline tests for admin endpoints that don't require Azure.

Covers:
  - delete_model guard (pipelines, assistants, no-refs)
  - import_bundle registering embedding models with DocGrok (create + skip paths)

Run via `python tests/api/test_admin_endpoints.py` or under the test harness
which also picks up `test-*.sh` and `test-*.ps1`.

No pytest / external test framework required — plain unittest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any

# Make api/ importable and avoid secret-required startup paths.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")

import api  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeHttp:
    """Minimal async httpx-compatible stub; records calls."""

    def __init__(self, registry_ids=(), register_status=201, get_status=200):
        self._registry = list(registry_ids)
        self._register_status = register_status
        self._get_status = get_status
        self.register_calls: list[dict] = []
        self.get_calls: list[str] = []

    async def get(self, url: str):
        self.get_calls.append(url)
        return FakeResponse(self._get_status, {"models": [{"id": x} for x in self._registry]})

    async def post(self, url: str, json: dict | None = None):
        if url.endswith("/admin/models/registry"):
            self.register_calls.append(json or {})
            if json and json.get("id"):
                self._registry.append(json["id"])
            return FakeResponse(self._register_status, {"id": (json or {}).get("id", "")})
        return FakeResponse(404, "unexpected url")

    async def delete(self, url: str):
        return FakeResponse(200, {})


class FakeStore:
    """In-memory doc store mimicking store.list/get/upsert/delete."""

    def __init__(self):
        self._docs: dict[tuple[str, str], dict] = {}  # (doc_type, id) -> doc

    def seed(self, docs):
        for d in docs:
            self._docs[(d["doc_type"], d["id"])] = dict(d)

    def list(self, doc_type: str):
        return [dict(v) for k, v in self._docs.items() if k[0] == doc_type]

    def get(self, doc_id: str, doc_type: str):
        d = self._docs.get((doc_type, doc_id))
        return dict(d) if d else None

    def upsert(self, doc: dict):
        key = (doc["doc_type"], doc["id"])
        self._docs[key] = dict(doc)

    def delete(self, doc_id: str, doc_type: str):
        self._docs.pop((doc_type, doc_id), None)

    def query(self, *a, **kw):
        return []


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# delete_model guard
# ---------------------------------------------------------------------------

class DeleteModelGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig_get_store = api.get_store
        self._orig_http = api.http_client
        self.store = FakeStore()
        api.get_store = lambda: self.store
        api.http_client = FakeHttp()

    def tearDown(self):
        api.get_store = self._orig_get_store
        api.http_client = self._orig_http

    def test_blocks_when_pipeline_uses_model(self):
        self.store.seed([
            {"doc_type": "docgrok_model", "id": "mdl-ext-a", "name": "emb", "model_category": "embedding"},
            {"doc_type": "pipeline", "id": "pip-1", "name": "my-pipe", "docgrok_pipeline": "mdl-ext-a"},
        ])
        with self.assertRaises(api.HTTPException) as ctx:
            _run(api.delete_model("mdl-ext-a"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("pipeline", ctx.exception.detail.lower())
        self.assertIn("my-pipe", ctx.exception.detail)

    def test_blocks_when_assistant_uses_model(self):
        self.store.seed([
            {"doc_type": "docgrok_model", "id": "mdl-ext-b", "name": "chat", "model_category": "chat"},
            {"doc_type": "assistant", "id": "ast-1", "name": "helpful", "model_id": "mdl-ext-b"},
        ])
        with self.assertRaises(api.HTTPException) as ctx:
            _run(api.delete_model("mdl-ext-b"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("assistant", ctx.exception.detail.lower())
        self.assertIn("helpful", ctx.exception.detail)

    def test_proceeds_for_chat_model_with_no_refs(self):
        self.store.seed([
            {"doc_type": "docgrok_model", "id": "mdl-ext-c", "name": "orphan-chat", "model_category": "chat"},
        ])
        # Chat-only path: deletes from Cosmos, no DocGrok call.
        result = _run(api.delete_model("mdl-ext-c"))
        self.assertEqual(result.get("status"), "deleted")
        self.assertIsNone(self.store.get("mdl-ext-c", "docgrok_model"))

    def test_allows_embedding_model_with_no_refs(self):
        """Guard must not fire; we don't test the downstream DocGrok call here."""
        self.store.seed([
            {"doc_type": "docgrok_model", "id": "mdl-ext-d", "name": "orphan-embed", "model_category": "embedding"},
        ])
        # Our FakeHttp.delete returns 200; delete should succeed.
        result = _run(api.delete_model("mdl-ext-d"))
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# import_bundle: DocGrok registration
# ---------------------------------------------------------------------------

class ImportBundleRegistrationTests(unittest.TestCase):
    def setUp(self):
        self._orig_get_store = api.get_store
        self._orig_http = api.http_client
        self.store = FakeStore()
        api.get_store = lambda: self.store
        # Stub keyvault so test doesn't touch Azure.
        import types
        fake_kv = types.ModuleType("keyvault_client")
        fake_kv.set_model_api_key = lambda mid, key: False  # fallback: persist api_key in doc
        fake_kv.get_model_api_key = lambda mid: None
        fake_kv.delete_model_api_key = lambda mid: None
        sys.modules["keyvault_client"] = fake_kv

    def tearDown(self):
        api.get_store = self._orig_get_store
        api.http_client = self._orig_http
        sys.modules.pop("keyvault_client", None)

    def _bundle(self, model_id="mdl-ext-x", category="embedding"):
        return {
            "omnivec_export_version": api._EXPORT_VERSION,
            "resources": {
                "models": [
                    {
                        "id": model_id,
                        "name": "azure-openai-embed",
                        "type": "azure-openai",
                        "endpoint": "https://ex.example.com",
                        "deployment": "text-embedding-3-small",
                        "embedding_dim": 1536,
                        "api_version": "2024-06-01",
                        "model_category": category,
                        "api_key": "sekret-123",
                    }
                ]
            },
        }

    def test_created_model_gets_registered_with_docgrok(self):
        http = FakeHttp(registry_ids=[])
        api.http_client = http
        resp = _run(api.import_bundle(self._bundle(), on_conflict="skip", dry_run=False))
        self.assertTrue(resp["success"])
        self.assertEqual(resp["summary"]["models"]["created"], 1)
        self.assertEqual(len(http.register_calls), 1)
        self.assertEqual(http.register_calls[0]["id"], "mdl-ext-x")
        self.assertEqual(http.register_calls[0]["api_key"], "sekret-123")

    def test_skipped_model_is_registered_if_missing_from_docgrok(self):
        """Regression: on_conflict=skip used to short-circuit registration."""
        # Model already in Cosmos (prior-run artefact), but missing from DocGrok.
        self.store.seed([{
            "doc_type": "docgrok_model", "id": "mdl-ext-y", "name": "stale-embed",
            "type": "azure-openai", "endpoint": "https://ex.example.com",
            "deployment": "text-embedding-3-small", "embedding_dim": 1536,
            "api_version": "2024-06-01", "model_category": "embedding",
            "api_key": "existing-key",
        }])
        http = FakeHttp(registry_ids=[])  # DocGrok registry is empty
        api.http_client = http

        bundle = self._bundle(model_id="mdl-ext-y")
        resp = _run(api.import_bundle(bundle, on_conflict="skip", dry_run=False))
        self.assertTrue(resp["success"])
        self.assertEqual(resp["summary"]["models"]["skipped"], 1)
        self.assertEqual(resp["summary"]["models"]["created"], 0)
        # The fix: skipped models still get registered with DocGrok when missing.
        self.assertEqual(len(http.register_calls), 1,
                         f"expected 1 registration; got {http.register_calls}")
        self.assertEqual(http.register_calls[0]["id"], "mdl-ext-y")

    def test_skipped_model_not_reregistered_if_already_in_docgrok(self):
        self.store.seed([{
            "doc_type": "docgrok_model", "id": "mdl-ext-z", "name": "ok-embed",
            "model_category": "embedding",
        }])
        http = FakeHttp(registry_ids=["mdl-ext-z"])
        api.http_client = http
        resp = _run(api.import_bundle(self._bundle(model_id="mdl-ext-z"), on_conflict="skip", dry_run=False))
        self.assertTrue(resp["success"])
        self.assertEqual(len(http.register_calls), 0,
                         f"expected no re-registration; got {http.register_calls}")

    def test_chat_model_not_registered_with_docgrok(self):
        http = FakeHttp(registry_ids=[])
        api.http_client = http
        resp = _run(api.import_bundle(self._bundle(model_id="mdl-ext-c", category="chat"),
                                      on_conflict="skip", dry_run=False))
        self.assertTrue(resp["success"])
        self.assertEqual(len(http.register_calls), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
