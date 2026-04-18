#!/bin/sh
# tests/hooks/test-deploy-ticker.sh — start/stop + PID lifecycle smoke test.

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  FAIL %s — %s\n" "$1" "$2"; }

STUB_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t dt)
export PATH="$STUB_DIR:$PATH"
# Stub az to return empty deployment list quickly so the poller can loop once.
cat > "$STUB_DIR/az" <<'EOF'
#!/bin/sh
case "$1 $2" in
    "deployment group")  echo '[]'; exit 0 ;;
    "deployment sub")    echo '[]'; exit 0 ;;
    *) exit 0 ;;
esac
EOF
chmod +x "$STUB_DIR/az"

# shellcheck source=../../hooks/lib/deploy-ticker.sh
. "$REPO_ROOT/hooks/lib/deploy-ticker.sh"

# ── 1. start + stop don't error ─────────────────────────────────────────────
export OMNIVEC_TICKER_INTERVAL=1
export OMNIVEC_TICKER_QUIET=1
_rc=0
deploy_ticker_start "rg-test" >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "start returns 0" || bad "start returns 0" "rc=$_rc"

# Give it a moment to actually spawn
sleep 1

# ── 2. PID file exists after start ──────────────────────────────────────────
if [ -n "${DEPLOY_TICKER_PID_FILE:-}" ] && [ -f "$DEPLOY_TICKER_PID_FILE" ]; then
    ok "PID file created"
else
    bad "PID file created" "no file at ${DEPLOY_TICKER_PID_FILE:-<unset>}"
fi

_rc=0
deploy_ticker_stop >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "stop returns 0" || bad "stop returns 0" "rc=$_rc"

# ── 3. PID file is cleaned up ───────────────────────────────────────────────
if [ -n "${DEPLOY_TICKER_PID_FILE:-}" ] && [ ! -f "$DEPLOY_TICKER_PID_FILE" ]; then
    ok "PID file cleaned up after stop"
else
    bad "PID file cleaned up" "still exists at ${DEPLOY_TICKER_PID_FILE:-<unset>}"
fi

# ── 4. double-stop is safe ──────────────────────────────────────────────────
_rc=0
deploy_ticker_stop >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "double stop is safe" || bad "double stop is safe" "rc=$_rc"

rm -rf "$STUB_DIR"
printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
