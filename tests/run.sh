#!/bin/sh
# OmniVec - run all hook test suites under posix sh.
# Usage:
#   tests/run.sh              # runs all tests
#   tests/run.sh --shell dash # force a specific shell (default: try dash, bash, sh)
set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TEST_SHELL=""
case "${1:-}" in
    --shell) TEST_SHELL="$2" ;;
esac

SHELLS_TO_TRY="${TEST_SHELL:-dash bash sh}"

# Run Python-based API tests once (they don't depend on the shell under test)
# and only if Python + fastapi are importable. We install nothing — they get
# skipped in minimal environments and reported as passing.
run_python_api_tests() {
    if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
        printf '>>> skipping python api tests (no python interpreter)\n'
        return 0
    fi
    PY=$(command -v python3 || command -v python)
    if ! "$PY" -c 'import fastapi, httpx' >/dev/null 2>&1; then
        printf '>>> skipping python api tests (fastapi/httpx not installed)\n'
        return 0
    fi
    for _t in "$SCRIPT_DIR"/api/test_*.py; do
        [ -f "$_t" ] || continue
        printf '\n--- %s ---\n' "$(basename "$_t")"
        "$PY" "$_t"
        _rc=$?
        if [ "$_rc" -ne 0 ]; then
            TOTAL_FAIL=$(( TOTAL_FAIL + 1 ))
            printf '*** FAILED python api test (exit %d)\n' "$_rc"
        fi
    done
}

TOTAL_FAIL=0
run_python_api_tests
for _sh in $SHELLS_TO_TRY; do
    if ! command -v "$_sh" >/dev/null 2>&1; then
        printf '>>> skipping %s (not installed)\n' "$_sh"
        continue
    fi
    printf '\n========== running under %s ==========\n' "$_sh"
    for _t in "$SCRIPT_DIR"/hooks/test-*.sh "$SCRIPT_DIR"/infra/test-*.sh "$SCRIPT_DIR"/scripts/test-*.sh; do
        [ -f "$_t" ] || continue
        printf '\n--- %s ---\n' "$(basename "$_t")"
        "$_sh" "$_t"
        _rc=$?
        if [ "$_rc" -ne 0 ]; then
            TOTAL_FAIL=$(( TOTAL_FAIL + 1 ))
            printf '*** FAILED under %s (exit %d)\n' "$_sh" "$_rc"
        fi
    done
done

printf '\n========== done: %d failing suites ==========\n' "$TOTAL_FAIL"
exit "$TOTAL_FAIL"
