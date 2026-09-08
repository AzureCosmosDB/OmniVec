"""Real chat-provider adapter contracts, exercised only through HTTP mocks."""
from __future__ import annotations

import json

import httpx
import pytest


@pytest.fixture
def backend(agent_app, monkeypatch):
    from agent import llm
    from agent.tools import omnivec_api
    model = {
        "id": "mdl-chat", "model_category": "chat", "type": "azure-openai",
        "endpoint": "https://approved.openai.azure.com", "deployment": "ops-chat",
        "api_version": "2024-06-01",
    }
    async def get_model(_p):
        return model
    monkeypatch.setattr(omnivec_api, "get_model", get_model)
    monkeypatch.setenv("AGENT_CHAT_AUTH_MODE", "api-key")
    monkeypatch.setenv("AGENT_CHAT_API_KEY", "test-provider-secret")
    monkeypatch.setenv("AGENT_CHAT_API_KEY_MODEL_ID", "mdl-chat")
    monkeypatch.setenv("AGENT_DEFAULT_MODEL_ID", "")
    requests = []
    response = {"choices": [{"message": {"content": "ready to diagnose", "tool_calls": []}, "finish_reason": "stop"}]}
    status = [200]
    def handle(request):
        requests.append(request)
        return httpx.Response(status[0], json=response)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    return llm, model, requests, response, status


@pytest.mark.asyncio
async def test_azure_registered_chat_forwards_tools_and_parses_standard_response(backend):
    llm, model, requests, response, _ = backend
    call = {"id": "call1", "type": "function", "function": {
        "name": "diagnose_pipeline", "arguments": '{"pipeline_id":"pip-1"}',
    }}
    response["choices"][0] = {"message": {"content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}
    tools = [{"type": "function", "function": {"name": "diagnose_pipeline"}}]
    result = await llm._LLMBackend().chat_completion([{"role": "user", "content": "diagnose"}], tools, "mdl-chat")
    assert result.tool_calls == [call]
    assert result.finish_reason == "tool_calls"
    request = requests[0]
    assert str(request.url) == "https://approved.openai.azure.com/openai/deployments/ops-chat/chat/completions?api-version=2024-06-01"
    assert request.headers["api-key"] == "test-provider-secret"
    body = json.loads(request.content)
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["max_tokens"] == 2048
    assert "/admin/models/registry/" not in str(request.url)
    assert "test-provider-secret" not in result.content


@pytest.mark.asyncio
async def test_azure_workload_identity_authentication(backend, monkeypatch):
    llm, _, requests, _, _ = backend
    monkeypatch.setenv("AGENT_CHAT_AUTH_MODE", "managed-identity")
    async def token():
        return "test-workload-token"
    monkeypatch.setattr(llm, "_azure_chat_token", token)
    result = await llm._LLMBackend().chat_completion([], [], "mdl-chat")
    assert result.content == "ready to diagnose"
    assert requests[0].headers["Authorization"] == "Bearer test-workload-token"
    assert "api-key" not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["https://provider.example", "https://provider.example/v1"])
async def test_openai_compatible_base_url(backend, endpoint):
    llm, model, requests, _, _ = backend
    model.update(type="openai-compatible", endpoint=endpoint, deployment="chat-model")
    await llm._LLMBackend().chat_completion([], None, "mdl-chat")
    assert str(requests[0].url) == "https://provider.example/v1/chat/completions"
    assert json.loads(requests[0].content)["model"] == "chat-model"
    assert requests[0].headers["Authorization"] == "Bearer test-provider-secret"


@pytest.mark.asyncio
async def test_api_key_cannot_follow_a_different_model_override(backend):
    llm, _, requests, _, _ = backend
    result = await llm._LLMBackend().chat_completion([], [], "mdl-other")
    assert "override refused" in result.content
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [
    "http://provider.example", "https://user:password@provider.example",
    "https://provider.example?token=secret", "https://provider.example#fragment",
])
async def test_invalid_provider_urls_do_not_receive_credentials(backend, endpoint):
    llm, model, requests, _, _ = backend
    model["endpoint"] = endpoint
    result = await llm._LLMBackend().chat_completion([], None, "mdl-chat")
    assert "HTTPS provider base URL" in result.content
    assert requests == []


@pytest.mark.asyncio
async def test_missing_identity_reports_configuration_blocker(backend, monkeypatch):
    llm, _, requests, _, _ = backend
    monkeypatch.setenv("AGENT_CHAT_AUTH_MODE", "managed-identity")
    async def token():
        raise RuntimeError("credential details must not escape")
    monkeypatch.setattr(llm, "_azure_chat_token", token)
    result = await llm._LLMBackend().chat_completion([], [], "mdl-chat")
    assert "workload identity authentication unavailable" in result.content
    assert "credential details" not in result.content
    assert requests == []


@pytest.mark.asyncio
async def test_provider_failure_does_not_expose_error_body(backend):
    llm, _, _, response, status = backend
    status[0] = 403
    response.update(error="provider secret should not escape")
    result = await llm._LLMBackend().chat_completion([], [], "mdl-chat")
    assert "HTTP 403" in result.content
    assert "provider secret" not in result.content


@pytest.mark.asyncio
async def test_invalid_completion_is_not_reported_as_working_chat(backend):
    llm, _, _, response, _ = backend
    response["choices"] = []
    result = await llm._LLMBackend().chat_completion([], [], "mdl-chat")
    assert "no valid completion" in result.content


@pytest.mark.asyncio
async def test_default_registered_model_id_is_supported(backend, monkeypatch):
    llm, _, requests, _, _ = backend
    monkeypatch.setenv("AGENT_DEFAULT_MODEL_ID", "mdl-chat")
    assert (await llm._LLMBackend().chat_completion([], [], None)).content == "ready to diagnose"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_workload_bearer_is_not_sent_to_non_azure_host(backend, monkeypatch):
    llm, model, requests, _, _ = backend
    model["endpoint"] = "https://unapproved.example"
    monkeypatch.setenv("AGENT_CHAT_AUTH_MODE", "managed-identity")
    async def token():
        pytest.fail("Must reject endpoint before acquiring a workload token")
    monkeypatch.setattr(llm, "_azure_chat_token", token)
    result = await llm._LLMBackend().chat_completion([], [], "mdl-chat")
    assert "no bearer token was sent" in result.content
    assert requests == []
