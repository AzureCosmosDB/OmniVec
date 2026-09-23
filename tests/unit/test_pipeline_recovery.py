"""Offline regression tests for control-plane recovery and concurrency."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)


class MemoryStore:
    def __init__(self, *docs):
        self.docs = {(doc["doc_type"], doc["id"]): deepcopy(doc) for doc in docs}
        self.replacements = []
        self.before_replace = None

    def get(self, doc_id, partition_key):
        return deepcopy(self.docs.get((partition_key, doc_id)))

    def list(self, doc_type):
        return [deepcopy(doc) for (kind, _), doc in self.docs.items() if kind == doc_type]

    def query(self, query, parameters=None, partition_key=None):
        return self.list(partition_key)

    def create(self, doc):
        key = (doc["doc_type"], doc["id"])
        if key in self.docs:
            raise CosmosResourceExistsError(status_code=409)
        return self.upsert(doc)

    def upsert(self, doc):
        saved = deepcopy(doc)
        saved["_etag"] = str(int(saved.get("_etag", "0")) + 1)
        self.docs[(doc["doc_type"], doc["id"])] = saved
        return deepcopy(saved)

    def replace_with_etag(self, doc, etag):
        if self.before_replace:
            callback, self.before_replace = self.before_replace, None
            callback()
        existing = self.get(doc["id"], doc["doc_type"])
        if existing is None:
            raise CosmosResourceNotFoundError(status_code=404)
        if existing["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.replacements.append(deepcopy(doc))
        return self.upsert(doc)

    def delete(self, doc_id, partition_key):
        if self.docs.pop((partition_key, doc_id), None) is None:
            raise CosmosResourceNotFoundError(status_code=404)


def pipeline(status="active", **overrides):
    return {
        "id": "pipeline", "doc_type": "pipeline", "_etag": "1", "name": "test",
        "sources": [{"source_id": "source"}], "docgrok_pipeline": "model",
        "destination_id": "destination", "vector_index_path": "embedding",
        "status": status, "generation": "1", **overrides,
    }


def job(job_id="job", **overrides):
    return {
        "id": job_id, "doc_type": "job", "_etag": "1", "pipeline_id": "pipeline",
        "source_id": "source", "source_ref": "document", "status": "processing",
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "retry_count": 0, "worker_field": "preserved", **overrides,
    }


@pytest.fixture
def modules(api_app, monkeypatch):
    loaded = {"api": sys.modules["api"], "store": sys.modules["store"]}
    for name in ("controller", "checkpoint_manager", "progress_tracker"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        loaded[name] = importlib.import_module(name)
    return SimpleNamespace(**loaded)


def attach(monkeypatch, module, store):
    monkeypatch.setattr(module, "get_store", lambda: store)


def test_store_uses_supported_cosmos_etag_contract(modules):
    store = object.__new__(modules.store.MetadataStore)
    store._container = SimpleNamespace(replace_item=Mock(return_value={"_etag": "2"}))
    document = {"id": "job", "doc_type": "job"}
    store.replace_with_etag(document, "1")
    store._container.replace_item.assert_called_once_with(
        item="job", body=document, etag="1", match_condition=MatchConditions.IfNotModified,
    )


def test_new_checkpoint_and_external_reset_clear_cached_state(modules, monkeypatch):
    store = MemoryStore()
    attach(monkeypatch, modules.checkpoint_manager, store)
    checkpoint = modules.checkpoint_manager.CheckpointManager("source", "backfill")
    assert checkpoint.load() is None
    assert checkpoint.save("token", "item", 10)
    assert checkpoint.get_continuation_token() == "token"
    store.delete(checkpoint.checkpoint_id, "checkpoint")
    assert checkpoint.reset()
    assert checkpoint.get_continuation_token() is None
    assert checkpoint._current_etag is None
    assert checkpoint.save(None, "", 0)
    store.delete(checkpoint.checkpoint_id, "checkpoint")
    assert checkpoint.load() is None
    assert checkpoint.get_items_processed() == 0


def test_checkpoint_create_conflict_does_not_roll_back_winning_writer(modules, monkeypatch):
    store = MemoryStore()
    attach(monkeypatch, modules.checkpoint_manager, store)
    first = modules.checkpoint_manager.CheckpointManager("source", "backfill")
    stale = modules.checkpoint_manager.CheckpointManager("source", "backfill")
    assert first.save("new-token", "new-item", 20)
    assert not stale.save("old-token", "old-item", 5)
    assert stale.get_items_processed() == 20
    assert not store.replacements


def test_checkpoint_save_keeps_its_own_write_etag(modules, monkeypatch):
    store = MemoryStore()
    attach(monkeypatch, modules.checkpoint_manager, store)
    checkpoint = modules.checkpoint_manager.CheckpointManager("source", "backfill")
    monkeypatch.setattr(store, "get", Mock(side_effect=AssertionError("unexpected read after write")))
    assert checkpoint.save("token", "item", 1)
    assert checkpoint._current_etag == "1"


@pytest.mark.parametrize("started", [
    (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat(),
    None,
])
def test_job_recovery_normalizes_timestamps_and_preserves_worker_fields(modules, monkeypatch, started):
    store = MemoryStore(pipeline(), job(started_at=started, _ts=int(datetime.now().timestamp()) - 7200))
    attach(monkeypatch, modules.controller, store)
    modules.controller.monitor_job_health()
    saved = store.get("job", "job")
    assert saved["status"] == "pending"
    assert saved["retry_count"] == 1
    assert saved["worker_field"] == "preserved"
    assert saved["started_at"] is None
    assert len(store.replacements) == 1


@pytest.mark.parametrize("status", ["paused", "error", None])
def test_inactive_pipeline_does_not_consume_job_retries(modules, monkeypatch, status):
    store = MemoryStore(job(status="failed"), *([pipeline(status)] if status else []))
    attach(monkeypatch, modules.controller, store)
    modules.controller.monitor_job_health()
    assert store.get("job", "job")["retry_count"] == 0
    assert not store.replacements


def test_bad_job_does_not_starve_later_jobs(modules, monkeypatch):
    store = MemoryStore(pipeline(), job("bad", started_at="not-a-date"), job("good"))
    attach(monkeypatch, modules.controller, store)
    modules.controller.monitor_job_health()
    assert store.get("good", "job")["status"] == "pending"


def test_concurrent_completion_wins_over_recovery(modules, monkeypatch):
    store = MemoryStore(pipeline(), job())
    attach(monkeypatch, modules.controller, store)
    store.before_replace = lambda: store.upsert(job(status="completed"))
    modules.controller.monitor_job_health()
    assert store.get("job", "job")["status"] == "completed"


def test_retry_limit_remains_terminal(modules, monkeypatch):
    store = MemoryStore(pipeline(), job(retry_count=modules.controller.MAX_RETRY_COUNT))
    attach(monkeypatch, modules.controller, store)
    modules.controller.monitor_job_health()
    assert store.get("job", "job")["status"] == "failed"
    assert "Timed out" in store.get("job", "job")["error"]


def test_cancel_rejects_concurrent_worker_claim(modules, monkeypatch):
    store = MemoryStore(job(status="pending"))
    attach(monkeypatch, modules.api, store)
    store.before_replace = lambda: store.upsert(job(status="processing"))
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api.cancel_job("job")
    assert error.value.status_code == 409
    assert store.get("job", "job")["status"] == "processing"


def test_lifecycle_action_cannot_resurrect_deleted_pipeline(modules, monkeypatch):
    store = MemoryStore(pipeline())
    attach(monkeypatch, modules.api, store)
    store.before_replace = lambda: store.delete("pipeline", "pipeline")
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api.pause_pipeline("pipeline")
    assert error.value.status_code == 409
    assert store.get("pipeline", "pipeline") is None


def test_manual_retry_requires_active_pipeline(modules, monkeypatch):
    store = MemoryStore(pipeline("paused"), job(status="failed"))
    attach(monkeypatch, modules.api, store)
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api.retry_job("job")
    assert error.value.status_code == 409
    assert store.get("job", "job")["retry_count"] == 0


def test_reset_does_not_hide_failed_job_cleanup(modules, monkeypatch):
    store = MemoryStore(pipeline(), job())
    attach(monkeypatch, modules.api, store)
    monkeypatch.setattr(store, "delete", Mock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api.reset_pipeline("pipeline")
    assert error.value.status_code == 503
    saved = store.get("pipeline", "pipeline")
    assert saved["status"] == "paused"
    assert saved["generation"] == "1"


@pytest.mark.asyncio
async def test_reset_chunk_cleanup_works_in_sync_endpoint_thread(modules, monkeypatch):
    destination = {
        "id": "destination", "doc_type": "destination", "_etag": "1", "name": "vector",
        "type": "cosmosdb-vector", "config": {"key": "test-key"},
    }
    store = MemoryStore(pipeline(content_strategy="chunk"), destination)
    attach(monkeypatch, modules.api, store)
    connector = importlib.import_module("connectors.cosmosdb_vector_connector")
    calls = []

    async def cleanup(config, prefix):
        calls.append((config, prefix))
        return 3

    monkeypatch.setattr(connector, "delete_chunks_by_prefix", cleanup)
    modules.api._pipeline_stats_cache["pipeline"] = ("stale", 0)
    result = await asyncio.to_thread(modules.api.reset_pipeline, "pipeline")
    assert result["chunks_deleted"] == 3
    assert calls == [({"key": "test-key"}, "pipeline-")]
    assert store.get("pipeline", "pipeline")["status"] == "active"
    assert "pipeline" not in modules.api._pipeline_stats_cache


@pytest.mark.asyncio
async def test_run_probes_and_persists_unmasked_destination_config(modules, monkeypatch):
    destination = {
        "id": "destination", "doc_type": "destination", "_etag": "1", "name": "vector",
        "type": "cosmosdb-vector", "enabled": False, "config": {"key": "test-key"},
    }
    store = MemoryStore(pipeline("paused"), destination)
    attach(monkeypatch, modules.api, store)
    connector = importlib.import_module("connectors.cosmosdb_vector_connector")

    async def probe(config):
        assert config["key"] == "test-key"
        return {"has_vector_policy": True}

    monkeypatch.setattr(connector, "test_vector_connection", probe)
    result = await modules.api.run_pipeline("pipeline")
    assert result["success"]
    assert store.get("destination", "destination")["config"]["key"] == "test-key"
    assert store.get("destination", "destination")["enabled"]


@pytest.mark.asyncio
async def test_run_rejects_deleted_destination(modules, monkeypatch):
    store = MemoryStore(pipeline("paused"))
    attach(monkeypatch, modules.api, store)
    with pytest.raises(modules.api.HTTPException) as error:
        await modules.api.run_pipeline("pipeline")
    assert error.value.status_code == 409
    assert store.get("pipeline", "pipeline")["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_chunk_cleanup_only_ignores_already_deleted_items(modules, monkeypatch, missing):
    connector = importlib.import_module("connectors.cosmosdb_vector_connector")
    failure = CosmosResourceNotFoundError(status_code=404) if missing else RuntimeError("unavailable")
    container = SimpleNamespace(
        query_items=Mock(return_value=[{"id": "pipeline-chunk"}]),
        delete_item=Mock(side_effect=failure),
    )
    database = SimpleNamespace(get_container_client=lambda _: container)

    async def client(_):
        return SimpleNamespace(get_database_client=lambda _: database)

    monkeypatch.setattr(connector, "get_cosmos_client", client)
    config = {"database": "database", "container": "vectors", "partition_key_path": "/id"}
    if missing:
        assert await connector.delete_chunks_by_prefix(config, "pipeline-") == 0
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            await connector.delete_chunks_by_prefix(config, "pipeline-")


def test_concurrent_progress_update_preserves_other_worker_fields(modules, monkeypatch):
    progress = {
        "id": "progress-source", "doc_type": "progress", "_etag": "1", "source_id": "source",
        "workers": {}, "live": {}, "status": "live",
    }
    store = MemoryStore(progress)
    attach(monkeypatch, modules.progress_tracker, store)
    monkeypatch.setattr(modules.progress_tracker.time, "sleep", lambda _: None)

    def concurrent_update():
        current = store.get("progress-source", "progress")
        current["live"] = {"events_processed": 42}
        current["workers"]["other"] = {"ready": 3}
        store.upsert(current)

    store.before_replace = concurrent_update
    modules.progress_tracker.ProgressTracker("source").update_workers("backfill", 1, 1, 1)
    saved = store.get("progress-source", "progress")
    assert saved["live"] == {"events_processed": 42}
    assert saved["workers"]["other"] == {"ready": 3}
    assert saved["workers"]["backfill"]["ready"] == 1


def test_concurrent_backfill_locations_merge_and_recalculate_totals(modules, monkeypatch):
    progress = {
        "id": "progress-source", "doc_type": "progress", "_etag": "1", "source_id": "source",
        "backfill": {"locations": {}, "totals": {}}, "status": "backfilling",
    }
    store = MemoryStore(progress)
    attach(monkeypatch, modules.progress_tracker, store)
    monkeypatch.setattr(modules.progress_tracker.time, "sleep", lambda _: None)

    def concurrent_update():
        current = store.get("progress-source", "progress")
        current["backfill"]["locations"]["other"] = {
            "blobs_enumerated": 20, "jobs_completed": 20, "total_estimated": 20,
        }
        store.upsert(current)

    store.before_replace = concurrent_update
    modules.progress_tracker.ProgressTracker("source").update_backfill_progress(
        "here", 10, 10, 10, 0, 0, total_estimated=10,
    )
    saved = store.get("progress-source", "progress")
    assert set(saved["backfill"]["locations"]) == {"here", "other"}
    assert saved["backfill"]["totals"]["jobs_completed"] == 30
    assert saved["backfill"]["totals"]["percent_complete"] == 100


def test_progress_contention_is_bounded_and_reported(modules, monkeypatch):
    store = MemoryStore({
        "id": "progress-source", "doc_type": "progress", "_etag": "1", "source_id": "source",
    })
    attach(monkeypatch, modules.progress_tracker, store)
    monkeypatch.setattr(modules.progress_tracker.time, "sleep", lambda _: None)
    replace = Mock(side_effect=CosmosAccessConditionFailedError(status_code=412))
    monkeypatch.setattr(store, "replace_with_etag", replace)
    tracker = modules.progress_tracker.ProgressTracker("source")
    with pytest.raises(CosmosAccessConditionFailedError):
        tracker.set_status(modules.progress_tracker.SourceStatus.LIVE, "running")
    assert replace.call_count == 5


def test_pipeline_progress_uses_current_schema_and_failed_totals(modules, monkeypatch):
    store = MemoryStore(pipeline(), {
        "id": "progress-source", "doc_type": "progress", "status": "backfilling",
        "backfill": {"totals": {"jobs_completed": 4, "jobs_pending": 3, "jobs_failed": 3}},
    })
    attach(monkeypatch, modules.progress_tracker, store)
    progress = modules.progress_tracker.get_pipeline_progress("pipeline")
    assert list(progress["sources"]) == ["source"]
    assert progress["totals"]["documents_failed"] == 3
    assert progress["totals"]["overall_percent"] == 40


@pytest.mark.parametrize("status,expected", [("active", "degraded"), ("paused", "paused")])
def test_missing_progress_is_not_reported_healthy(modules, monkeypatch, status, expected):
    store = MemoryStore(pipeline(status))
    attach(monkeypatch, modules.progress_tracker, store)
    assert modules.progress_tracker.get_pipeline_progress("pipeline")["status"] == expected


def inline_source(source_id="source", kind="cosmosdb", **config):
    return {
        "id": source_id, "doc_type": "source", "type": kind, "name": source_id,
        "config": config or {"endpoint": "https://account.example/", "database": "db", "container": "docs"},
    }


def inline_destination(**config):
    return {
        "id": "destination", "doc_type": "destination", "_etag": "1",
        "name": "destination", "type": "cosmosdb-vector", "enabled": True,
        "config": config or {"endpoint": "https://account.example/", "database": "db", "container": "docs"},
    }


@pytest.mark.parametrize("source", [
    inline_source("alias", endpoint="https://ACCOUNT.EXAMPLE", database="db", container="docs"),
    inline_source("alias", endpoint="https://ACCOUNT.EXAMPLE:443/", database="db", container="docs"),
    inline_source("source"),
])
def test_inline_conflict_matches_physical_source_even_with_different_vector_field(modules, source):
    other = pipeline(id="other", processing_mode="inline", vector_index_path="other_embedding")
    store = MemoryStore(inline_source(), source, other)
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api._require_exclusive_inline_sources(store, [{"source_id": source["id"]}])
    assert error.value.status_code == 409
    assert "other" in error.value.detail


@pytest.mark.parametrize("kind,first,second", [
    ("postgresql",
     {"host": "PG.EXAMPLE", "database": "db", "table": "docs"},
     {"host": "pg.example", "port": "5432", "database": "db", "schema": "public", "table": "docs"}),
    ("postgresql",
     {"host": "pg.example", "database": "db", "table": "docs"},
     {"connection_string": 'Host=pg.example;Database="db";Password="unused;quoted";',
      "host": "ignored.example", "database": "ignored", "schema_name": "public", "table": "docs"}),
    ("mssql",
     {"host": "sql.example", "database": "db", "schema": "dbo", "table": "docs"},
     {"server": "SQL.EXAMPLE", "port": "1433", "database": "db", "table": "docs"}),
    ("mssql",
     {"host": "sql.example", "database": "db", "table": "docs"},
     {"connection_string": "Data Source=tcp:SQL.EXAMPLE,1433;Initial Catalog=db;Password='unused;quoted';",
      "host": "ignored.example", "database": "ignored", "table": "docs"}),
])
def test_inline_sql_aliases_share_single_slot_metadata(modules, kind, first, second):
    store = MemoryStore(
        inline_source("source", kind, **first), inline_source("alias", kind, **second),
        pipeline(id="other", processing_mode="inline", vector_index_path="different_vector"),
    )
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api._require_exclusive_inline_sources(store, [{"source_id": "alias"}])
    assert error.value.status_code == 409


@pytest.mark.parametrize("kind,first,second", [
    ("cosmosdb",
     {"endpoint": "https://one.example", "database": "db", "container": "docs"},
     {"endpoint": "https://two.example", "database": "db", "container": "docs"}),
    ("cosmosdb",
     {"endpoint": "https://one.example", "database": "db", "container": "docs"},
     {"endpoint": "https://one.example", "database": "db", "container": "other"}),
    ("postgresql",
     {"host": "pg.example", "database": "db", "table": "docs"},
     {"host": "pg.example", "port": 5433, "database": "db", "table": "docs"}),
    ("postgresql",
     {"host": "pg.example", "database": "db", "table": "docs"},
     {"host": "pg.example", "database": "db", "schema_name": "other", "table": "docs"}),
    ("mssql",
     {"host": "sql.example", "database": "db", "table": "docs"},
     {"host": "sql.example", "database": "other", "table": "docs"}),
])
def test_inline_unrelated_physical_targets_remain_allowed(modules, kind, first, second):
    store = MemoryStore(
        inline_source("source", kind, **first), inline_source("unrelated", kind, **second),
        pipeline(id="other", processing_mode="inline"),
    )
    modules.api._require_exclusive_inline_sources(store, [{"source_id": "unrelated"}])


@pytest.mark.parametrize("status,mode", [("paused", "inline"), ("error", "inline"), ("active", "queue")])
def test_inline_ownership_ignores_inactive_and_queue_pipelines(modules, status, mode):
    store = MemoryStore(inline_source(), pipeline(id="other", status=status, processing_mode=mode))
    modules.api._require_exclusive_inline_sources(store, [{"source_id": "source"}])


def test_inline_ownership_ignores_self_and_allows_unrelated_multisource_pipelines(modules):
    store = MemoryStore(
        inline_source(), inline_source("second", endpoint="https://other.example", database="db", container="docs"),
        pipeline(processing_mode="inline"),
    )
    modules.api._require_exclusive_inline_sources(
        store, [{"source_id": "source"}, {"source_id": "second"}], pipeline_id="pipeline",
    )


def test_inline_pipeline_rejects_aliased_sources_within_request(modules):
    store = MemoryStore(inline_source(), inline_source("alias"))
    with pytest.raises(modules.api.HTTPException) as error:
        modules.api._require_exclusive_inline_sources(
            store, [{"source_id": "source"}, {"source_id": "alias"}],
        )
    assert error.value.status_code == 409
    assert "metadata" in error.value.detail


def test_unrelated_existing_alias_conflict_does_not_block_new_inline_target(modules):
    store = MemoryStore(
        inline_source(), inline_source("alias"),
        inline_source("unrelated", endpoint="https://other.example", database="db", container="docs"),
        pipeline(id="other", processing_mode="inline",
                 sources=[{"source_id": "source"}, {"source_id": "alias"}]),
    )
    modules.api._require_exclusive_inline_sources(store, [{"source_id": "unrelated"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "resume", "run", "mode", "reset"])
async def test_inline_conflict_is_rejected_on_every_activation_path(modules, monkeypatch, operation):
    candidate = pipeline(
        name="candidate", processing_mode="inline",
        status="paused" if operation in ("resume", "run") else "active",
    )
    other = pipeline(id="other", name="other", processing_mode="inline", vector_index_path="different_vector")
    if operation == "mode":
        candidate["processing_mode"] = "queue"
    store = MemoryStore(inline_source(), inline_destination(), other, *([] if operation == "create" else [candidate]))
    attach(monkeypatch, modules.api, store)
    request = modules.api.CreatePipelineRequest(**{**candidate, "processing_mode": "inline"})
    with pytest.raises(modules.api.HTTPException) as error:
        if operation == "create":
            await modules.api.create_pipeline(request)
        elif operation == "update":
            await modules.api.update_pipeline("pipeline", request)
        elif operation == "resume":
            modules.api.resume_pipeline("pipeline")
        elif operation == "run":
            await modules.api.run_pipeline("pipeline")
        elif operation == "mode":
            modules.api.set_processing_mode("pipeline", "inline")
        else:
            modules.api.reset_pipeline("pipeline")
    assert error.value.status_code == 409
    assert "metadata" in error.value.detail
    assert store.get("pipeline", "pipeline") == (None if operation == "create" else candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "resume", "run", "mode"])
async def test_queue_paths_remain_allowed_with_inline_source_owner(modules, monkeypatch, operation):
    candidate = pipeline(name="candidate", processing_mode="queue")
    other = pipeline(id="other", name="other", processing_mode="inline")
    destination = inline_destination(endpoint="https://account.example/", database="db", container="vectors")
    store = MemoryStore(inline_source(), destination, other, *([] if operation == "create" else [candidate]))
    attach(monkeypatch, modules.api, store)
    monkeypatch.setattr(modules.api, "_require_blob_source_enabled", lambda *_: None)
    monkeypatch.setattr(modules.api, "_validate_docgrok_ref", AsyncMock(return_value="model"))
    monkeypatch.setattr(modules.api, "_enforce_pipeline_dim_match", AsyncMock())
    request = modules.api.CreatePipelineRequest(**candidate)
    if operation == "create":
        result = await modules.api.create_pipeline(request)
    elif operation == "update":
        result = await modules.api.update_pipeline("pipeline", request)
    elif operation == "resume":
        result = modules.api.resume_pipeline("pipeline")
    elif operation == "run":
        result = await modules.api.run_pipeline("pipeline")
    else:
        result = modules.api.set_processing_mode("pipeline", "queue")
    assert result["success"]


@pytest.mark.asyncio
async def test_paused_inline_configuration_can_be_edited_without_claiming_source(modules, monkeypatch):
    candidate = pipeline(name="candidate", processing_mode="inline", status="paused")
    other = pipeline(id="other", name="other", processing_mode="inline")
    store = MemoryStore(inline_source(), inline_destination(), other, candidate)
    attach(monkeypatch, modules.api, store)
    monkeypatch.setattr(modules.api, "_validate_docgrok_ref", AsyncMock(return_value="model"))
    monkeypatch.setattr(modules.api, "_enforce_pipeline_dim_match", AsyncMock())
    result = await modules.api.update_pipeline("pipeline", modules.api.CreatePipelineRequest(**candidate))
    assert result["success"]
    assert store.get("pipeline", "pipeline")["status"] == "paused"
