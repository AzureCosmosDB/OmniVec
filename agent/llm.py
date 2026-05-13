"""LLM client for the OmniVec Agent.

The agent calls a chat-completion endpoint with tool-calling enabled via the
DocGrok router, which owns the model registry and provider auth. Routing:

  1. ``model_id`` arg on the request — looked up in DocGrok's external model
     registry (``GET {DOCGROK_URL}/admin/models/registry/{id}``); the chat
     call is proxied through ``POST {DOCGROK_URL}/admin/models/registry/{id}/chat``.
  2. ``AGENT_DEFAULT_MODEL_ID`` env var (same DocGrok lookup).
  3. If neither is configured, a stub response is returned so the agent
     service stays healthy.

DocGrok is reached over the in-cluster service URL (``DOCGROK_URL``). No
direct AOAI endpoint or key is read from agent env — provider credentials
live in the DocGrok model registry.

Tests inject a fake LLM by replacing ``_LLM_BACKEND`` with an object whose
``chat_completion`` coroutine returns deterministic ``LLMResponse`` instances.
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
    """Default backend — proxies chat through the DocGrok router."""

    async def chat_completion(self, messages: list[dict], tools: list[dict] | None, model_id: str | None) -> LLMResponse:  # pragma: no cover
        mid = (model_id or os.environ.get("AGENT_DEFAULT_MODEL_ID", "")).strip()
        if not mid:
            return LLMResponse(
                content="agent: no chat model configured. Register one in DocGrok and set agent.defaultModelId or pass model_id in the request.",
                finish_reason="stop",
            )

        docgrok_url = os.environ.get("DOCGROK_URL", "http://omnivec-docgrok-router").rstrip("/")
        url = f"{docgrok_url}/admin/models/registry/{mid}/chat"
        body: dict[str, Any] = {"messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            resp = await client.post(url, json=body)
            if resp.status_code >= 400:
                return LLMResponse(
                    content=f"agent: chat call to model '{mid}' failed: HTTP {resp.status_code} {resp.text[:300]}",
                    finish_reason="stop",
                )
            data = resp.json()
        return LLMResponse(
            content=data.get("content") or "",
            tool_calls=data.get("tool_calls") or [],
            finish_reason=data.get("finish_reason") or "stop",
        )


_LLM_BACKEND: _LLMBackend = _LLMBackend()


async def chat_completion(messages: list[dict], tools: list[dict] | None = None, model_id: str | None = None) -> LLMResponse:
    """Call the active LLM backend. Indirection used so tests can swap it."""
    return await _LLM_BACKEND.chat_completion(messages, tools, model_id)
