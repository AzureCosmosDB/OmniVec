"""Tool-calling reasoning loop for the OmniVec Agent.

Phase 1 lifecycle:
  1. Build tool schemas (role-filtered) + initial messages (system + history + user).
  2. Call the LLM. If it returned tool_calls, validate, execute, append results,
     loop. Otherwise emit ``final`` and stop.
  3. Cap at MAX_ITERATIONS to prevent runaway loops.

Phase 2 — **approval gate** for mutating tools:
  * When the LLM proposes a tool with ``readonly=False`` and no
    pre-approved-call-id matches, the loop:
      - Parks a ``PendingApproval`` in ``approvals.get_approvals_store()``
        carrying the full messages-so-far (including the assistant turn that
        proposed the call) + the raw tool_call dict.
      - Emits ``{type: "approval_required", ...}`` and exits cleanly.
  * The HTTP layer's ``POST /v1/chat/approve`` later calls
    ``resume_after_approval`` which pops the pending record, executes (or
    records a synthetic denial result), and re-enters ``run_agent`` with the
    updated message history so the LLM produces a final answer.

All events are dicts of shape ``{"type": <token|tool_call|tool_result|
approval_required|final|error>, ...}``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable

from pydantic import ValidationError

from .approvals import PendingApproval, get_approvals_store
from .audit import AuditWriter
from .llm import LLMResponse, chat_completion
from .tools import Tool, get_tool, list_tools, validate_args
from .tools.mutations import danger_level


logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are the OmniVec Agent — an in-cluster ops assistant for the OmniVec "
    "platform. You answer operational questions ('why is pipeline X stuck?', "
    "'what is the dead-letter count?', 'show me docgrok-router logs') by "
    "calling the diagnostic tools available to you. "
    "IMPORTANT — mutating tools (pause/resume/restart/scale/retry/purge): "
    "when the user asks for one, CALL THE TOOL DIRECTLY. The system will "
    "automatically pause execution and show the operator an inline Approve/Deny "
    "card with the tool name, arguments, and danger level. Do NOT ask the user "
    "to confirm in chat — that creates a confusing double-confirmation. If the "
    "operator denies the call, you will receive a tool result with "
    "{denied: true, reason: ...}; in that case, acknowledge briefly and stop. "
    "If the operator approves, you will receive the real tool result and can "
    "continue. Plan first, call one tool at a time, then synthesize a concise "
    "answer that cites the tool outputs you used. Never invent data; if a tool "
    "fails, say so."
)


LLMCallable = Callable[[list[dict], list[dict] | None, str | None], Awaitable[LLMResponse]]


async def _default_llm(messages, tools, model_id) -> LLMResponse:
    return await chat_completion(messages, tools, model_id)


async def _execute_tool(t: Tool, raw_args: dict) -> dict:
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


def _build_initial_messages(history: list[dict], user_message: str) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages


async def run_agent(
    *,
    queue: asyncio.Queue,
    user_message: str | None,
    history: list[dict],
    role: str,
    model_id: str | None,
    caller_id: str,
    session_id: str,
    audit: AuditWriter | None = None,
    llm: LLMCallable | None = None,
    max_iterations: int = MAX_ITERATIONS,
    initial_messages: list[dict] | None = None,
    approved_call_ids: set[str] | None = None,
) -> None:
    """Drive the tool-calling loop and stream events to ``queue``.

    Args:
        user_message: New user turn (Phase 1 path). Ignored if
            ``initial_messages`` is supplied (resume path).
        initial_messages: When set, used verbatim as the starting message
            stack. Used by ``resume_after_approval`` to re-enter the loop
            after a human approves / denies a mutating tool.
        approved_call_ids: Set of tool_call IDs that have been pre-approved
            and may execute without re-prompting. Used during resume.
    """
    llm = llm or _default_llm
    approved_call_ids = set(approved_call_ids or ())
    tool_schemas, tools_by_name = _tools_for_role(role)

    if initial_messages is not None:
        messages = list(initial_messages)
    else:
        messages = _build_initial_messages(history, user_message or "")

    approvals = get_approvals_store()

    try:
        for i in range(max_iterations):
            try:
                response = await llm(messages, tool_schemas, model_id)
            except Exception as e:  # noqa: BLE001
                await queue.put({"type": "error", "stage": "llm", "detail": str(e)})
                return

            if response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": response.tool_calls,
                })

                # Walk the proposed calls. We may park on the very first
                # mutating call and stop emitting; remaining calls are
                # dropped and the LLM will see only the partial context on
                # resume (which is fine — it'll just re-propose).
                parked = False
                for tc in response.tool_calls:
                    tc_id = tc.get("id") or f"call_{i}"
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "")
                    try:
                        raw_args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        raw_args = {}

                    t = tools_by_name.get(name) or get_tool(name)

                    # Role / existence guard.
                    if t is None or (t.role == "admin" and role != "admin"):
                        await queue.put({
                            "type": "tool_call", "id": tc_id, "name": name, "args": raw_args,
                        })
                        err = {"error": f"tool '{name}' not available for role '{role}'"}
                        await queue.put({"type": "tool_result", "id": tc_id, "name": name, "result": err})
                        messages.append({
                            "role": "tool", "tool_call_id": tc_id, "name": name,
                            "content": json.dumps(err),
                        })
                        continue

                    # Approval gate — mutating tools need explicit human OK
                    # unless this specific call_id was pre-approved.
                    if not t.readonly and tc_id not in approved_call_ids:
                        summary = _summarize_proposal(name, raw_args)
                        pending = PendingApproval(
                            session_id=session_id, call_id=tc_id,
                            user_id=caller_id, role=role,
                            tool_name=name, args=raw_args,
                            danger_level=danger_level(name),
                            summary=summary,
                            history=list(messages),
                            tool_call=tc, model_id=model_id,
                        )
                        await approvals.put(pending)
                        await queue.put({
                            "type": "approval_required",
                            "call_id": tc_id, "tool": name, "args": raw_args,
                            "danger_level": pending.danger_level,
                            "summary": summary,
                        })
                        parked = True
                        break  # stop processing further tool_calls this turn

                    # Read-only OR pre-approved mutating call — execute.
                    await queue.put({
                        "type": "tool_call", "id": tc_id, "name": name, "args": raw_args,
                    })
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

                if parked:
                    return  # SSE consumer will see approval_required + done
                continue  # loop and let the LLM see the tool results.

            # No tool calls => final answer.
            final_text = response.content or ""
            messages.append({"role": "assistant", "content": final_text})
            if final_text:
                await queue.put({"type": "token", "text": final_text})
            await queue.put({"type": "final", "text": final_text, "iterations": i + 1})
            return

        await queue.put({
            "type": "error", "stage": "loop",
            "detail": f"max_iterations ({max_iterations}) exceeded",
        })
    finally:
        await queue.put(None)


async def resume_after_approval(
    *,
    queue: asyncio.Queue,
    pending: PendingApproval,
    decision: str,
    comment: str = "",
    caller_id: str,
    role: str,
    audit: AuditWriter | None = None,
    llm: LLMCallable | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> None:
    """Resume the loop after the operator approved or denied a mutating call.

    On **approve**: execute the parked tool, append its result, re-enter
    ``run_agent`` with the updated history. The same call_id is added to
    ``approved_call_ids`` to skip the gate (defence-in-depth — we don't
    actually re-traverse it because we execute it directly here).

    On **deny**: synthesize a ``{denied: true, reason: comment}`` tool_result
    so the LLM can read the denial and adapt (e.g. suggest a different
    action). The user-facing assistant message comes from the resumed loop.
    """
    decision = (decision or "").lower()
    if decision not in ("approve", "deny"):
        await queue.put({"type": "error", "stage": "approve", "detail": "invalid decision"})
        await queue.put(None)
        return

    messages = list(pending.history)
    tc_id = pending.call_id
    name = pending.tool_name

    if decision == "approve":
        t = get_tool(name)
        if t is None:
            result = {"error": f"tool '{name}' no longer registered"}
        elif t.role == "admin" and role != "admin":
            result = {"error": f"tool '{name}' requires admin role"}
        else:
            try:
                result = await _execute_tool(t, pending.args)
                if audit:
                    await audit.record(
                        session_id=pending.session_id, user=caller_id, role=role,
                        tool_name=name, args=pending.args,
                        result_summary=_summarize(result),
                        reasoning_trace_snippet=f"approved by {caller_id}: {comment[:200]}",
                    )
            except ValidationError as ve:
                result = {"error": "invalid arguments", "detail": ve.errors()}
            except Exception as e:  # noqa: BLE001
                logger.exception("approved tool %s failed", name)
                result = {"error": str(e)}
        await queue.put({"type": "tool_result", "id": tc_id, "name": name, "result": result})
        messages.append({
            "role": "tool", "tool_call_id": tc_id, "name": name,
            "content": json.dumps(result)[:8000],
        })
    else:  # deny
        result = {"denied": True, "reason": comment or "operator denied"}
        if audit:
            await audit.record(
                session_id=pending.session_id, user=caller_id, role=role,
                tool_name=name, args=pending.args,
                result_summary="denied",
                reasoning_trace_snippet=f"denied by {caller_id}: {comment[:200]}",
            )
        await queue.put({"type": "tool_result", "id": tc_id, "name": name, "result": result})
        messages.append({
            "role": "tool", "tool_call_id": tc_id, "name": name,
            "content": json.dumps(result),
        })

    await run_agent(
        queue=queue,
        user_message=None,
        history=[],
        role=role,
        model_id=pending.model_id,
        caller_id=caller_id,
        session_id=pending.session_id,
        audit=audit,
        llm=llm,
        max_iterations=max_iterations,
        initial_messages=messages,
        approved_call_ids={tc_id},
    )


def _summarize(result: dict) -> str:
    return json.dumps(result)[:500]


def _summarize_proposal(name: str, args: dict) -> str:
    """Short human-readable description shown on the Approve / Deny card."""
    arg_str = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return f"{name}({arg_str})"[:300]


async def stream_events(queue: asyncio.Queue) -> AsyncIterator[dict]:
    while True:
        evt = await queue.get()
        if evt is None:
            return
        yield evt
