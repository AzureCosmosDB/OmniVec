import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException


def test_model_validation_and_limits_use_the_router_registry(api_app, monkeypatch):
    module = sys.modules["api"]
    monkeypatch.setattr(module, "get_store", lambda: SimpleNamespace(get=lambda *args: None))
    paths = []
    model_id = "mdl-ext-registry-contract"

    def handler(request):
        paths.append(request.url.path)
        if request.url.path != "/admin/models/registry":
            return httpx.Response(404)
        return httpx.Response(200, json={"models": [{
            "id": model_id,
            "max_batch_size": 64,
            "context_window_tokens": 512,
            "max_batch_tokens": 6000,
        }]})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(module, "http_client", client)
            assert await module._validate_docgrok_ref(model_id) == model_id
            limits = await module._resolve_model_request_limits(model_id)
            assert limits.model_dump() == {
                "max_batch_size": 64,
                "context_window_tokens": 512,
                "max_batch_tokens": 6000,
            }

    asyncio.run(check())
    assert paths == ["/admin/models/registry", "/admin/models/registry"]


@pytest.mark.parametrize(
    "status,payload,expected_status",
    [
        (404, {}, 503),
        (500, {}, 503),
        (200, {}, 503),
        (200, [], 503),
        (200, {"models": [None]}, 503),
        (200, {"models": []}, 400),
    ],
)
def test_registry_failures_do_not_approve_unknown_models(
    api_app, monkeypatch, status, payload, expected_status,
):
    module = sys.modules["api"]
    monkeypatch.setattr(module, "get_store", lambda: SimpleNamespace(get=lambda *args: None))

    async def check():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            monkeypatch.setattr(module, "http_client", client)
            for operation in (module._validate_docgrok_ref, module._resolve_model_request_limits):
                with pytest.raises(HTTPException) as caught:
                    await operation("mdl-ext-missing")
                assert caught.value.status_code == expected_status

    asyncio.run(check())
