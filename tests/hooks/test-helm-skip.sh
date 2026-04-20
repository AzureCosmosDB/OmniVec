#!/bin/sh
# test-helm-skip.sh - Static checks on the helm-skip logic in postprovision.sh
# and postprovision.ps1. Ensures the skip block exists, gates on all required
# conditions, and caches the fingerprint only on a successful deploy.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)

pass=0
fail=0
_ok()   { pass=$((pass + 1)); printf "  OK  %s\n" "$1"; }
_fail() { fail=$((fail + 1)); printf "  FAIL %s -- %s\n" "$1" "$2"; }

SH="$ROOT_DIR/hooks/postprovision.sh"
PS="$ROOT_DIR/hooks/postprovision.ps1"

if bash -n "$SH"; then _ok "postprovision.sh parses cleanly"; else _fail "postprovision.sh parses cleanly" "bash -n failed"; fi

for needle in OMNIVEC_FORCE_HELM IMAGES_CHANGED _helm_state CURRENT_FP "availableReplicas==0"; do
    if grep -q "$needle" "$SH"; then
        _ok "sh: skip check references '"'"'$needle'"'"'"
    else
        _fail "sh: skip check references '"'"'$needle'"'"'" "missing"
    fi
done

if grep -q "find.*CHART_DIR.*" "$SH" && grep -q "xargs.*sha256sum" "$SH"; then
    _ok "sh: fingerprint covers chart directory contents"
else
    _fail "sh: fingerprint covers chart directory contents" "chart hash not rolled in"
fi

if awk '"'"'/helm_rc.*-eq 0/ { seen=NR } /FINGERPRINT_FILE/ && seen && NR-seen<=3 { found=1 } END { exit(found?0:1) }'"'"' "$SH"; then
    _ok "sh: fingerprint cached only after helm_rc=0"
else
    _fail "sh: fingerprint cached only after helm_rc=0" "not gated on success"
fi

for needle in OMNIVEC_FORCE_HELM imagesChanged helmState currentFp availableReplicas; do
    if grep -q "$needle" "$PS"; then
        _ok "ps1: references '"'"'$needle'"'"'"
    else
        _fail "ps1: references '"'"'$needle'"'"'" "missing"
    fi
done

if grep -q "Get-ChildItem" "$PS" && grep -q "chartDir" "$PS"; then
    _ok "ps1: fingerprint covers chart directory contents"
else
    _fail "ps1: fingerprint covers chart directory contents" "chart hash not rolled in"
fi

printf "\n%d passed, %d failed\n" "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
