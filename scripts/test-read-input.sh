#!/bin/sh
# Quick smoke test for postprovision.sh read_input function
# Verifies it doesn't crash under set -eu with various input scenarios
#
# Usage: ./scripts/test-read-input.sh

set -eu

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓ PASS\033[0m  %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); printf "  \033[31m✗ FAIL\033[0m  %s\n" "$1"; }

echo ""
echo "Testing postprovision.sh read_input function"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Extract the read_input function from postprovision.sh
_read_input_src=$(sed -n '/^read_input()/,/^}/p' "$SCRIPT_DIR/../hooks/postprovision.sh")

if [ -z "$_read_input_src" ]; then
  fail "Could not extract read_input function from postprovision.sh"
  exit 1
fi

# Test 1: read_input with stdin input (simulates interactive)
_result=$(echo "test-token-123" | sh -c "
set -eu
YELLOW='' NC=''
$_read_input_src
read_input 'Enter token: '
")
if echo "$_result" | grep -q "test-token-123"; then
  pass "read_input with stdin pipe returns input value"
else
  fail "read_input with stdin pipe — expected 'test-token-123', got '$_result'"
fi

# Test 2: read_input with empty stdin (simulates Enter key)
_result=$(echo "" | sh -c "
set -eu
YELLOW='' NC=''
$_read_input_src
read_input 'Enter token: '
" 2>&1)
_exit=$?
if [ "$_exit" -eq 0 ]; then
  pass "read_input with empty stdin — no crash (exit 0)"
else
  fail "read_input with empty stdin — crashed with exit $_exit"
fi

# Test 3: read_input with closed stdin (simulates no-TTY, no pipe)
_result=$(sh -c "
set -eu
YELLOW='' NC=''
$_read_input_src
read_input 'Enter token: '
" </dev/null 2>&1)
_exit=$?
if [ "$_exit" -eq 0 ]; then
  pass "read_input with /dev/null stdin — no crash (exit 0)"
else
  fail "read_input with /dev/null stdin — crashed with exit $_exit"
fi

# Test 4: Verify token prompt text appears in output
_output=$(echo "my-token" | sh -c "
set -eu
YELLOW='' NC=''
$_read_input_src
read_input 'Enter token for registry: '
" 2>&1)
if echo "$_output" | grep -q "Enter token for registry"; then
  pass "read_input shows prompt text"
else
  fail "read_input did not show prompt text"
fi

# Test 5: Simulate the full auth flow — anonymous fail → token prompt → fallback
_output=$(echo "" | sh -c "
set -eu
YELLOW='\033[1;33m' NC='\033[0m' GREEN='\033[0;32m' RED='\033[0;31m'
$_read_input_src

ANON_OK=false
TOKEN_OK=false

# Simulate anonymous pull failure
printf '  Testing anonymous pull...'
ANON_OK=false
printf ' requires auth\n'

# No stored token
# Prompt for token
if [ \"\$TOKEN_OK\" = 'false' ]; then
  printf '  Registry token required for import.\n'
  _new_token=\$(read_input '  Enter token (or Enter to build from source): ')
  if [ -z \"\$_new_token\" ]; then
    printf '  No token — will build from source.\n'
  fi
fi
" 2>&1)
_exit=$?
if [ "$_exit" -eq 0 ] && echo "$_output" | grep -q "token required"; then
  pass "Full auth flow: anon fail → prompt → empty input → build fallback (no crash)"
else
  fail "Full auth flow crashed (exit $_exit) or missing prompt. Output: $_output"
fi

echo ""
echo "============================================"
printf "  Results: %d passed, %d failed\n" "$PASS" "$FAIL"
echo "============================================"
echo ""

exit "$FAIL"
