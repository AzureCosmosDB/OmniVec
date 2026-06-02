#!/bin/sh
# End-to-end mock harness test for hooks/preprovision.sh.
#
# Runs the REAL preprovision.sh against recording mocks of azd/az/kubectl/helm.
# This catches regressions like "we forgot to set OMNIVEC_GPU_NODE_COUNT" or
# "we accidentally removed the `</dev/null` guard and now blocks on stdin".
#
# Tests cover:
#   1. NONINTERACTIVE early-exit path (most common CI case): six OMNIVEC_*
#      defaults must be azd-env-set, no cloud calls made, exit 0.
#   2. Fast-fail when no TTY and no NONINTERACTIVE flag.
#   3. Lock file is created and released on exit.
#
# Works under dash, bash, sh.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
MOCKS_DIR="$SCRIPT_DIR/mocks"
PREPROVISION="$REPO_ROOT/hooks/preprovision.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t omnivec-mock)
trap 'rm -rf "$TMP"' EXIT

printf '\n=== Mock harness: hooks/preprovision.sh end-to-end ===\n'

# Ensure mock stubs are executable (git may not preserve +x on Windows checkouts).
chmod +x "$MOCKS_DIR"/azd "$MOCKS_DIR"/az "$MOCKS_DIR"/kubectl "$MOCKS_DIR"/helm 2>/dev/null || true

# --- Case 1: NONINTERACTIVE path ---
(
    LOG="$TMP/case1.log"
    AZDENV="$TMP/case1.env"
    : > "$LOG"
    : > "$AZDENV"

    # Isolate HOME so the lock file can't collide with a real run.
    HOME="$TMP/home-case1"
    mkdir -p "$HOME"

    export OMNIVEC_MOCK_LOG="$LOG"
    export OMNIVEC_MOCK_AZD_ENV="$AZDENV"
    export PATH="$MOCKS_DIR:$PATH"
    export HOME
    export AZURE_ENV_NAME="mock-case1"
    export AZURE_LOCATION="centralus"
    export OMNIVEC_NONINTERACTIVE=1
    export OMNIVEC_FORCE_NO_TTY=1
    # Keep heartbeat quiet so test output is readable.
    export OMNIVEC_HEARTBEAT_QUIET=1

    # Run the REAL hook. It should hit require_tty_or_preset, apply defaults,
    # print the success banner, and exit 0 without reaching the big prompt block.
    sh "$PREPROVISION" </dev/null >"$TMP/case1.out" 2>&1
    rc=$?

    [ "$rc" -eq 0 ] || { printf 'rc=%s\nout=\n%s\n' "$rc" "$(cat "$TMP/case1.out")" >&2; exit 1; }

    # Verify the six defaults were recorded. Accept the values either quoted or
    # unquoted (our recorder quotes whitespace-containing args only).
    for kv in \
        'OMNIVEC_SYSTEM_NODE_VM_SIZE=Standard_B4ms' \
        'OMNIVEC_SYSTEM_NODE_COUNT=2' \
        'OMNIVEC_GPU_NODE_COUNT=0' \
        'OMNIVEC_METADATA_STORE=cosmosdb-serverless'; do
        if ! grep -q "^${kv}\$" "$AZDENV"; then
            printf 'missing %s\n--- env store ---\n%s\n--- mock log ---\n%s\n' \
                "$kv" "$(cat "$AZDENV")" "$(cat "$LOG")" >&2
            exit 1
        fi
    done

    # GPU SKU may be empty - check the key exists even if value is ''.
    grep -q '^OMNIVEC_GPU_NODE_VM_SIZE=' "$AZDENV" || {
        printf 'missing OMNIVEC_GPU_NODE_VM_SIZE key\n' >&2; exit 1
    }

    # No mutating cloud calls should happen on this fast path - read-only
    # preflight (az account show / az group exists) is fine, but no create/
    # update/delete/apply, no helm install, no kubectl apply.
    if grep -Eq '^(kubectl (apply|create|delete|patch)|helm (install|upgrade|uninstall)|az [a-z-]+ (create|update|delete|set))' "$LOG"; then
        printf 'unexpected mutating cloud calls on fast path:\n%s\n' "$(cat "$LOG")" >&2
        exit 1
    fi

    # Success banner must show up.
    grep -q 'non-interactive' "$TMP/case1.out" || {
        printf 'banner missing:\n%s\n' "$(cat "$TMP/case1.out")" >&2; exit 1
    }

    exit 0
) && pass "NONINTERACTIVE path: six OMNIVEC_* defaults set, no mutating cloud calls" \
  || fail "NONINTERACTIVE path"

