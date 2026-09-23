#!/bin/sh
# tests/hooks/test-retry.sh — verify retry_run wraps transient failures.

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
WORK_DIR="$SCRIPT_DIR/.retry-test-$$"
(umask 077 && mkdir "$WORK_DIR") || exit 1
trap 'rm -rf "$WORK_DIR"' EXIT
export TMPDIR="$WORK_DIR"
# shellcheck source=../../hooks/lib/retry.sh
. "$REPO_ROOT/hooks/lib/retry.sh"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  FAIL %s — %s\n" "$1" "$2"; }

# Silence retry logs during tests
export OMNIVEC_RETRY_QUIET=1
# Short delay so tests don't take 10s
export OMNIVEC_RETRY_ATTEMPTS=3
export OMNIVEC_RETRY_BASE_SEC=0

# ── 1. Succeeds on first try ────────────────────────────────────────────────
_rc=0
retry_run "noop" -- true || _rc=$?
[ "$_rc" -eq 0 ] && ok "succeeds on first try" || bad "succeeds on first try" "rc=$_rc"

# ── 2. Fails fast on non-transient error ────────────────────────────────────
T="$WORK_DIR/nontransient"; : > "$T"
fail_nontransient() { printf 'some fatal error\n' >&2; echo "x" >> "$T"; return 1; }
_rc=0
retry_run "non-transient" -- fail_nontransient >/dev/null 2>&1 || _rc=$?
_n=$(wc -l < "$T" | tr -d ' ')
[ "$_rc" -ne 0 ] && [ "$_n" = "1" ] && ok "no retry on non-transient" || bad "no retry on non-transient" "rc=$_rc calls=$_n"

# ── 3. Retries on transient error then succeeds ─────────────────────────────
T2="$WORK_DIR/flaky"; echo 0 > "$T2"
flaky_transient() {
    n=$(cat "$T2")
    n=$((n+1)); echo "$n" > "$T2"
    if [ "$n" -lt 2 ]; then
        echo "429 TooManyRequests" >&2
        return 1
    fi
    return 0
}
_rc=0
retry_run "flaky" -- flaky_transient >/dev/null 2>&1 || _rc=$?
_n=$(cat "$T2")
[ "$_rc" -eq 0 ] && [ "$_n" -ge 2 ] && ok "retries on transient 429" || bad "retries on transient 429" "rc=$_rc calls=$_n"

# ── 4. Gives up after max attempts ──────────────────────────────────────────
T3="$WORK_DIR/always"; echo 0 > "$T3"
always_transient() {
    n=$(cat "$T3"); n=$((n+1)); echo "$n" > "$T3"
    echo "503 ServiceUnavailable" >&2
    return 1
}
_rc=0
retry_run "always" -- always_transient >/dev/null 2>&1 || _rc=$?
_n=$(cat "$T3")
[ "$_rc" -ne 0 ] && [ "$_n" = "3" ] && ok "gives up after max attempts" || bad "gives up after max attempts" "rc=$_rc calls=$_n"

rm -f "$T" "$T2" "$T3"

# ── 5. Excerpt is shown to stderr on transient retry ────────────────────────
T4="$WORK_DIR/excerpt"; echo 0 > "$T4"
flaky_with_msg() {
    n=$(cat "$T4"); n=$((n+1)); echo "$n" > "$T4"
    if [ "$n" -lt 2 ]; then
        echo "kubectl error: 429 TooManyRequests" >&2
        echo "  details: throttled by API server" >&2
        return 1
    fi
    return 0
}
ERR="$WORK_DIR/excerpt.err"
retry_run "excerpt-test" -- flaky_with_msg >/dev/null 2>"$ERR"
if grep -q '\-\-\-\- last output \-\-\-\-' "$ERR" && grep -q 'TooManyRequests' "$ERR"; then
    ok "excerpt printed on transient retry"
else
    bad "excerpt printed on transient retry" "stderr did not contain excerpt"
fi
rm -f "$T4" "$ERR"

# ── 6. Heartbeat fires while a long-running command is executing ────────────
export OMNIVEC_RETRY_HEARTBEAT_SEC=1
export OMNIVEC_RETRY_HEARTBEAT_CMD='echo "    sentinel-pod  Running"'
slow_ok() { sleep 3; return 0; }
HB_ERR="$WORK_DIR/heartbeat.err"
retry_run "hb-test" -- slow_ok >/dev/null 2>"$HB_ERR"
if grep -q 'still running' "$HB_ERR" && grep -q 'sentinel-pod' "$HB_ERR"; then
    ok "heartbeat emitted during long-running cmd"
else
    bad "heartbeat emitted during long-running cmd" "stderr missing heartbeat"
fi
unset OMNIVEC_RETRY_HEARTBEAT_CMD OMNIVEC_RETRY_HEARTBEAT_SEC
rm -f "$HB_ERR"

# ── 7. Heartbeat does NOT fire for sub-interval commands ────────────────────
export OMNIVEC_RETRY_HEARTBEAT_SEC=5
export OMNIVEC_RETRY_HEARTBEAT_CMD='echo "should-not-appear"'
HB_ERR2="$WORK_DIR/fast-heartbeat.err"
retry_run "hb-fast" -- true >/dev/null 2>"$HB_ERR2"
if ! grep -q 'should-not-appear' "$HB_ERR2"; then
    ok "heartbeat suppressed for fast commands"
else
    bad "heartbeat suppressed for fast commands" "heartbeat leaked"
fi
unset OMNIVEC_RETRY_HEARTBEAT_CMD OMNIVEC_RETRY_HEARTBEAT_SEC
rm -f "$HB_ERR2"

# Simulate a 601-second readiness wait without an actual delay or Helm call.
helm_calls=0
stalled_helm() {
    helm_calls=$((helm_calls + 1))
    printf 'Error: context deadline exceeded (simulated 601s elapsed)\n' >&2
    return 1
}
_rc=0
retry_run "helm-deploy" -- stalled_helm >/dev/null 2>&1 || _rc=$?
if [ "$_rc" -ne 0 ] && [ "$helm_calls" -eq 1 ]; then
    ok "Helm readiness stall fails without repeating upgrades"
else
    bad "Helm readiness deadline" "rc=$_rc calls=$helm_calls"
fi

printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
