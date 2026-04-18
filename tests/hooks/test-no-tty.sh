#!/bin/sh
# Functional test for a2 (no-TTY fast-fail + non-interactive mode).
#
# We can't run the real preprovision.sh here (it would need az login, azd,
# a real Azure subscription, etc.), so we extract the a2 helpers into a
# stub harness, stub out azd/az, and assert behavior under three stdin
# configurations:
#   1. TTY attached           → helpers allow continuation
#   2. No TTY, NONINTERACTIVE → helpers auto-apply defaults and exit 0
#   3. No TTY, no NONINTERACTIVE → helpers fail fast with clear error
#
# Works under dash, bash, sh.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
PREPROVISION="$REPO_ROOT/hooks/preprovision.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t omnivec-a2)
trap 'rm -rf "$TMP"' EXIT

printf '\n=== a2: no-TTY / non-interactive tests ===\n'

# Verify helpers exist in preprovision.sh (catches refactoring regressions).
for fn in require_tty_or_preset is_noninteractive apply_quickstart_defaults; do
    if grep -q "^${fn}()" "$PREPROVISION" 2>/dev/null \
    || grep -q "^${fn}()[[:space:]]*{" "$PREPROVISION" 2>/dev/null; then
        pass "preprovision.sh defines ${fn}()"
    else
        fail "preprovision.sh missing ${fn}()"
    fi
done

# Extract the three helpers into a fragment we can source in isolation.
# They occupy a contiguous block near the top of the file.
python_or_awk_extract() {
    # Extract _can_prompt + is_noninteractive + apply_quickstart_defaults +
    # require_tty_or_preset from preprovision.sh. Single-print state machine.
    awk '
        /^_can_prompt\(\)/             { inblock=1 }
        /^require_tty_or_preset\(\)/   { inrt=1 }
        inblock { print }
        inrt && /^}[[:space:]]*$/      { exit }
    ' "$PREPROVISION"
}

FRAG="$TMP/a2.sh"
{
    printf '%s\n' '#!/bin/sh'
    printf '%s\n' 'set -u'
    # Color vars used inside helpers.
    printf '%s\n' 'GREEN="" RED="" YELLOW="" CYAN="" NC=""'
    # Stub azd: record env sets into $TMP/azdenv.
    printf '%s\n' 'azd() {'
    printf '%s\n' '  if [ "$1" = "env" ] && [ "$2" = "set" ]; then'
    printf '%s\n' '    printf "%s=%s\n" "$3" "$4" >> "$AZDENV_FILE"'
    printf '%s\n' '    return 0'
    printf '%s\n' '  fi'
    printf '%s\n' '  return 0'
    printf '%s\n' '}'
    python_or_awk_extract
} > "$FRAG"

# Sanity: fragment extracted and parses.
if sh -n "$FRAG" 2>"$TMP/syn.err"; then
    pass "extracted helper fragment parses cleanly"
else
    fail "helper fragment syntax error" "$(cat "$TMP/syn.err")"
fi

# --- Case 1: non-interactive mode applies Quick-start defaults ---
(
    export AZDENV_FILE="$TMP/env1"
    : > "$AZDENV_FILE"
    export OMNIVEC_NONINTERACTIVE=1
    export OMNIVEC_FORCE_NO_TTY=1
    out=$(sh -c ". $FRAG; require_tty_or_preset; echo POSTCALL" </dev/null 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ] \
       && ! printf '%s' "$out" | grep -q POSTCALL \
       && grep -q 'OMNIVEC_SYSTEM_NODE_VM_SIZE=Standard_B4ms' "$AZDENV_FILE" \
       && grep -q 'OMNIVEC_ENABLE_BLOB_SOURCE=true' "$AZDENV_FILE"; then
        exit 0
    fi
    printf 'rc=%s\nout=%s\nenv=%s\n' "$rc" "$out" "$(cat "$AZDENV_FILE")" >&2
    exit 1
) && pass "OMNIVEC_NONINTERACTIVE=1 + no TTY → auto-applies defaults and exits 0" \
  || fail "non-interactive mode"

