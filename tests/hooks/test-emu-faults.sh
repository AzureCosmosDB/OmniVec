#!/bin/sh
# tests/hooks/test-emu-faults.sh - fault-injection scenarios against the emulator.
set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
HARNESS="$REPO_ROOT/tests/emu/run-azd-up.sh"

PASS=0; FAIL=0
COUNTS="$(mktemp)"
printf '0 0\n' > "$COUNTS"
_inc() {
    # $1 = field (1=pass, 2=fail); atomic-ish since suite runs serially.
    read _p _f < "$COUNTS"
    if [ "$1" = "1" ]; then _p=$((_p+1)); else _f=$((_f+1)); fi
    printf '%d %d\n' "$_p" "$_f" > "$COUNTS"
}
ok()  { _inc 1; printf "  OK  %s\n" "$1"; }
bad() { _inc 2; printf "  FAIL %s -- %s\n" "$1" "$2"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP" "$COUNTS"' EXIT
chmod +x "$REPO_ROOT/tests/emu/bin"/* "$HARNESS" 2>/dev/null || true

# Keep retries fast for tests
export OMNIVEC_RETRY_BASE_SEC=0
export OMNIVEC_RETRY_ATTEMPTS=4

# ── Scenario 1: transient helm failure recovers ─────────────────────────────
(
    OMNIVEC_EMU_STATE="$TMP/s1"
    export OMNIVEC_EMU_STATE
    mkdir -p "$OMNIVEC_EMU_STATE"
    export OMNIVEC_EMU_TRANSIENT_CMD='helm upgrade:2'
    LOG="$TMP/s1.log"
    if sh "$HARNESS" >"$LOG" 2>&1; then
        if grep -q 'transient failure' "$LOG" && grep -q 'Release "omnivec" has been upgraded' "$LOG"; then
            ok "transient helm failure retried and recovered"
        else
            bad "transient helm failure retried and recovered" "missing expected markers in log"
        fi
    else
        bad "transient helm failure retried and recovered" "harness rc=$?"
    fi
)

# ── Scenario 2: hard failure on helm upgrade halts deploy ──────────────────
(
    OMNIVEC_EMU_STATE="$TMP/s2"
    export OMNIVEC_EMU_STATE
    mkdir -p "$OMNIVEC_EMU_STATE"
    export OMNIVEC_EMU_FAIL_CMD='helm upgrade'
    LOG="$TMP/s2.log"
    sh "$HARNESS" >"$LOG" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        if grep -q 'injected non-transient failure' "$LOG" \
           || grep -q 'failed after' "$LOG" \
           || grep -q 'Helm deploy failed' "$LOG"; then
            ok "non-transient helm failure halts deploy"
        else
            bad "non-transient helm failure halts deploy" \
                "harness exited $rc but expected markers missing"
        fi
    else
        bad "non-transient helm failure halts deploy" "harness succeeded rc=0"
    fi
)

# ── Scenario 3: delay on az acr import does not cause a failure ────────────
(
    OMNIVEC_EMU_STATE="$TMP/s3"
    export OMNIVEC_EMU_STATE
    mkdir -p "$OMNIVEC_EMU_STATE"
    export OMNIVEC_EMU_DELAY_CMD='az acr import:1'
    LOG="$TMP/s3.log"
    if sh "$HARNESS" >"$LOG" 2>&1; then
        ok "delay on az acr import is absorbed"
    else
        bad "delay on az acr import is absorbed" "harness rc=$?"
    fi
)

# ── Scenario 4: idempotent rerun — skip path should kick in ────────────────
(
    OMNIVEC_EMU_STATE="$TMP/s4"
    export OMNIVEC_EMU_STATE
    mkdir -p "$OMNIVEC_EMU_STATE"
    # First run: baseline
    sh "$HARNESS" >"$TMP/s4a.log" 2>&1 || true
    # Second run: nothing changed, skip-helm should fire
    LOG="$TMP/s4b.log"
    sh "$HARNESS" >"$LOG" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ] && grep -q 'skipping helm upgrade' "$LOG"; then
        ok "second run skips helm when nothing changed"
    else
        bad "second run skips helm when nothing changed" "rc=$rc; tail:"
        tail -10 "$LOG" | sed 's/^/    /'
    fi
)

# ── Scenario 5: CrashLoopBackOff regression — worker never becomes ready ────
# Reproduces the live-cluster bug we shipped PR #81/#82 for.
(
    OMNIVEC_EMU_STATE="$TMP/s5"
    export OMNIVEC_EMU_STATE
    mkdir -p "$OMNIVEC_EMU_STATE"
    export OMNIVEC_EMU_CRASHLOOP_DEPLOY=omnivec-dotnet-worker
    LOG="$TMP/s5.log"
    sh "$HARNESS" >"$LOG" 2>&1
    rc=$?
    # Harness should fail (rc != 0) AND the hook logs should surface the
    # CrashLoopBackOff or the UPGRADE FAILED banner so the user is never
    # left staring at a silent hang.
    if [ "$rc" -ne 0 ] \
       && grep -qE 'UPGRADE FAILED|CrashLoopBackOff|BackOff|context deadline exceeded' "$LOG"; then
        ok "CrashLoopBackOff surfaces a visible failure (no silent hang)"
    else
        bad "CrashLoopBackOff surfaces a visible failure (no silent hang)" "rc=$rc; tail:"
        tail -15 "$LOG" | sed 's/^/    /'
    fi
)

read PASS FAIL < "$COUNTS"
printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
