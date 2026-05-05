# Security tooling

## `filter_sarif.py`

Post-processes CodeQL SARIF output to honor inline `# lgtm[rule-id]`
(or `// lgtm[rule-id]`) suppression comments.

The CodeQL CLI itself does *not* honor lgtm comments — that was a feature
of the now-deprecated lgtm.com hosted product. This filter brings parity so
the SARIF we keep / upload to GitHub Code Scanning reflects the
post-remediation view.

Usage:

```bash
python scripts/security/filter_sarif.py input.sarif output.sarif <repo-root>
```

Matching rules:
- A finding is dropped when any line within ±3 lines of `startLine` in the
  reported source file contains a `lgtm[<rule>]` comment whose rule list
  includes the finding's rule id (multiple rules per line are supported,
  comma-separated or as separate `lgtm[...]` comments).
- The ±3 window accommodates multi-line statements where SARIF reports the
  format-string line vs. the args-continuation line.

CI integration: see `.github/workflows/codeql.yml`. The workflow runs
`codeql-action/analyze` with `upload: never`, then post-processes each
SARIF, then uploads the filtered result to Code Scanning.

## When to use `# lgtm[rule]` instead of fixing

- **Never** for high-severity findings (SSRF, SQLi, path-injection,
  clear-text-logging-sensitive-data, etc.). Always apply a real runtime
  guard for those — `lgtm` comments are only for marking the validated
  sink, not for hiding the issue.
- For lint-class findings (empty-except, log-injection on internal fields,
  unused-import in generated code, etc.) that have low real-world risk
  and a costly real fix, `lgtm[rule]` is acceptable so the report stays
  signal-rich.

## Conventions

- Place the comment on the same line CodeQL reports as `startLine`.
- For multi-rule sites, you may stack comments:
  `# lgtm[py/sql-injection]  # lgtm[py/log-injection]`
- Keep the runtime validator (`security_utils.py`) call on the same line
  as the suppressed sink so reviewers can see the guard at a glance.
