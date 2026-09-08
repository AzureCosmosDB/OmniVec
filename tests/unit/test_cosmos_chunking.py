"""Offline validation of the Cosmos-specific queue chunk contract."""
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from .test_pipeline_recovery import MemoryStore, pipeline


@pytest.fixture
def setup(api_app, monkeypatch):
    api = sys.modules["api"]
    models = sys.modules["models"]
    source = {"id": "source", "name": "source", "doc_type": "source", "type": "cosmosdb",
              "config": {"endpoint": "https://local.invalid", "database": "db", "container": "source"}}
    dest = {"id": "destination", "name": "destination", "doc_type": "destination", "type": "cosmosdb-vector",
            "config": {"endpoint": "https://local.invalid", "database": "db",
                       "container": "vectors", "partition_key_path": "/id"}}
    store = MemoryStore(source, dest)
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "_require_blob_source_enabled", lambda *_: None)
    monkeypatch.setattr(api, "_validate_docgrok_ref", AsyncMock(return_value="mdl-test"))
    monkeypatch.setattr(api, "_enforce_pipeline_dim_match", AsyncMock())
    req = models.CreatePipelineRequest(name="chunk", sources=[{"source_id": "source"}],
        destination_id="destination", vector_index_path="embedding", docgrok_pipeline="mdl-test",
        content_strategy="chunk", chunk_config={"store_text": True})
    return SimpleNamespace(api=api, models=models, store=store, req=req, dest=dest)


@pytest.mark.parametrize("config", [
    {"chunk_size": 99}, {"chunk_overlap": -1}, {"chunk_size": 100},
    {"chunk_unit": "bpe"}, {"unknown": True}, {"doc_id_pattern": "{source}"},
    {"doc_id_pattern": "{unknown}-{chunk}"}, {"doc_id_pattern": "{chunk:03d}"},
    {"text_field": "source_ref"}, {"text_field": "embedding"},
    {"text_field": "id"}, {"doc_id_pattern": "bad/path-{chunk}"},
    {"doc_id_pattern": "same-name-for-every-chunk"},
    {"doc_id_pattern": "{source}-{pipeline}"},
])
def test_invalid_config_rejected(setup, config):
    setup.req.chunk_config = config
    with pytest.raises(HTTPException) as error:
        setup.api._validate_cosmos_chunking(setup.store, setup.req, setup.dest)
    assert error.value.status_code == 400


@pytest.mark.parametrize("unit", ["chars", "tokens"])
def test_valid_config_and_defaults(setup, unit):
    setup.req.chunk_config = {"chunk_size": 500, "chunk_overlap": 50, "chunk_unit": unit}
    config = setup.api._validate_cosmos_chunking(setup.store, setup.req, setup.dest)
    assert config.chunk_size == 500 and config.chunk_overlap == 50
    assert config.chunk_unit == unit and not config.store_text and config.text_field == "text"


def test_custom_template_is_preserved_not_replaced_with_default(setup):
    template = "custom-{pipeline_hash}-{source_hash}-{source_ref}-{chunk}"
    setup.req.chunk_config = {"chunk_size": 450, "chunk_overlap": 90, "doc_id_pattern": template}
    config = setup.api._validate_cosmos_chunking(setup.store, setup.req, setup.dest)
    assert config.doc_id_pattern == template
    assert config.chunk_size == 450 and config.chunk_overlap == 90


@pytest.mark.asyncio
async def test_create_update_resume_run_mode_reject_inline(setup):
    setup.req.processing_mode = "inline"
    with pytest.raises(HTTPException, match="") as error:
        await setup.api.create_pipeline(setup.req)
    assert "chunk+inline" in error.value.detail
    doc = pipeline(content_strategy="chunk", chunk_config={"store_text": True})
    setup.store.upsert(doc)
    with pytest.raises(HTTPException):
        await setup.api.update_pipeline("pipeline", setup.req)
    with pytest.raises(HTTPException):
        setup.api.set_processing_mode("pipeline", "inline")
    doc["processing_mode"] = "inline"
    setup.store.upsert(doc)
    with pytest.raises(HTTPException):
        setup.api.resume_pipeline("pipeline")
    with pytest.raises(HTTPException):
        await setup.api.run_pipeline("pipeline")
    assert setup.store.get("pipeline", "pipeline")["processing_mode"] == "inline"


@pytest.mark.asyncio
async def test_create_persists_and_update_validates_chunk_config(setup):
    response = await setup.api.create_pipeline(setup.req)
    created = response["pipeline"]
    assert created.content_strategy == "chunk" and created.chunk_config.store_text
    setup.req.chunk_config["chunk_overlap"] = -1
    with pytest.raises(HTTPException):
        await setup.api.update_pipeline(created.id, setup.req)
    stored = setup.store.get(created.id, "pipeline")
    assert stored["chunk_config"]["chunk_overlap"] == 200
    setup.req.chunk_config = {"store_text": True, "chunk_size": 400, "chunk_overlap": 50}
    updated = await setup.api.update_pipeline(created.id, setup.req)
    assert updated["pipeline"].chunk_config.chunk_size == 400
    setup.req.chunk_config["store_text"] = False
    with pytest.raises(HTTPException):
        await setup.api.update_pipeline(created.id, setup.req)
    setup.req.content_strategy = "truncate"
    with pytest.raises(HTTPException):
        await setup.api.update_pipeline(created.id, setup.req)


@pytest.mark.asyncio
async def test_custom_chunk_fields_round_trip_and_template_edit(setup):
    setup.req.chunk_config = {"chunk_size": 450, "chunk_overlap": 0, "chunk_unit": "tokens",
                             "store_text": True, "text_field": "passage",
                             "doc_id_pattern": "custom-{source_hash}-{chunk}"}
    created = (await setup.api.create_pipeline(setup.req))["pipeline"]
    setup.req.chunk_config.update(chunk_size=500, doc_id_pattern="edited-{pipeline_hash}-{chunk}")
    await setup.api.update_pipeline(created.id, setup.req)
    stored = setup.store.get(created.id, "pipeline")
    assert stored["chunk_config"] == setup.req.chunk_config
    setup.req.chunk_config["text_field"] = "other"
    with pytest.raises(HTTPException) as error:
        await setup.api.update_pipeline(created.id, setup.req)
    assert "immutable" in error.value.detail
    assert setup.store.get(created.id, "pipeline")["chunk_config"]["text_field"] == "passage"


@pytest.mark.asyncio
async def test_document_template_update_persists(setup):
    setup.req.content_strategy = "truncate"
    setup.req.doc_id_pattern = "original-{source}"
    created = (await setup.api.create_pipeline(setup.req))["pipeline"]
    setup.req.doc_id_pattern = "edited-{source_hash}"
    await setup.api.update_pipeline(created.id, setup.req)
    assert setup.store.get(created.id, "pipeline")["doc_id_pattern"] == "edited-{source_hash}"


def test_source_modes_destination_and_partition_validated(setup):
    for mode in ("url", "auto"):
        setup.req.sources[0].content_mode = mode
        with pytest.raises(HTTPException):
            setup.api._validate_cosmos_chunking(setup.store, setup.req, setup.dest)
    setup.req.sources[0].content_mode = "field"
    for pk in ("", "/source_ref", "/nested/key"):
        dest = deepcopy(setup.dest)
        dest["config"]["partition_key_path"] = pk
        with pytest.raises(HTTPException):
            setup.api._validate_cosmos_chunking(setup.store, setup.req, dest)
    setup.req.content_strategy = "truncate"
    assert setup.api._validate_cosmos_chunking(setup.store, setup.req, setup.dest) is None
