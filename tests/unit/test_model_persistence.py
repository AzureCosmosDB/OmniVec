"""Model operations must never report durable success after a storage failure."""
import asyncio
from copy import deepcopy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError
from fastapi import HTTPException
import httpx
import pytest


MODEL_ID = "mdl-ext-test"


def response(status=200, body=None):
    return httpx.Response(
        status, json=body or {}, request=httpx.Request("GET", "http://docgrok.test.invalid"),
    )


@pytest.fixture
def registry(api_app, monkeypatch):
    api = sys.modules["api"]
    doc = {
        "id": MODEL_ID, "doc_type": "docgrok_model", "_etag": "version-1",
        "name": "fixture-model", "model_category": "embedding",
        "endpoint": "https://model.example.invalid", "deployment": "embedding",
        "api_key_envelope": {"ciphertext": "fixture-envelope"},
    }
    store = Mock()
    store.get.side_effect = lambda *args: deepcopy(doc)
    store.query.return_value = []
    store.list.return_value = []
    client = SimpleNamespace(
        get=AsyncMock(return_value=response(body={"models": [{"id": MODEL_ID}]})),
        post=AsyncMock(return_value=response(201, {"id": MODEL_ID})),
        delete=AsyncMock(return_value=response(body={"deleted": MODEL_ID})),
    )
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "http_client", client)
    keyvault = __import__("keyvault_client")
    monkeypatch.setattr(keyvault, "set_model_api_key", Mock(return_value=False))
    monkeypatch.setattr(keyvault, "delete_model_api_key", Mock())
    return SimpleNamespace(api=api, store=store, client=client, doc=doc)


def fails(awaitable, status):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(awaitable)
    assert caught.value.status_code == status


@pytest.mark.parametrize("query_number", [0, 1])
def test_list_fails_closed_on_each_metadata_query(registry, query_number):
    registry.store.query.side_effect = [[], AzureError("unavailable")] if query_number else AzureError("unavailable")
    fails(registry.api.list_models(), 503)


@pytest.mark.parametrize("status,body", [(503, {}), (200, {"error": "not a registry"})])
def test_list_rejects_upstream_failure_and_invalid_registry(registry, status, body):
    registry.client.get.return_value = response(status, body)
    fails(registry.api.list_models(), 503)


def test_list_merges_chat_models_without_returning_credentials(registry):
    registry.store.query.side_effect = [
        [{"id": MODEL_ID, "model_category": "embedding"}],
        [{"id": "chat-model", "model_category": "chat", "api_key": "fixture-key"}],
    ]
    result = asyncio.run(registry.api.list_models())
    assert [(m["id"], m["model_category"]) for m in result["models"]] == [
        (MODEL_ID, "embedding"), ("chat-model", "chat"),
    ]
    assert "fixture-key" not in str(result)


def test_registration_does_not_allocate_another_id_after_lookup_failure(registry):
    registry.store.query.side_effect = AzureError("unavailable")
    fails(registry.api.create_model({"name": "fixture-model"}), 503)
    registry.client.post.assert_not_awaited()
    registry.store.create.assert_not_called()


def test_registration_metadata_failure_is_not_success(registry):
    registry.store.get.side_effect = AzureError("unavailable")
    fails(registry.api.create_model({"name": "fixture-model"}), 503)


def test_registration_metadata_patch_preserves_envelope_and_uses_etag(registry):
    registry.doc.pop("model_category")
    registry.store.query.return_value = [deepcopy(registry.doc)]
    result = asyncio.run(registry.api.create_model({"name": "fixture-model"}))
    assert result["id"] == MODEL_ID
    assert registry.client.post.call_args.kwargs["json"]["id"] == MODEL_ID
    saved, etag = registry.store.replace_with_etag.call_args.args
    assert saved["api_key_envelope"] == registry.doc["api_key_envelope"]
    assert saved["model_category"] == "embedding"
    assert etag == "version-1"
    registry.store.upsert.assert_not_called()


def test_registration_metadata_conflict_does_not_overwrite_new_credentials(registry):
    registry.doc.pop("model_category")
    registry.store.replace_with_etag.side_effect = CosmosAccessConditionFailedError(status_code=412)
    fails(registry.api.create_model({"name": "fixture-model"}), 409)
    registry.store.upsert.assert_not_called()


@pytest.mark.parametrize("credential", [{"api_key": "fixture-key"}, {"api_key_source": "keyvault"}])
def test_chat_reregistration_preserves_existing_credential(registry, credential):
    registry.doc.update(model_category="chat", **credential)
    registry.store.query.return_value = [deepcopy(registry.doc)]
    result = asyncio.run(registry.api.create_model({"name": "fixture-model", "model_category": "chat"}))
    saved, etag = registry.store.replace_with_etag.call_args.args
    assert all(saved[k] == v for k, v in credential.items())
    assert result["id"] == MODEL_ID
    assert "api_key" not in result and "api_key_envelope" not in result
    assert etag == "version-1"
    registry.client.post.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 409, 503])
