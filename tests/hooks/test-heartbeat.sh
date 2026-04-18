#!/bin/sh
# Unit tests for hooks/lib/heartbeat.sh
# Run on Linux/macOS/WSL. Deliberately sticks to POSIX sh.
#
# Usage: ./tests/hooks/test-heartbeat.sh
#        sh   ./tests/hooks/test-heartbeat.sh
#        dash ./tests/hooks/test-heartbeat.sh   # strictest
#
# Exits 0 if all pass, 1 if any fail.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
LIB="$REPO_ROOT/hooks/lib/heartbeat.sh"

if [ ! -f "$LIB" ]; then
    echo "FATAL: $LIB not found" >&2
    exit 2
fi

# Isolate HOME so test runs don't pollute real ~/.omnivec
TMP_HOME=$(mktemp -d 2>/dev/null || mktemp -d -t omnivec-hb)
HOME=$TMP_HOME
export HOME
unset OMNIVEC_RUN_START OMNIVEC_TIMINGS_FILE OMNIVEC_HEARTBEAT_INTERVAL OMNIVEC_HEARTBEAT_QUIET

PASS=0
FAIL=0
trap 'rm -rf "$TMP_HOME"' EXIT

pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

# ------------------------------------------------------------------
printf '\n=== heartbeat.sh unit tests ===\n'

# 1. Source is idempotent and sets init vars
(
    . "$LIB"
    [ -n "${OMNIVEC_RUN_START:-}" ] || exit 10
    [ -n "${OMNIVEC_TIMINGS_FILE:-}" ] || exit 11
    [ -n "${OMNIVEC_HEARTBEAT_INTERVAL:-}" ] || exit 12
    # second source doesn't reset start time
    _first=$OMNIVEC_RUN_START
    sleep 1
    . "$LIB"
    [ "$OMNIVEC_RUN_START" = "$_first" ] || exit 13
) && pass "sourcing initializes env and is idempotent" || fail "sourcing / idempotence (exit $?)"

# 2. hb_now returns [mm:ss] format
(
    . "$LIB"
    _v=$(hb_now)
    case "$_v" in
        '['[0-9][0-9]':'[0-9][0-9]']') exit 0 ;;
        *) echo "got: $_v" >&2; exit 1 ;;
    esac
) && pass "hb_now formats [mm:ss]" || fail "hb_now format"

# 3. hb_log prints with timestamp and message
(
    . "$LIB"
    out=$(hb_log info "hello world" 2>&1)
    case "$out" in
        *'[0'*':'*']'*'hello world'*) exit 0 ;;
        *) echo "got: $out" >&2; exit 1 ;;
    esac
) && pass "hb_log emits timestamp + message" || fail "hb_log format"

# 4. wait_with_heartbeat: fast command returns 0 with no ticks
(
    . "$LIB"
    OMNIVEC_HEARTBEAT_INTERVAL=10
    out=$(wait_with_heartbeat "fast-op" true 2>&1)
    rc=$?
    [ $rc -eq 0 ] || { echo "exit=$rc"; exit 1; }
    # No "still" line should appear
    case "$out" in
        *still*) echo "unexpected tick: $out"; exit 2 ;;
        *) exit 0 ;;
    esac
) && pass "wait_with_heartbeat: fast cmd, no ticks, rc=0" || fail "fast-cmd ticks"

# 5. wait_with_heartbeat propagates non-zero exit code
(
    . "$LIB"
    OMNIVEC_HEARTBEAT_INTERVAL=10
    wait_with_heartbeat "failing" sh -c 'exit 42' >/dev/null 2>&1
    rc=$?
    [ $rc -eq 42 ] || { echo "expected 42, got $rc"; exit 1; }
) && pass "wait_with_heartbeat preserves exit code 42" || fail "exit code preservation"

# 6. wait_with_heartbeat emits tick for slow command
(
    . "$LIB"
    OMNIVEC_HEARTBEAT_INTERVAL=1
    out=$(wait_with_heartbeat "slow-op" sleep 3 2>&1)
    rc=$?
    [ $rc -eq 0 ] || { echo "exit=$rc"; exit 1; }
    case "$out" in
        *still\ slow-op*) exit 0 ;;
        *) echo "no tick in: $out"; exit 2 ;;
    esac
) && pass "wait_with_heartbeat emits tick at interval=1s" || fail "tick emission"

