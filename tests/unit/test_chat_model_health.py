"""Chat registration is not embedding readiness or verified inference."""
import ast
from datetime import datetime
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


@pytest.fixture
def checker():
    source = (Path(__file__).resolve().parents[2] / "api" / "health_checker.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if getattr(node, "name", None) == "check_model")
    store = Mock()
    store.get.return_value = None
    namespace = {"datetime": datetime, "httpx": httpx, "get_store": lambda: store,
                 "logger": logging.getLogger(__name__), "CHECK_TIMEOUT": 10, "DOCGROK_URL": "http://docgrok"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<model-health>", "exec"), namespace)
    return namespace["check_model"], store


@pytest.mark.asyncio
async def test_chat_model_never_uses_embedding_probe_or_claims_inference(checker):
    check, store = checker
    store.get.return_value = {"model_category": "chat", "name": "Operations chat", "api_key": "private-test-value"}
    client = SimpleNamespace(get=AsyncMock(), post=AsyncMock())
    result = await check("mdl-chat", client)
    assert result["status"] == "warning"
    assert result["checks"][0]["check"] == "chat_inference"
    assert "not tested" in result["checks"][0]["detail"]
    assert "private-test-value" not in json.dumps(result)
    client.get.assert_not_called()
    client.post.assert_not_called()
    store.get.assert_called_once_with("mdl-chat", "docgrok_model")


@pytest.mark.asyncio
async def test_embedding_model_keeps_real_probe(checker):
    check, _ = checker
    client = SimpleNamespace(get=AsyncMock(side_effect=[
        httpx.Response(200, json={"status": "healthy"}),
        httpx.Response(200, json={"name": "Embedding model", "endpoint": "https://approved.openai.azure.com"}),
    ]), post=AsyncMock(return_value=httpx.Response(200, json={"ok": True, "status": 200})))
    result = await check("mdl-embedding", client)
    assert result["status"] == "healthy"
    assert client.post.call_args.args[0].endswith("/mdl-embedding/healthcheck")


@pytest.mark.asyncio
async def test_category_lookup_failure_does_not_guess_model_capability(checker):
    check, store = checker
    store.get.side_effect = RuntimeError("private database details")
    client = SimpleNamespace(get=AsyncMock(), post=AsyncMock())
    result = await check("mdl-model", client)
    assert result["status"] == "warning"
    assert "private" not in json.dumps(result)
    client.post.assert_not_called()