def test_embedding_update_propagates_upstream_failure(registry, status):
    registry.client.post.return_value = response(status)
    fails(registry.api.update_model(MODEL_ID, {"deployment": "new"}), status)
    assert registry.doc["deployment"] == "embedding"
    registry.store.upsert.assert_not_called()
    registry.store.replace_with_etag.assert_not_called()


def test_embedding_update_timeout_is_not_success(registry):
    registry.client.post.side_effect = httpx.ReadTimeout("unavailable")
    fails(registry.api.update_model(MODEL_ID, {"deployment": "new"}), 503)


def test_update_read_failure_is_not_not_found(registry):
    registry.store.get.side_effect = AzureError("unavailable")
    fails(registry.api.update_model(MODEL_ID, {"deployment": "new"}), 503)
    registry.client.post.assert_not_awaited()


@pytest.mark.parametrize("upstream_status,expected", [(200, 503), (503, 503), (404, 404)])
def test_update_distinguishes_missing_metadata_from_missing_model(registry, upstream_status, expected):
    registry.store.get.side_effect = None
    registry.store.get.return_value = None
    registry.client.get.return_value = response(upstream_status)
    fails(registry.api.update_model(MODEL_ID, {"deployment": "new"}), expected)
    registry.client.post.assert_not_awaited()


@pytest.mark.parametrize("client_id", ["fixture-client", ""])
def test_successful_update_keeps_identity_and_forwards_client_id(registry, client_id):
    result = asyncio.run(registry.api.update_model(
        MODEL_ID, {"deployment": "new", "client_id": client_id},
    ))
    sent = registry.client.post.call_args.kwargs["json"]
    assert sent["id"] == MODEL_ID
    assert sent["api_key"] == ""
    assert sent["client_id"] == client_id
    assert result["status"] == "updated"


@pytest.mark.parametrize("error,status", [
    (AzureError("unavailable"), 503),
    (CosmosAccessConditionFailedError(status_code=412), 409),
])
def test_chat_update_storage_failure_is_not_success(registry, error, status):
    registry.doc["model_category"] = "chat"
    registry.store.replace_with_etag.side_effect = error
    fails(registry.api.update_model(MODEL_ID, {"deployment": "new"}), status)


@pytest.mark.parametrize("list_number", [0, 1, 2])
def test_delete_does_not_bypass_reference_guard_when_storage_fails(registry, list_number):
    registry.store.list.side_effect = [[] for _ in range(list_number)] + [AzureError("unavailable")]
    fails(registry.api.delete_model(MODEL_ID), 503)
    registry.client.delete.assert_not_awaited()
    registry.store.delete.assert_not_called()


@pytest.mark.parametrize("config", [
    {"model_id": MODEL_ID},
    {"model": MODEL_ID},
    {"steps": [{"type": "model", "model_id": MODEL_ID}]},
    {"steps": [{"model": MODEL_ID}]},
])
def test_delete_guards_indirect_routing_pipeline_references(registry, config):
    registry.store.list.side_effect = lambda kind: [{"id": "routing", **config}] if kind == "docgrok_pipeline" else []
    fails(registry.api.delete_model(MODEL_ID), 400)
    registry.client.delete.assert_not_awaited()


def test_delete_metadata_failure_is_not_success(registry):
    registry.store.delete.side_effect = AzureError("unavailable")
    fails(registry.api.delete_model(MODEL_ID), 503)


def test_delete_tolerates_only_already_deleted_shared_metadata(registry):
    registry.store.delete.side_effect = CosmosResourceNotFoundError(status_code=404)
    assert asyncio.run(registry.api.delete_model(MODEL_ID)) == {"deleted": MODEL_ID}


def test_delete_retry_finishes_cleanup_when_router_already_deleted_model(registry):
    registry.client.delete.return_value = response(404)
    assert asyncio.run(registry.api.delete_model(MODEL_ID)) == {"status": "deleted", "id": MODEL_ID}
    registry.store.delete.assert_called_once_with(MODEL_ID, "docgrok_model")


def test_delete_upstream_failure_leaves_metadata(registry):
    registry.client.delete.return_value = response(503)
    fails(registry.api.delete_model(MODEL_ID), 503)
    registry.store.delete.assert_not_called()


def test_delete_does_not_claim_to_remove_a_native_deployment(registry):
    fails(registry.api.delete_model("mdl-native-fixture"), 400)
    registry.client.delete.assert_not_awaited()
