#!/bin/sh
# tests/scripts/test-doctor.sh — smoke-test scripts/doctor.sh runs without error.

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  FAIL %s — %s\n" "$1" "$2"; }

# ── 1. Runs without syntax errors ───────────────────────────────────────────
_rc=0
sh -n "$REPO_ROOT/scripts/doctor.sh" || _rc=$?
[ "$_rc" -eq 0 ] && ok "doctor.sh syntax clean" || bad "doctor.sh syntax clean" "rc=$_rc"

# ── 2. Runs to completion (may exit 1 if az is not logged in, which is OK) ──
_out=$(OMNIVEC_FORCE_NO_TTY=1 sh "$REPO_ROOT/scripts/doctor.sh" 2>&1 </dev/null || true)
case "$_out" in
    *"OmniVec Doctor"*) ok "doctor.sh produces banner" ;;
    *) bad "doctor.sh runs" "no banner in output" ;;
esac
case "$_out" in
    *"Summary"*) ok "doctor.sh prints summary" ;;
    *) bad "doctor.sh prints summary" "no summary line" ;;
esac

printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
