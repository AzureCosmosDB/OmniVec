#!/bin/sh
# Static-analysis regression test for a4 (stdin guards).
#
# Azd hooks run with stdin piped from azd. Nested `az`/`azd`/`kubectl`/`helm`/
# `git`/`curl` calls inherit that stdin unless explicitly redirected. Without
# `</dev/null` they can:
#   - consume the hook's own stdin (eating pipe data in while-read loops)
#   - hang waiting for input on prompts that never come
#
# Emit a line for each call site that lacks a stdin redirection OR a
# `# stdin-ok` opt-out marker. Fail if any found. Known-safe patterns
# (right-hand side of `|`, `apply -f -`, comments, heredocs) are skipped.
#
# Works under dash, bash, sh.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

printf '\n=== stdin-guard static analysis ===\n'

issues_file=$(mktemp 2>/dev/null || mktemp -t stdin-scan)
trap 'rm -f "$issues_file"' EXIT

for f in "$REPO_ROOT"/hooks/*.sh; do
    [ -f "$f" ] || continue
    rel=${f#$REPO_ROOT/}
    printf '  scanning %s\n' "$rel"

    # awk pipeline: emit risky lines only. Much faster than a pure-shell loop.
    awk -v REL="$rel" '
        # Strip leading whitespace for prefix checks.
        {
            line = $0
            stripped = line
            sub(/^[ \t]+/, "", stripped)
        }

        # Skip comments / blanks.
        stripped == "" { next }
        stripped ~ /^#/ { next }

        # Skip if explicit opt-out marker or existing stdin redirect is present.
        line ~ /# stdin-ok/        { next }
        line ~ /<[ ]*\/dev\/null/  { next }
        line ~ /<</               { next }

        # Skip common stdin-is-the-input patterns.
        line ~ /apply[ ]+-f[ ]+-/    { next }
        line ~ /create[ ]+-f[ ]+-/   { next }
        line ~ /replace[ ]+-f[ ]+-/  { next }
        line ~ /delete[ ]+-f[ ]+-/   { next }

        # Skip line-continuation (we only check complete logical lines).
        line ~ /\\$/ { next }

        # Look for risky command as first non-trivial token on a command segment.
        # Segments are separated by ;, &&, ||, (, {. Pipe handling below.
        {
            # Replace known segment separators with newlines to check each segment.
            segs = line
            gsub(/\|\|/, "\n", segs)
            gsub(/&&/,   "\n", segs)
            gsub(/;/,    "\n", segs)
            gsub(/\(/,   "\n", segs)
            n = split(segs, arr, "\n")
            for (i = 1; i <= n; i++) {
                seg = arr[i]
                sub(/^[ \t]+/, "", seg)
                # Skip segments that are the RHS of a pipe (stdin = pipe data).
                if (match(seg, /^\|[ ]/) || match(seg, /^\| /)) continue
                if (seg ~ /^\|/) continue

                # Pull first token.
                first = seg
                sub(/[ \t].*$/, "", first)

                if (first == "az" || first == "azd" || first == "kubectl" \
                 || first == "helm" || first == "curl" || first == "git") {
                    # Check if THIS segment has a stdin redirect.
                    if (seg ~ /<[ ]*\/dev\/null/) continue
                    if (seg ~ /# stdin-ok/) continue
                    printf("    %s:%d  %s\n", REL, NR, stripped)
                    next
                }
            }
        }
    ' "$f" >> "$issues_file"
done

if [ -s "$issues_file" ]; then
    count=$(wc -l < "$issues_file" | tr -d ' ')
    cat "$issues_file"
    fail "$count call site(s) missing stdin guard" "add </dev/null or '# stdin-ok' marker"
else
    pass "all hook shell calls have explicit stdin redirection or # stdin-ok marker"
fi

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
