"""Render BinSkim + CodeQL SARIF files into a single self-contained HTML page.

Usage (run from repo root):
    python scripts/sarif_to_html.py

Output: security-report.html (next to the SARIF files at repo root).
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import pathlib
import sys
from collections import Counter
from typing import Any, Iterable

REPO = pathlib.Path(__file__).resolve().parent.parent
INPUTS = [
    ("BinSkim — .NET binaries", REPO / "binskim-result.sarif"),
    ("CodeQL — Python", REPO / "codeql-python.sarif"),
    ("CodeQL — C#", REPO / "codeql-csharp.sarif"),
]
OUT = REPO / "security-report.html"


def _bucket(sev: float | None, level: str | None) -> str:
    if sev is not None:
        if sev >= 9.0:
            return "critical"
        if sev >= 7.0:
            return "high"
        if sev >= 4.0:
            return "medium"
        if sev > 0:
            return "low"
    if level == "error":
        return "high"
    if level == "warning":
        return "medium"
    return "note"


BUCKET_ORDER = ["critical", "high", "medium", "low", "note"]
BUCKET_COLOR = {
    "critical": "#b91c1c",
    "high":     "#dc2626",
    "medium":   "#d97706",
    "low":      "#0891b2",
    "note":     "#64748b",
}


def _load(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(sarif: dict[str, Any]) -> tuple[list[dict], dict[str, dict]]:
    """Return (results, rules-by-id)."""
    run = sarif["runs"][0]
    rules = {r["id"]: r for r in run.get("tool", {}).get("driver", {}).get("rules", []) or []}
    out = []
    for res in run.get("results", []) or []:
        rule = rules.get(res.get("ruleId"), {})
        sev_raw = (rule.get("properties") or {}).get("security-severity")
        try:
            sev = float(sev_raw) if sev_raw is not None else None
        except (TypeError, ValueError):
            sev = None
        loc = (res.get("locations") or [{}])[0].get("physicalLocation", {}) or {}
        af = loc.get("artifactLocation", {}) or {}
        region = loc.get("region", {}) or {}
        out.append({
            "rule": res.get("ruleId", ""),
            "name": (rule.get("shortDescription") or {}).get("text") or res.get("ruleId", ""),
            "kind": res.get("kind") or "fail",
            "level": res.get("level"),
            "sev": sev,
            "bucket": _bucket(sev, res.get("level")),
            "uri": af.get("uri", ""),
            "line": region.get("startLine"),
            "msg": (res.get("message") or {}).get("text", "").strip(),
            "rule_help": (rule.get("help") or {}).get("text", "")
                          or (rule.get("fullDescription") or {}).get("text", ""),
        })
    return out, rules


def _section(title: str, sarif: dict[str, Any] | None) -> str:
    if sarif is None:
        return f"<section><h2>{html.escape(title)}</h2><p><em>(report file missing)</em></p></section>"
    results, rules = _flatten(sarif)
    # Drop pass + notApplicable for the visible report; keep counts in the meta header.
    visible = [r for r in results if r["kind"] not in ("pass", "notApplicable", "informational")]
    counts = Counter(r["bucket"] for r in visible)
    total_kind = Counter(r["kind"] for r in results)
    by_rule = Counter(r["rule"] for r in visible)

    header_chips = " ".join(
        f'<span class="chip" style="background:{BUCKET_COLOR[b]}">{b}: {counts.get(b, 0)}</span>'
        for b in BUCKET_ORDER
    )
    pass_chip = (
        f'<span class="chip pass">pass: {total_kind.get("pass", 0)}</span>'
        f'<span class="chip na">notApplicable: {total_kind.get("notApplicable", 0)}</span>'
    )

    if not visible:
        body = '<p class="ok">✅ No actionable findings in this report.</p>'
    else:
        # Group by bucket, severity desc inside.
        rows: list[str] = []
        for b in BUCKET_ORDER:
            bucket_items = sorted(
                [r for r in visible if r["bucket"] == b],
                key=lambda r: (-(r["sev"] or 0), r["rule"], r["uri"], r["line"] or 0),
            )
            if not bucket_items:
                continue
            rows.append(
                f'<tr class="bucket-row"><td colspan="5"><strong style="color:{BUCKET_COLOR[b]}">'
                f'{b.upper()} ({len(bucket_items)})</strong></td></tr>'
            )
            for r in bucket_items:
                sev = f'{r["sev"]:.1f}' if r["sev"] is not None else ''
                loc = html.escape(r["uri"])
                if r["line"]:
                    loc += f':{r["line"]}'
                rows.append(
                    "<tr>"
                    f'<td><span class="sev" style="color:{BUCKET_COLOR[b]}">{sev}</span></td>'
                    f'<td><code>{html.escape(r["rule"])}</code></td>'
                    f'<td>{html.escape(r["name"])}</td>'
                    f'<td><code class="loc">{loc}</code></td>'
                    f'<td class="msg">{html.escape(r["msg"][:300])}{"…" if len(r["msg"])>300 else ""}</td>'
                    "</tr>"
                )
        # Top rules summary
        top_rules = "".join(
            f'<li><code>{html.escape(rid)}</code> &mdash; {n}</li>'
            for rid, n in by_rule.most_common(8)
        )
        body = (
            f'<details open><summary><strong>Top rules</strong></summary>'
            f'<ul class="toprules">{top_rules}</ul></details>'
            '<table class="findings"><thead><tr>'
            '<th>Sev</th><th>Rule</th><th>Description</th><th>Location</th><th>Message</th>'
            '</tr></thead><tbody>'
            + "\n".join(rows) + "</tbody></table>"
        )

    return (
        f'<section><h2>{html.escape(title)}</h2>'
        f'<div class="chips">{header_chips} {pass_chip}'
        f' <span class="chip total">total findings (visible): {len(visible)}</span></div>'
        f'{body}</section>'
    )


def main() -> int:
    sections = []
    grand: Counter[str] = Counter()
    for title, path in INPUTS:
        sarif = _load(path)
        if sarif is not None:
            results, _ = _flatten(sarif)
            for r in results:
                if r["kind"] not in ("pass", "notApplicable", "informational"):
                    grand[r["bucket"]] += 1
        sections.append(_section(title, sarif))

    grand_chips = " ".join(
        f'<span class="chip" style="background:{BUCKET_COLOR[b]}">{b}: {grand.get(b, 0)}</span>'
        for b in BUCKET_ORDER
    )
    head_total = sum(grand.values())
    crit_high = grand.get("critical", 0) + grand.get("high", 0)
    headline_status = (
        f'<span class="ok">✅ 0 critical/high findings</span>' if crit_high == 0
        else f'<span class="bad">❌ {crit_high} critical/high findings</span>'
    )

    css = """
    :root { color-scheme: light dark; }
    body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           margin: 0; padding: 28px 40px; max-width: 1400px; }
    h1 { margin: 0 0 8px; }
    h2 { margin: 32px 0 8px; padding-bottom: 6px; border-bottom: 1px solid #888; }
    .meta { color: #666; font-size: 12px; }
    .chips { margin: 10px 0 14px; }
    .chip { display: inline-block; padding: 3px 10px; border-radius: 12px; color: white;
            font-size: 12px; margin-right: 4px; font-weight: 500; }
    .chip.pass { background: #16a34a; }
    .chip.na   { background: #94a3b8; }
    .chip.total{ background: #1e293b; }
    .ok  { color: #16a34a; font-weight: 600; }
    .bad { color: #dc2626; font-weight: 600; }
    .headline { font-size: 18px; padding: 12px 16px; border: 2px solid; border-radius: 8px;
                margin: 12px 0 24px; }
    table.findings { border-collapse: collapse; width: 100%; margin-top: 8px; }
    .findings th, .findings td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #d1d5db;
                                 vertical-align: top; }
    .findings th { background: #f1f5f9; font-size: 12px; }
    .findings tr.bucket-row td { background: #f8fafc; padding: 10px; border-top: 2px solid #cbd5e1; }
    .findings code { background: #f1f5f9; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
    .findings code.loc { color: #475569; }
    .findings .sev { font-weight: 700; }
    .findings .msg { color: #475569; max-width: 480px; }
    ul.toprules { margin: 4px 0 12px; columns: 2; max-width: 720px; }
    ul.toprules li { break-inside: avoid; margin: 2px 0; }
    @media (prefers-color-scheme: dark) {
      body { background: #0b1220; color: #e2e8f0; }
      .findings th { background: #1e293b; color: #cbd5e1; }
      .findings tr.bucket-row td { background: #111827; }
      .findings code { background: #1e293b; color: #e2e8f0; }
      .findings .msg { color: #94a3b8; }
      .findings th, .findings td { border-bottom-color: #334155; }
      h2 { border-color: #334155; }
    }
    """

    now = _dt.datetime.now().isoformat(timespec="seconds")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>OmniVec — Security Scan Report</title>
<style>{css}</style></head><body>
<h1>OmniVec — Security Scan Report</h1>
<p class="meta">Generated {html.escape(now)} · sources: BinSkim + CodeQL (Python &amp; C#)</p>
<div class="headline" style="border-color:{'#16a34a' if crit_high == 0 else '#dc2626'}">
  {headline_status} &nbsp;·&nbsp; {head_total} actionable findings across {len(INPUTS)} scans
</div>
<div class="chips">{grand_chips}</div>
{''.join(sections)}
<footer style="margin-top:40px; color:#64748b; font-size:12px;">
  Raw SARIF: <code>binskim-result.sarif</code>, <code>codeql-python.sarif</code>,
  <code>codeql-csharp.sarif</code> &nbsp;·&nbsp;
  Suppression policy: <code>.github/codeql/codeql-config.yml</code>
</footer>
</body></html>
"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes; total findings: {head_total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