# --- Case 2: CI=true also triggers non-interactive ---
(
    export AZDENV_FILE="$TMP/env2"
    : > "$AZDENV_FILE"
    unset OMNIVEC_NONINTERACTIVE AZD_NONINTERACTIVE GITHUB_ACTIONS
    export CI=true
    export OMNIVEC_FORCE_NO_TTY=1
    out=$(sh -c ". $FRAG; require_tty_or_preset; echo POSTCALL" </dev/null 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ] && grep -q 'OMNIVEC_GPU_NODE_COUNT=0' "$AZDENV_FILE"; then
        exit 0
    fi
    printf 'rc=%s\nout=%s\n' "$rc" "$out" >&2
    exit 1
) && pass "CI=true + no TTY → auto-applies defaults" \
  || fail "CI env triggers non-interactive"

# --- Case 3: No TTY, no flag → fast fail with helpful message ---
(
    export AZDENV_FILE="$TMP/env3"
    : > "$AZDENV_FILE"
    unset OMNIVEC_NONINTERACTIVE AZD_NONINTERACTIVE CI GITHUB_ACTIONS
    export OMNIVEC_FORCE_NO_TTY=1
    out=$(sh -c ". $FRAG; require_tty_or_preset; echo POSTCALL" </dev/null 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ] \
       && ! printf '%s' "$out" | grep -q POSTCALL \
       && printf '%s' "$out" | grep -q 'No TTY' \
       && printf '%s' "$out" | grep -q 'OMNIVEC_NONINTERACTIVE' \
       && printf '%s' "$out" | grep -q 'azd env set'; then
        exit 0
    fi
    printf 'rc=%s\nout=%s\n' "$rc" "$out" >&2
    exit 1
) && pass "No TTY + no flag → exits non-zero with actionable error" \
  || fail "no-TTY fast-fail"

# --- Case 4: read_input returns empty when no TTY and doesn't hang ---
(
    awk '
        /^_can_prompt\(\)/ { inblock=1 }
        /^read_input\(\)/  { inri=1 }
        inblock { print }
        inri && /^}[[:space:]]*$/ { exit }
    ' "$PREPROVISION" > "$TMP/readinput.sh"
    {
        printf '%s\n' '#!/bin/sh'
        printf '%s\n' 'set -u'
        cat "$TMP/readinput.sh"
        printf '%s\n' 'val=$(read_input "prompt: ")'
        printf '%s\n' 'printf "RESULT=[%s]\n" "$val"'
    } > "$TMP/ri-driver.sh"
    out=$(OMNIVEC_FORCE_NO_TTY=1 sh "$TMP/ri-driver.sh" </dev/null 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'RESULT=\[\]'; then
        exit 0
    fi
    printf 'rc=%s\nout=%s\n' "$rc" "$out" >&2
    exit 1
) && pass "read_input with no TTY returns empty string without hanging" \
  || fail "read_input no-TTY behavior"

# --- Case 5: stdin-guard on get-helm-3 curl|sh line ---
if grep -q 'get-helm-3.*</dev/null.*| .*sh .*</dev/null' "$PREPROVISION"; then
    pass "helm install pipeline has </dev/null on both curl and sh"
else
    fail "helm install pipeline missing </dev/null guards"
fi

# --- Case 6: git submodule update has stdin guard ---
if grep -q 'git submodule update.*</dev/null' "$PREPROVISION"; then
    pass "git submodule update has </dev/null"
else
    fail "git submodule update missing </dev/null"
fi

# --- Case 7: kubectl calls inside while-read loop in postprovision have guards ---
POSTP="$REPO_ROOT/hooks/postprovision.sh"
if [ -f "$POSTP" ]; then
    # Expect BOTH `describe pod` and `logs` inside the failure-diagnostic loop
    # to have explicit </dev/null, to prevent pipe-stdin collision.
    if grep -q 'kubectl.*describe pod.*</dev/null' "$POSTP" \
       && grep -q 'kubectl.*logs.*--tail=80.*</dev/null' "$POSTP"; then
        pass "postprovision while-read loop: inner kubectl calls have </dev/null"
    else
        fail "postprovision inner kubectl calls missing </dev/null (breaks loop)"
    fi
fi

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
