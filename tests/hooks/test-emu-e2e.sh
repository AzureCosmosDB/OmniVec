#!/bin/sh
# tests/hooks/test-emu-e2e.sh - end-to-end happy-path test against the full
# service emulator. Runs preprovision.sh + postprovision.sh and asserts the
# cluster ended up in a healthy state.
set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
HARNESS="$REPO_ROOT/tests/emu/run-azd-up.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  FAIL %s -- %s\n" "$1" "$2"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export OMNIVEC_EMU_STATE="$TMP/state"
mkdir -p "$OMNIVEC_EMU_STATE"

LOG="$TMP/run.log"
chmod +x "$REPO_ROOT/tests/emu/bin"/* "$HARNESS" 2>/dev/null || true

if sh "$HARNESS" >"$LOG" 2>&1; then
    ok "azd up end-to-end succeeded"
else
    bad "azd up end-to-end succeeded" "rc=$? (see tail below)"
    tail -40 "$LOG" | sed 's/^/    /'
fi

if grep -q 'Release "omnivec" has been upgraded' "$LOG"; then
    ok "helm upgrade emitted success banner"
else
    bad "helm upgrade emitted success banner" "missing in log"
fi

# Check helm release recorded
if [ -f "$OMNIVEC_EMU_STATE/helm/releases/omnivec" ]; then
    ok "helm release persisted to state"
else
    bad "helm release persisted to state" "file missing"
fi

# Check synthesised deployments exist
if [ -d "$OMNIVEC_EMU_STATE/k8s/ns/omnivec/deployments" ] \
   && [ "$(ls "$OMNIVEC_EMU_STATE/k8s/ns/omnivec/deployments" 2>/dev/null | wc -l)" -ge 5 ]; then
    ok "at least 5 deployments synthesised in omnivec namespace"
else
    bad "at least 5 deployments synthesised in omnivec namespace" \
        "found: $(ls "$OMNIVEC_EMU_STATE/k8s/ns/omnivec/deployments" 2>/dev/null | wc -l)"
fi

# Event log recorded hooks-driven calls
if grep -q '\baz\b' "$OMNIVEC_EMU_STATE/events.log" \
   && grep -q '\bhelm\b' "$OMNIVEC_EMU_STATE/events.log" \
   && grep -q '\bkubectl\b' "$OMNIVEC_EMU_STATE/events.log"; then
    ok "event log captured az+helm+kubectl invocations"
else
    bad "event log captured az+helm+kubectl invocations" \
        "counts: az=$(grep -c '\baz\b' "$OMNIVEC_EMU_STATE/events.log") \
helm=$(grep -c '\bhelm\b' "$OMNIVEC_EMU_STATE/events.log") \
kubectl=$(grep -c '\bkubectl\b' "$OMNIVEC_EMU_STATE/events.log")"
fi

# Helm status reports deployed
EMU_BIN="$REPO_ROOT/tests/emu/bin"
export PATH="$EMU_BIN:$PATH"
if "$EMU_BIN/helm" status omnivec -n omnivec -o json 2>/dev/null | grep -q '"status":"deployed"'; then
    ok "helm status reports release=deployed"
else
    bad "helm status reports release=deployed" "unexpected output"
fi

printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