# 7. OMNIVEC_HEARTBEAT_QUIET=1 suppresses ticks
(
    . "$LIB"
    OMNIVEC_HEARTBEAT_INTERVAL=1
    OMNIVEC_HEARTBEAT_QUIET=1
    out=$(wait_with_heartbeat "quiet-op" sleep 2 2>&1)
    case "$out" in
        *still*) echo "tick leaked in quiet mode: $out"; exit 1 ;;
        *) exit 0 ;;
    esac
) && pass "OMNIVEC_HEARTBEAT_QUIET=1 suppresses ticks" || fail "quiet mode"

# 8. hb_step_start / hb_step_end writes to timings file
(
    . "$LIB"
    hb_step_start "provision-aks" >/dev/null
    sleep 1
    hb_step_end "provision-aks" ok >/dev/null
    [ -f "$OMNIVEC_TIMINGS_FILE" ] || { echo "timings file missing"; exit 1; }
    grep -q '"name":"provision-aks"' "$OMNIVEC_TIMINGS_FILE" || { echo "no record"; exit 2; }
    grep -q '"status":"ok"' "$OMNIVEC_TIMINGS_FILE" || { echo "no status"; exit 3; }
    grep -q '"duration":[0-9]' "$OMNIVEC_TIMINGS_FILE" || { echo "no duration"; exit 4; }
) && pass "hb_step_start/end writes JSONL timing record" || fail "timing record"

# 9. Step name with special chars is slot-safe
(
    . "$LIB"
    hb_step_start "weird name: with spaces/colons" >/dev/null
    hb_step_end "weird name: with spaces/colons" fail >/dev/null
    grep -q '"status":"fail"' "$OMNIVEC_TIMINGS_FILE" || exit 1
) && pass "step names with punctuation work" || fail "special-char step name"

# 10. hb_slowest_summary emits top-N sorted desc
(
    . "$LIB"
    rm -f "$OMNIVEC_TIMINGS_FILE"
    # Seed manually with deterministic durations
    printf '{"name":"short","status":"ok","start":1,"end":2,"duration":1}\n' >> "$OMNIVEC_TIMINGS_FILE"
    printf '{"name":"medium","status":"ok","start":1,"end":6,"duration":5}\n' >> "$OMNIVEC_TIMINGS_FILE"
    printf '{"name":"long","status":"ok","start":1,"end":21,"duration":20}\n' >> "$OMNIVEC_TIMINGS_FILE"
    out=$(hb_slowest_summary 2>&1)
    # "long" should appear before "short"
    line_long=$(printf '%s\n' "$out" | grep -n '  long$' | head -1 | cut -d: -f1)
    line_short=$(printf '%s\n' "$out" | grep -n '  short$' | head -1 | cut -d: -f1)
    [ -n "$line_long" ] && [ -n "$line_short" ] || { echo "missing entries"; echo "$out"; exit 1; }
    [ "$line_long" -lt "$line_short" ] || { echo "wrong order"; echo "$out"; exit 2; }
) && pass "hb_slowest_summary sorts slowest first" || fail "slowest summary order"

# 11. Timings file path does not contain spaces or newlines
(
    . "$LIB"
    case "$OMNIVEC_TIMINGS_FILE" in
        *' '*|*'
'*) echo "bad path: $OMNIVEC_TIMINGS_FILE"; exit 1 ;;
        *) exit 0 ;;
    esac
) && pass "timings file path is safe" || fail "timings path safety"

# 12. wait_with_heartbeat: concurrent call isolation (two in sequence)
(
    . "$LIB"
    OMNIVEC_HEARTBEAT_INTERVAL=10
    wait_with_heartbeat "first" sh -c 'exit 7' >/dev/null 2>&1
    rc1=$?
    wait_with_heartbeat "second" true >/dev/null 2>&1
    rc2=$?
    [ $rc1 -eq 7 ] && [ $rc2 -eq 0 ] || { echo "rc1=$rc1 rc2=$rc2"; exit 1; }
) && pass "sequential calls don't leak state" || fail "state isolation"

# 13. wait_with_heartbeat: usage error returns 2
(
    . "$LIB"
    wait_with_heartbeat >/dev/null 2>&1
    [ $? -eq 2 ] || exit 1
    wait_with_heartbeat only_label >/dev/null 2>&1
    [ $? -eq 2 ] || exit 2
) && pass "wait_with_heartbeat rejects bad usage (rc=2)" || fail "usage errors"

# ------------------------------------------------------------------
printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
