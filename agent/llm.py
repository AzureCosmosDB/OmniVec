"""LLM client for the OmniVec Agent.

The control plane owns registered CHAT model metadata. DocGrok's registry
handles embeddings and does not expose the previously assumed chat route.
The agent calls the registered provider's HTTPS chat endpoint with tool calling.

  1. ``model_id`` arg on the request — looked up through ``/api/models``.
  2. ``AGENT_DEFAULT_MODEL_ID`` env var (same registry lookup).
  3. If neither is configured, a stub response is returned so the agent
     service stays healthy.

Azure OpenAI defaults to workload identity. API-key auth requires an explicit
mode and a secret bound to a single model ID. No secret is read from model
list responses, returned in tool results or sent to a different model override.

Tests inject a fake LLM by replacing ``_LLM_BACKEND`` with an object whose
``chat_completion`` coroutine returns deterministic ``LLMResponse`` instances.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode, urlsplit
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"


class _LLMBackend:
    """Default backend — registered Azure OpenAI/OpenAI-compatible chat models."""

    async def chat_completion(self, messages: list[dict], tools: list[dict] | None, model_id: str | None) -> LLMResponse:  # pragma: no cover
        mid = (model_id or os.environ.get("AGENT_DEFAULT_MODEL_ID", "")).strip()
        if not mid:
            return LLMResponse(
                content="agent: no chat model configured. Register a chat-capable model in OmniVec and set agent.defaultModelId or pass model_id in the request.",
                finish_reason="stop",
            )
        from .tools.omnivec_api import get_model, _ModelId
        try:
            model = await get_model(_ModelId(model_id=mid))
        except Exception:
            return LLMResponse(content="agent: cannot verify the configured chat model; model registry is unavailable.")
        if model.get("error") or model.get("model_category") != "chat":
            return LLMResponse(
                content="agent: selected model is not a registered chat model. Embedding deployments (including text-embedding-3-small) cannot run troubleshooting chat. Deterministic diagnostics remain available."
            )

        endpoint = (model.get("endpoint") or "").rstrip("/")
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return LLMResponse(content="agent: registered chat endpoint must be an HTTPS provider base URL without credentials, query or fragment.")
        provider = model.get("type")
        if provider not in ("azure-openai", "openai", "openai-compatible"):
            return LLMResponse(content="agent: selected chat provider is unsupported; configure Azure OpenAI or an OpenAI-compatible HTTPS endpoint.")
        deployment = (model.get("deployment") or "").strip()
        if not deployment:
            return LLMResponse(content="agent: registered chat deployment/model name is missing.")
        auth_mode = os.getenv("AGENT_CHAT_AUTH_MODE", "managed-identity").strip()
        if auth_mode == "api-key":
            key = os.getenv("AGENT_CHAT_API_KEY", "")
            if not key or os.getenv("AGENT_CHAT_API_KEY_MODEL_ID", "").strip() != mid:
                return LLMResponse(content="agent: API-key chat requires a Kubernetes secret and AGENT_CHAT_API_KEY_MODEL_ID matching the selected model; override refused.")
            headers = {"api-key": key} if provider == "azure-openai" else {"Authorization": f"Bearer {key}"}
        elif auth_mode == "managed-identity" and provider == "azure-openai":
            if not parsed.hostname.endswith((".openai.azure.com", ".cognitiveservices.azure.com")):
                return LLMResponse(content="agent: workload-identity chat requires an Azure public-cloud OpenAI account endpoint; no bearer token was sent.")
            try:
                headers = {"Authorization": f"Bearer {await _azure_chat_token()}"}
            except Exception:
                return LLMResponse(content="agent: chat workload identity authentication unavailable. Configure workload identity and Cognitive Services OpenAI User access on the approved account.")
        else:
            return LLMResponse(content="agent: invalid chat authentication mode; non-Azure providers require explicitly configured api-key mode.")

        if provider == "azure-openai":
            url = f"{endpoint}/openai/deployments/{quote(deployment, safe='')}/chat/completions"
            url += "?" + urlencode({"api-version": model.get("api_version") or "2024-06-01"})
        else:
            url = endpoint + ("/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions")
        body: dict[str, Any] = {"messages": messages, "temperature": 0.1, "max_tokens": 2048}
        if provider != "azure-openai":
            body["model"] = deployment
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                return LLMResponse(
                    content=f"agent: chat call to model '{mid}' failed: HTTP {resp.status_code}. Check the registered chat deployment and provider permissions.",
                    finish_reason="stop",
                )
            data = resp.json()
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            return LLMResponse(content="agent: chat provider returned no valid completion; model/tool-calling capability is not verified.")
        message = choices[0]["message"]
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=message.get("tool_calls") or [],
            finish_reason=choices[0].get("finish_reason") or "stop",
        )


async def _azure_chat_token() -> str:
    from azure.identity.aio import DefaultAzureCredential
    async with DefaultAzureCredential() as credential:
        return (await credential.get_token("https://cognitiveservices.azure.com/.default")).token


_LLM_BACKEND: _LLMBackend = _LLMBackend()


async def chat_completion(messages: list[dict], tools: list[dict] | None = None, model_id: str | None = None) -> LLMResponse:
    """Call the active LLM backend. Indirection used so tests can swap it."""
    return await _LLM_BACKEND.chat_completion(messages, tools, model_id)
