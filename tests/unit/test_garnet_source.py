from types import SimpleNamespace
import sys

import pytest
from fastapi import HTTPException


@pytest.fixture
def native_api(api_app):
    return sys.modules["api"]


class Store:
    def get(self, identifier, kind):
        if kind == "source" and identifier == "source":
            return {"type": "garnet", "config": {
                "endpoint": "garnet:6379", "tls": False, "hash_key": "documents",
            }}


def pipeline(**changes):
    values = {
        "sources": [SimpleNamespace(
            source_id="source", content_fields=["content"], content_mode="field",
            content_type_field=None, filters={},
        )],
        "processing_mode": "inline", "content_strategy": "truncate", "chunk_config": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def destination(**changes):
    config = {"endpoint": "garnet:6379", "tls": False, "vector_set": "vectors"}
    config.update(changes)
    return {"type": "garnet", "config": config}


def test_native_garnet_inline_contract(native_api):
    native_api._require_garnet_pipeline_contract(Store(), pipeline(), destination())
    native_api._require_inline_compatible(Store(), pipeline().sources, destination())


@pytest.mark.parametrize("changes", [
    {"processing_mode": "queue"}, {"content_strategy": "chunk"},
    {"chunk_config": {}},
])
def test_native_garnet_rejects_unsupported_processing(native_api, changes):
    with pytest.raises(HTTPException):
        native_api._require_garnet_pipeline_contract(Store(), pipeline(**changes), destination())


@pytest.mark.parametrize("changes", [
    {"vector_set": "documents"}, {"vector_set": "__omnivec:source-checkpoints:test"},
    {"endpoint": "other:6379"}, {"tls": True},
])
def test_native_garnet_rejects_invalid_target(native_api, changes):
    with pytest.raises(HTTPException):
        native_api._require_garnet_pipeline_contract(Store(), pipeline(), destination(**changes))


@pytest.mark.parametrize("changes", [
    {"content_fields": ["other"]}, {"content_mode": "http_url"},
    {"filters": {"something": True}},
])
def test_native_garnet_rejects_unsupported_binding(native_api, changes):
    value = pipeline()
    for key, setting in changes.items():
        setattr(value.sources[0], key, setting)
    with pytest.raises(HTTPException):
        native_api._require_garnet_pipeline_contract(Store(), value, destination())
