import asyncio
import sys
import threading
from unittest.mock import Mock

import httpx
import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError, CosmosHttpResponseError


@pytest.mark.asyncio
async def test_readiness_fail_closed_recovers_and_liveness_stays_light(api_app, monkeypatch):
    from readiness import StorageReadiness
    api = sys.modules["api"]
    probe = Mock(side_effect=RuntimeError("storage down"))
    monkeypatch.setattr(api, "storage_readiness", StorageReadiness(probe, cache_seconds=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://api") as client:
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).status_code == 200
        assert probe.call_count == 1
        probe.side_effect = None
        assert (await client.get("/ready")).status_code == 200
        probe.side_effect = RuntimeError("lost access")
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
async def test_readiness_caches_and_coalesces_timed_out_requests(api_app):
    from readiness import StorageReadiness
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    state = StorageReadiness(probe, timeout=0.01, cache_seconds=5)
    try:
        assert await state.ready() is False
        assert entered.is_set()
        assert await asyncio.gather(state.ready(), state.ready()) == [False, False]
        assert len(calls) == 1
    finally:
        release.set()
        await state._task
    assert await state.ready() is True
    assert len(calls) == 1


def test_storage_probe_missing_item_is_healthy_but_missing_container_and_denial_are_not(api_app):
    from store import MetadataStore
    store = MetadataStore.__new__(MetadataStore)
    store._container = Mock()
    store._container.read_item.side_effect = CosmosResourceNotFoundError(status_code=404)
    store.check_readiness()
    kwargs = store._container.read_item.call_args.kwargs
    assert kwargs["retry_total"] == 0
    assert kwargs["read_timeout"] == 2
    store._container.read.side_effect = CosmosResourceNotFoundError(status_code=404)
    with pytest.raises(CosmosResourceNotFoundError):
        store.check_readiness()
    store._container.read.side_effect = None
    store._container.read_item.side_effect = CosmosHttpResponseError(status_code=403)
    with pytest.raises(CosmosHttpResponseError):
        store.check_readiness()


@pytest.mark.asyncio
async def test_startup_initialization_failure_is_not_a_healthy_server(api_app, monkeypatch):
    api = sys.modules["api"]
    monkeypatch.setattr(api, "init_store", Mock(side_effect=RuntimeError("init failed")))
    with pytest.raises(RuntimeError, match="init failed"):
        await api.startup()
    assert api.http_client is None
