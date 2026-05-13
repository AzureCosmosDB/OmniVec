"""Tool-calling reasoning loop for the OmniVec Agent.

Lifecycle of one chat turn:

  1. Build an OpenAI-style tool schema list from the registered tools (filtered
     by the caller's role).
  2. Call the LLM with system + history + user message.
  3. If the LLM returned ``tool_calls`` (rather than a final answer): for each,
     validate the args against the tool's Pydantic model, execute the tool,
     append the tool_call + tool_result messages, audit the invocation, and
     loop back to step 2.
  4. Cap at ``MAX_ITERATIONS`` (default 8) to prevent runaway loops.
  5. Stream events through an asyncio.Queue so the SSE endpoint can fan them
     out to the caller as they happen.

All events are dicts of shape ``{"type": <token|tool_call|tool_result|final|error>, ...}``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable

from pydantic import ValidationError

from .audit import AuditWriter
from .llm import LLMResponse, chat_completion
from .tools import Tool, get_tool, list_tools, validate_args


logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are the OmniVec Agent — an in-cluster ops assistant for the OmniVec "
    "platform. You answer operational questions ('why is pipeline X stuck?', "
    "'what is the dead-letter count?', 'show me docgrok-router logs') by "
    "calling the read-only diagnostic tools available to you. Plan first, "
    "call one or two tools at a time, then synthesize a concise answer that "
    "cites the tool outputs you used. Never invent data; if a tool fails, "
    "say so."
)


# Type alias: an LLM caller. Default delegates to llm.chat_completion; tests
# pass in a deterministic fake.
LLMCallable = Callable[[list[dict], list[dict] | None, str | None], Awaitable[LLMResponse]]


async def _default_llm(messages, tools, model_id) -> LLMResponse:
    return await chat_completion(messages, tools, model_id)


async def _execute_tool(t: Tool, raw_args: dict) -> dict:
    """Validate + execute one tool. Always returns a dict-shaped result."""
    args = validate_args(t.name, raw_args)
    result = await t.callable(args)
    if not isinstance(result, dict):
        return {"value": result}
    return result


def _tools_for_role(role: str) -> tuple[list[dict], dict[str, Tool]]:
    tools = list_tools(role)
    schemas = [t.json_schema() for t in tools]
    by_name = {t.name: t for t in tools}
    return schemas, by_name


async def run_agent(
    *,
    queue: asyncio.Queue,
    user_message: str,
    history: list[dict],
    role: str,
    model_id: str | None,
    caller_id: str,
    session_id: str,
    audit: AuditWriter | None = None,
    llm: LLMCallable | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> None:
    """Drive the tool-calling loop and stream events to ``queue``.

    The function never raises; errors are surfaced as ``{"type":"error"}`` and
    a sentinel ``None`` is always pushed to ``queue`` at the end so the
    consumer can terminate cleanly.
    """
    llm = llm or _default_llm
    tool_schemas, tools_by_name = _tools_for_role(role)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    final_text = ""
    try:
        for i in range(max_iterations):
            try:
                response = await llm(messages, tool_schemas, model_id)
            except Exception as e:  # noqa: BLE001
                await queue.put({"type": "error", "stage": "llm", "detail": str(e)})
                return

            if response.tool_calls:
                # Append the assistant turn with tool_calls so the next LLM call
                # sees the same call IDs it produced.
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": response.tool_calls,
                })

                for tc in response.tool_calls:
                    tc_id = tc.get("id") or f"call_{i}"
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "")
                    try:
                        raw_args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        raw_args = {}
                    await queue.put({
                        "type": "tool_call",
                        "id": tc_id,
                        "name": name,
                        "args": raw_args,
                    })

                    t = tools_by_name.get(name) or get_tool(name)
                    if t is None or (t.role == "admin" and role != "admin"):
                        err = {"error": f"tool '{name}' not available for role '{role}'"}
                        await queue.put({"type": "tool_result", "id": tc_id, "name": name, "result": err})
                        messages.append({
                            "role": "tool", "tool_call_id": tc_id, "name": name,
                            "content": json.dumps(err),
                        })
                        continue

                    try:
                        result = await _execute_tool(t, raw_args)
                        if audit:
                            await audit.record(
                                session_id=session_id, user=caller_id, role=role,
                                tool_name=name, args=raw_args, result_summary=_summarize(result),
                            )
                    except ValidationError as ve:
                        result = {"error": "invalid arguments", "detail": ve.errors()}
                    except Exception as e:  # noqa: BLE001
                        logger.exception("tool %s failed", name)
                        result = {"error": str(e)}
                    await queue.put({"type": "tool_result", "id": tc_id, "name": name, "result": result})
                    messages.append({
                        "role": "tool", "tool_call_id": tc_id, "name": name,
                        "content": json.dumps(result)[:8000],
                    })
                continue  # loop and let the LLM see the tool results.

            # No tool calls => final answer.
            final_text = response.content or ""
            messages.append({"role": "assistant", "content": final_text})
            if final_text:
                # Emit one token event with the full final text so SSE
                # consumers get something incremental even when the LLM
                # doesn't actually stream.
                await queue.put({"type": "token", "text": final_text})
            await queue.put({"type": "final", "text": final_text, "iterations": i + 1})
            return

        # Hit the iteration cap without a final answer.
        await queue.put({
            "type": "error",
            "stage": "loop",
            "detail": f"max_iterations ({max_iterations}) exceeded",
        })
    finally:
        await queue.put(None)  # sentinel for the SSE consumer


def _summarize(result: dict) -> str:
    s = json.dumps(result)[:500]
    return s


async def stream_events(queue: asyncio.Queue) -> AsyncIterator[dict]:
    """Yield events from ``queue`` until the sentinel ``None`` arrives."""
    while True:
        evt = await queue.get()
        if evt is None:
            return
        yield evt
