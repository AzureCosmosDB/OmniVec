"""Filter CodeQL SARIF results: drop findings on lines that carry an
inline ``# lgtm[rule-id]`` (or ``// lgtm[rule-id]``) suppression comment.

The CodeQL CLI does not honor lgtm suppressions in SARIF output (only the
hosted lgtm.com platform does). This filter brings parity so the SARIF we
keep in CI is the post-remediation view.

Usage::

    python filter_sarif.py input.sarif output.sarif [repo-root]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LGTM_RE = re.compile(r"lgtm\s*\[\s*([^\]]+)\s*\]")


def line_suppresses(rule_id: str, source_line: str) -> bool:
    matches = LGTM_RE.findall(source_line or "")
    if not matches:
        return False
    suppressed: set[str] = set()
    for m in matches:
        for tok in m.split(","):
            tok = tok.strip()
            if tok:
                suppressed.add(tok)
    return rule_id in suppressed


def _read_lines_window(fpath: Path, line_no: int, window: int = 3) -> list[str]:
    """Return up to ``window`` lines centred on ``line_no`` (1-based)."""
    out: list[str] = []
    lo, hi = max(1, line_no - window), line_no + window
    try:
        with fpath.open("r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f, start=1):
                if lo <= i <= hi:
                    out.append(raw)
                if i > hi:
                    break
    except OSError:
        pass
    return out


def filter_sarif(sarif_path: Path, root: Path) -> tuple[dict, int, int]:
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    kept_total = 0
    dropped_total = 0
    for run in data.get("runs", []):
        kept = []
        for r in run.get("results", []):
            rid = r.get("ruleId", "")
            locs = r.get("locations") or []
            suppressed = False
            for loc in locs:
                phys = loc.get("physicalLocation") or {}
                uri = (phys.get("artifactLocation") or {}).get("uri")
                line_no = (phys.get("region") or {}).get("startLine")
                if not uri or not line_no:
                    continue
                fpath = root / uri
                window = _read_lines_window(fpath, line_no, window=3)
                if any(line_suppresses(rid, w) for w in window):
                    suppressed = True
                if suppressed:
                    break
            if suppressed:
                dropped_total += 1
            else:
                kept.append(r)
        run["results"] = kept
        kept_total += len(kept)
    return data, kept_total, dropped_total


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    root = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".")
    data, kept, dropped = filter_sarif(inp, root)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"kept={kept}  dropped={dropped}  -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
