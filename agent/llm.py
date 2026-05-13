"""LLM client for the OmniVec Agent.

The agent calls a chat-completion endpoint with tool-calling enabled. Routing
priority:

  1. ``model_id`` arg on the request — looked up in DocGrok's external model
     registry (``GET /api/docgrok/models/{id}``); if found we proxy through
     ``POST /api/docgrok/chat`` (when DocGrok supports it) so the call goes
     through the existing AOAI-routing layer.
  2. ``AGENT_DEFAULT_MODEL_ID`` env var (same lookup).
  3. Direct Azure OpenAI chat-completions call using
     ``AOAI_ENDPOINT`` + ``AOAI_DEPLOYMENT`` + workload-identity bearer.

Tests inject a fake LLM by replacing ``_LLM_BACKEND`` with an object whose
``chat_completion`` coroutine returns deterministic ``{role, content,
tool_calls}`` dicts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"


class _LLMBackend:
    """Default backend — no network in tests; real impl gated by env."""

    async def chat_completion(self, messages: list[dict], tools: list[dict] | None, model_id: str | None) -> LLMResponse:  # pragma: no cover
        endpoint = os.environ.get("AOAI_ENDPOINT", "")
        deployment = os.environ.get("AOAI_DEPLOYMENT", "gpt-4o-mini")
        api_version = os.environ.get("AOAI_API_VERSION", "2024-08-01-preview")
        if not endpoint:
            return LLMResponse(content="agent: no LLM configured (AOAI_ENDPOINT unset)", finish_reason="stop")

        import httpx
        from azure.identity.aio import DefaultAzureCredential

        async with DefaultAzureCredential() as cred:
            tok = await cred.get_token("https://cognitiveservices.azure.com/.default")
            headers = {"Authorization": f"Bearer {tok.token}"}
        url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        body: dict[str, Any] = {"messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            finish_reason=choice.get("finish_reason") or "stop",
        )


_LLM_BACKEND: _LLMBackend = _LLMBackend()


async def chat_completion(messages: list[dict], tools: list[dict] | None = None, model_id: str | None = None) -> LLMResponse:
    """Call the active LLM backend. Indirection used so tests can swap it."""
    return await _LLM_BACKEND.chat_completion(messages, tools, model_id)