# --- Case 2: No TTY + no flag -> fast fail with helpful message ---
(
    LOG="$TMP/case2.log"
    AZDENV="$TMP/case2.env"
    : > "$LOG"
    : > "$AZDENV"
    HOME="$TMP/home-case2"
    mkdir -p "$HOME"

    export OMNIVEC_MOCK_LOG="$LOG"
    export OMNIVEC_MOCK_AZD_ENV="$AZDENV"
    export PATH="$MOCKS_DIR:$PATH"
    export HOME
    export AZURE_ENV_NAME="mock-case2"
    export OMNIVEC_FORCE_NO_TTY=1
    export OMNIVEC_HEARTBEAT_QUIET=1
    # Explicitly unset every non-interactive signal.
    unset OMNIVEC_NONINTERACTIVE AZD_NONINTERACTIVE CI GITHUB_ACTIONS

    sh "$PREPROVISION" </dev/null >"$TMP/case2.out" 2>&1
    rc=$?

    [ "$rc" -ne 0 ] || { printf 'expected non-zero rc, got %s\n' "$rc" >&2; exit 1; }

    # Must mention actionable fix options.
    grep -q 'OMNIVEC_NONINTERACTIVE' "$TMP/case2.out" \
        && grep -q 'azd env set' "$TMP/case2.out" \
        || { printf 'error message missing guidance:\n%s\n' "$(cat "$TMP/case2.out")" >&2; exit 1; }

    exit 0
) && pass "no-TTY + no flag: exits non-zero with actionable message" \
  || fail "no-TTY fast-fail"

# --- Case 3: Lock file is created and released ---
(
    LOG="$TMP/case3.log"
    AZDENV="$TMP/case3.env"
    : > "$LOG"; : > "$AZDENV"
    HOME="$TMP/home-case3"
    mkdir -p "$HOME"

    export OMNIVEC_MOCK_LOG="$LOG"
    export OMNIVEC_MOCK_AZD_ENV="$AZDENV"
    export PATH="$MOCKS_DIR:$PATH"
    export HOME
    export AZURE_ENV_NAME="mock-case3"
    export AZURE_LOCATION="centralus"
    export OMNIVEC_NONINTERACTIVE=1
    export OMNIVEC_FORCE_NO_TTY=1
    export OMNIVEC_HEARTBEAT_QUIET=1

    sh "$PREPROVISION" </dev/null >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || { printf 'rc=%s\n' "$rc" >&2; exit 1; }

    # Lock file should NOT remain after successful exit.
    if [ -f "$HOME/.omnivec/locks/${AZURE_ENV_NAME}.lock" ]; then
        printf 'lock file leaked: %s\n' "$HOME/.omnivec/locks/${AZURE_ENV_NAME}.lock" >&2
        exit 1
    fi
    # But the locks dir should exist (acquire_lock mkdir -p).
    [ -d "$HOME/.omnivec/locks" ] || { printf 'locks dir not created\n' >&2; exit 1; }

    exit 0
) && pass "lock file created and released on clean exit" \
  || fail "lock file lifecycle"

# --- Case 4: Stale lock (dead PID) is cleaned up ---
(
    LOG="$TMP/case4.log"
    AZDENV="$TMP/case4.env"
    : > "$LOG"; : > "$AZDENV"
    HOME="$TMP/home-case4"
    LOCK_DIR="$HOME/.omnivec/locks"
    mkdir -p "$LOCK_DIR"
    # Seed with a lock for a PID that cannot exist (PID 1 is init and kill -0
    # would succeed; use a high unused pid for portability).
    printf '999999\nsomehost\n' > "$LOCK_DIR/mock-case4.lock"

    export OMNIVEC_MOCK_LOG="$LOG"
    export OMNIVEC_MOCK_AZD_ENV="$AZDENV"
    export PATH="$MOCKS_DIR:$PATH"
    export HOME
    export AZURE_ENV_NAME="mock-case4"
    export AZURE_LOCATION="centralus"
    export OMNIVEC_NONINTERACTIVE=1
    export OMNIVEC_FORCE_NO_TTY=1
    export OMNIVEC_HEARTBEAT_QUIET=1

    sh "$PREPROVISION" </dev/null >"$TMP/case4.out" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || { printf 'rc=%s\nout=%s\n' "$rc" "$(cat "$TMP/case4.out")" >&2; exit 1; }
    # Stale lock message should show up.
    grep -qi 'stale lock' "$TMP/case4.out" || {
        printf 'expected "Stale lock" notice:\n%s\n' "$(cat "$TMP/case4.out")" >&2
        exit 1
    }
    exit 0
) && pass "stale lock (dead PID) is auto-cleaned" \
  || fail "stale lock recovery"

# --- Case 5: Mocks themselves record correctly ---
(
    LOG="$TMP/case5.log"
    AZDENV="$TMP/case5.env"
    : > "$LOG"; : > "$AZDENV"
    export OMNIVEC_MOCK_LOG="$LOG"
    export OMNIVEC_MOCK_AZD_ENV="$AZDENV"
    export PATH="$MOCKS_DIR:$PATH"
    azd env set FOO bar </dev/null
    azd env set WITH_SPACE "hello world" </dev/null
    val=$(azd env get-value FOO </dev/null)
    [ "$val" = "bar" ] || { printf 'expected bar, got %s\n' "$val" >&2; exit 1; }
    val2=$(azd env get-value WITH_SPACE </dev/null)
    [ "$val2" = "hello world" ] || { printf 'expected hello world, got %s\n' "$val2" >&2; exit 1; }
    # Log must have both calls.
    grep -q '^azd env set FOO bar$' "$LOG" || { printf 'missing FOO line\n'; cat "$LOG"; exit 1; } >&2
    grep -q 'WITH_SPACE "hello world"' "$LOG" || { printf 'missing quoted arg\n'; cat "$LOG"; exit 1; } >&2
    exit 0
) && pass "mock recorder: set/get round-trip, whitespace-safe logging" \
  || fail "mock recorder self-test"

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
