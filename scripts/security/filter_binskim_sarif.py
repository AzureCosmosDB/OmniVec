#!/usr/bin/env python3
"""
Filter BinSkim SARIF: drop results whose ruleId+binary appears in
scripts/security/binskim_suppressions.json (documented upstream-toolchain
issues). Marks them as suppressed/justified rather than removing silently
so the audit trail is preserved.

Usage:
    python filter_binskim_sarif.py <input.sarif> <output.sarif> [suppressions.json]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_suppressions(path: Path) -> tuple[list[dict], list[dict]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("suppressions", []), data.get("invocation_notifications", [])


def matches(result: dict, supp: dict) -> bool:
    if result.get("ruleId") != supp.get("ruleId"):
        return False
    binary = supp.get("binary", "")
    if not binary:
        return True
    for loc in result.get("locations", []) or []:
        uri = (
            loc.get("physicalLocation", {})
            .get("artifactLocation", {})
            .get("uri", "")
        )
        if uri.lower().endswith(binary.lower()):
            return True
    return False


def _resolve_message_text(result: dict, rule: dict | None) -> str:
    """Resolve SARIF message.text from message.id + arguments when the tool
    only emitted a message reference. GitHub Code Scanning requires
    message.text directly; missing text yields the upload error
    "expected a result message".
    """
    msg = result.get("message") or {}
    text = msg.get("text")
    if text:
        return text
    msg_id = msg.get("id")
    args = msg.get("arguments") or []
    template = ""
    if rule and msg_id:
        strings = rule.get("messageStrings") or {}
        entry = strings.get(msg_id) or {}
        template = entry.get("text") or ""
    if not template:
        # Fall back to a deterministic placeholder so SARIF stays uploadable.
        return f"{result.get('ruleId', 'rule')}: {msg_id or 'no message text'}"
    try:
        # SARIF templates use {0}, {1}, ... placeholders.
        return template.format(*args)
    except (IndexError, KeyError):
        return template


def _ensure_message_text(sarif: dict) -> int:
    """Walk all results and synthesize message.text if missing. Returns
    the number of results that were patched."""
    patched = 0
    for run in sarif.get("runs", []) or []:
        rules = ((run.get("tool") or {}).get("driver") or {}).get("rules") or []
        for r in run.get("results", []) or []:
            msg = r.get("message") or {}
            if msg.get("text"):
                continue
            idx = r.get("ruleIndex")
            rule = None
            if isinstance(idx, int) and 0 <= idx < len(rules):
                rule = rules[idx]
            if rule is None:
                rid = r.get("ruleId")
                rule = next((x for x in rules if x.get("id") == rid), None)
            msg["text"] = _resolve_message_text(r, rule)
            r["message"] = msg
            patched += 1
    return patched


def _notification_matches(notif: dict, supp: dict) -> bool:
    """Match a SARIF toolConfigurationNotification against a suppression entry."""
    desc_id = (notif.get("descriptor") or {}).get("id") or notif.get("ruleId")
    if desc_id != supp.get("descriptorId"):
        return False
    binary = supp.get("binary", "")
    if not binary:
        return True
    for loc in notif.get("locations", []) or []:
        uri = (
            loc.get("physicalLocation", {})
            .get("artifactLocation", {})
            .get("uri", "")
        )
        if uri.lower().endswith(binary.lower()):
            return True
    return False


def _normalize_invocations(sarif: dict, notif_suppressions: list[dict]) -> int:
    """Demote accepted invocation notifications to ``note`` (with audit-trail
    suppression annotation) and flip ``executionSuccessful`` back to True when
    no error-level notifications remain. Returns the number of notifications
    that were demoted."""
    demoted = 0
    for run in sarif.get("runs", []) or []:
        for inv in run.get("invocations", []) or []:
            for notif in inv.get("toolConfigurationNotifications", []) or []:
                if notif.get("level") not in ("error", "warning"):
                    continue
                matched = next(
                    (s for s in notif_suppressions if _notification_matches(notif, s)),
                    None,
                )
                if not matched:
                    continue
                notif["level"] = "note"
                notif.setdefault("suppressions", []).append(
                    {
                        "kind": "external",
                        "status": "accepted",
                        "justification": matched.get("reason", ""),
                    }
                )
                demoted += 1
            remaining_errors = sum(
                1
                for n in inv.get("toolConfigurationNotifications", []) or []
                if n.get("level") in ("error", "warning")
            )
            if remaining_errors == 0:
                inv["executionSuccessful"] = True
    return demoted


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    inp = Path(argv[1])
    outp = Path(argv[2])
    supp_path = (
        Path(argv[3])
        if len(argv) > 3
        else Path(__file__).with_name("binskim_suppressions.json")
    )

    suppressions, notif_suppressions = load_suppressions(supp_path)

    with inp.open("r", encoding="utf-8") as fh:
        sarif = json.load(fh)

    kept = 0
    dropped = 0
    for run in sarif.get("runs", []) or []:
        new_results = []
        for r in run.get("results", []) or []:
            matched = next(
                (s for s in suppressions if matches(r, s)),
                None,
            )
            if matched:
                # Annotate as suppressed with justification for audit trail.
                r.setdefault("suppressions", []).append(
                    {
                        "kind": "external",
                        "status": "accepted",
                        "justification": matched.get("reason", ""),
                    }
                )
                # Demote to "Note" so it doesn't fail the gate.
                r["level"] = "note"
                new_results.append(r)
                dropped += 1
            else:
                new_results.append(r)
                kept += 1
        run["results"] = new_results

    outp.parent.mkdir(parents=True, exist_ok=True)
    patched = _ensure_message_text(sarif)
    notif_demoted = _normalize_invocations(sarif, notif_suppressions)
    with outp.open("w", encoding="utf-8") as fh:
        json.dump(sarif, fh, indent=2)

    fails = sum(
        1
        for run in sarif.get("runs", []) or []
        for r in run.get("results", []) or []
        if r.get("level") in ("error", "warning") and not r.get("suppressions")
    )
    print(
        f"BinSkim filter: kept={kept} suppressed={dropped} "
        f"unsuppressed_fails_or_warnings={fails} message_text_patched={patched} "
        f"notifications_demoted={notif_demoted} -> {outp}"
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
