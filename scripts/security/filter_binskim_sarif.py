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


def load_suppressions(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("suppressions", [])


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

    suppressions = load_suppressions(supp_path)

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
        f"unsuppressed_fails_or_warnings={fails} -> {outp}"
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
