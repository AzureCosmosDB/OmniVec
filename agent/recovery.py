"""Runtime-enforced post-action verification, independent of LLM assertions."""
from __future__ import annotations

import asyncio

from .tools import diagnostics


MAX_ACTIONS = 2
MAX_VERIFICATION_SAMPLES = 2


def rejected(state: dict, tool_name: str, reason: str) -> None:
    state["last"] = {
        "outcome": "ACTION_NOT_EXECUTED", "tool": tool_name, "scope": "requested action",
        "attempt": state.get("attempts", 0), "max_actions": MAX_ACTIONS,
        "verification": {"status": "UNKNOWN", "processing_verified": False, "unknown": [reason]},
    }


def action_failed(result: dict) -> bool:
    return bool(result.get("error") or result.get("stub") or result.get("denied")
                or result.get("success") is False
                or result.get("status") in ("failed", "error", "unhealthy"))


async def execute_verified(execute, tool, args: dict, state: dict) -> dict:
    """Observe -> execute once -> observe at most twice; never manufacture success."""
    scope = args.get("pipeline_id")
    before = await diagnostics.collect_snapshot(scope)
    diagnosis = diagnostics.evaluate(before)
    state["attempts"] = state.get("attempts", 0) + 1
    try:
        result = await execute(tool, args)
    except Exception as exc:
        # Provider exception messages may contain credentials or signed URLs.
        result = {"error": f"Action failed ({type(exc).__name__}); inspect restricted service logs"}
    verification = {"status": "UNKNOWN", "processing_verified": False,
                    "unknown": ["Post-action verification not completed"]}
    if action_failed(result):
        outcome = "ACTION_FAILED"
    else:
        for _ in range(MAX_VERIFICATION_SAMPLES):
            await asyncio.sleep(diagnostics.SAMPLE_SECONDS)
            after = await diagnostics.collect_snapshot(scope)
            verification = diagnostics.evaluate(after, before)
            if verification["processing_verified"]:
                break
        outcome = "VERIFIED_PROCESSING" if verification["processing_verified"] else (
            "READY_IDLE" if verification["status"] == "READY_IDLE" else "NOT_VERIFIED"
        )
    record = {
        "outcome": outcome, "tool": tool.name, "scope": scope or "system",
        "attempt": state["attempts"], "max_actions": MAX_ACTIONS,
        "diagnosis_status": diagnosis["status"],
        "diagnosis": {"findings": diagnosis["findings"][:8], "unknown": diagnosis["unknown"]},
        "verification": verification,
        "preservation": "No automatic reset, purge, model replacement or checkpoint deletion.",
    }
    state["last"] = record
    return {"recovery": record, **result}


def final_result(state: dict) -> tuple[str, dict] | None:
    """Authoritative repair answer; unchecked model-generated success prose is discarded."""
    record = state.get("last")
    if not record:
        return None
    outcome = record["outcome"]
    verification = record["verification"]
    if outcome == "VERIFIED_PROCESSING":
        text = f"Verified processing recovery for {record['scope']}: destination progress increased and observed dependencies are healthy."
    elif outcome == "READY_IDLE":
        text = "Action executed. Observed dependencies are ready/idle, but no workload demonstrated processing; repair is NOT verified."
    elif outcome == "ACTION_FAILED":
        text = "The approved action failed. Repair is NOT verified; no automatic retry or destructive fallback was attempted."
    elif outcome == "ACTION_NOT_EXECUTED":
        text = "The requested action was not executed. Repair is NOT verified."
    else:
        text = "The action was attempted, but recovery is NOT verified. The bounded observation window did not establish healthy processing."
    findings = verification.get("findings", [])
    unknown = verification.get("unknown", [])
    if findings:
        text += " Next: " + findings[0]["next_action"]
    elif unknown:
        text += " Missing evidence: " + "; ".join(unknown[:3]) + "."
    if outcome != "VERIFIED_PROCESSING":
        text += " Escalate unresolved dependencies or re-observe after an authorized real workload; do not purge queues or reset checkpoints."
    return text, record
