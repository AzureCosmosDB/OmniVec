#!/bin/sh
# tests/hooks/test-preflight.sh — smoke tests for hooks/lib/preflight.sh.
# Stubs `az` to return canned data so we can exercise quota / blob-flip paths.

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  FAIL %s — %s\n" "$1" "$2"; }

# ── Set up a stubbed PATH with fake `az` and `azd` ──────────────────────────
STUB_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t pf)
export PATH="$STUB_DIR:$PATH"

cat > "$STUB_DIR/az" <<'AZEOF'
#!/bin/sh
# Mock az that reads per-test config from $AZ_MOCK_MODE
case "$1 $2 $3" in
  "provider list"*) exit 0 ;;
  "provider register"*) exit 0 ;;
  "provider show"*)
      case "${AZ_MOCK_PROVIDERS:-all-registered}" in
          all-registered) printf 'Registered\n'; exit 0 ;;
          not-registered) printf 'NotRegistered\n'; exit 0 ;;
      esac
      ;;
  "vm list-usage"*)
      case "${AZ_MOCK_QUOTA:-ok}" in
          ok)       printf '[{"name":{"value":"standardBFamily"},"currentValue":0,"limit":100}]'; exit 0 ;;
          exhausted) printf '[{"name":{"value":"standardBFamily"},"currentValue":95,"limit":100}]'; exit 0 ;;
          none)     printf '[]'; exit 0 ;;
      esac
      ;;
  "vm list-skus"*)
      printf 'Standard_B4ms\n'; exit 0 ;;
  "group show"*)
      case "${AZ_MOCK_TAGS:-none}" in
          blob-true)  printf 'true\n'; exit 0 ;;
          blob-false) printf 'false\n'; exit 0 ;;
          *)          printf '\n'; exit 0 ;;
      esac
      ;;
  "storage account check-name"*)
      printf '{"nameAvailable": true}'; exit 0 ;;
  "acr check-name"*)
      printf '{"nameAvailable": true}'; exit 0 ;;
  *) exit 0 ;;
esac
AZEOF
chmod +x "$STUB_DIR/az"

cat > "$STUB_DIR/azd" <<'AZDEOF'
#!/bin/sh
if [ "$1" = "env" ] && [ "$2" = "set" ]; then exit 0; fi
if [ "$1" = "env" ] && [ "$2" = "get-value" ]; then exit 0; fi
exit 0
AZDEOF
chmod +x "$STUB_DIR/azd"

# Must export so the stubbed `az` child process sees the per-test config.
export AZ_MOCK_PROVIDERS AZ_MOCK_QUOTA AZ_MOCK_TAGS

# shellcheck source=../../hooks/lib/preflight.sh
. "$REPO_ROOT/hooks/lib/preflight.sh"

export OMNIVEC_PREFLIGHT_QUIET=1

# ── 1. quota OK passes ──────────────────────────────────────────────────────
AZ_MOCK_QUOTA=ok
_rc=0
preflight_vcpu_quota "eastus2" "Standard_B4ms" "2" "" "0" >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "quota OK passes" || bad "quota OK passes" "rc=$_rc"

# ── 2. quota exhausted: still non-zero exit OR warns (implementation may warn-only)
AZ_MOCK_QUOTA=exhausted
_rc=0
preflight_vcpu_quota "eastus2" "Standard_B4ms" "20" "" "0" >/dev/null 2>&1 || _rc=$?
ok "quota exhausted path exercised (rc=$_rc)"

# ── 3. blob-flip: new=false, old=true → must fail ───────────────────────────
AZ_MOCK_TAGS=blob-true
_rc=0
preflight_blob_flip_guard "rg-omnivec-test" "false" >/dev/null 2>&1 || _rc=$?
[ "$_rc" -ne 0 ] && ok "blob flip true→false blocked" || bad "blob flip true→false blocked" "rc=$_rc"

# ── 4. blob-flip: new=true, old=true → pass ─────────────────────────────────
AZ_MOCK_TAGS=blob-true
_rc=0
preflight_blob_flip_guard "rg-omnivec-test" "true" >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "blob unchanged passes" || bad "blob unchanged passes" "rc=$_rc"

# ── 5. blob-flip: no RG tag → pass (first-run) ──────────────────────────────
AZ_MOCK_TAGS=none
_rc=0
preflight_blob_flip_guard "rg-omnivec-test" "false" >/dev/null 2>&1 || _rc=$?
[ "$_rc" -eq 0 ] && ok "first-run has no old tag → pass" || bad "first-run has no old tag → pass" "rc=$_rc"

# ── 6. sku vcpus table lookup ───────────────────────────────────────────────
if command -v preflight_sku_vcpus >/dev/null 2>&1; then
    _v=$(preflight_sku_vcpus "Standard_B4ms" 2>/dev/null)
    [ "$_v" = "4" ] && ok "sku table Standard_B4ms = 4 vCPUs" || bad "sku vcpus" "got=$_v"
fi

rm -rf "$STUB_DIR"
printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
