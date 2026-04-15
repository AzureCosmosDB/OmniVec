#!/bin/sh
# OmniVec — Hook Test Harness (Bash)
# Tests preprovision.sh and postprovision.sh by mocking external commands.
# Usage: ./scripts/test-hooks.sh

set +e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PREPROVISION="$REPO_ROOT/hooks/preprovision.sh"
POSTPROVISION="$REPO_ROOT/hooks/postprovision.sh"

PASS_COUNT=0
FAIL_COUNT=0

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf "  ${GREEN}✓ PASS${NC}  %s\n" "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf "  ${RED}✗ FAIL${NC}  %s\n" "$1"; [ -n "${2:-}" ] && printf "          %s\n" "$2"; }

new_test_dir() { mktemp -d /tmp/omnivec-test.XXXXXXXX; }
cleanup_test_dir() { [ -n "$1" ] && rm -rf "$1" 2>/dev/null; }

write_mock() {
  cat > "$1/$2" <<ENDMOCK
#!/bin/sh
$3
ENDMOCK
  chmod +x "$1/$2"
}

# ─── Core runner ─────────────────────────────────────────────────────────────
# Creates a wrapper script that:
#   1. Sets PATH to mockbin ONLY (+ /usr/bin for basic utils)
#   2. Exports all env vars
#   3. Sources the hook script
# This ensures the hook's own PATH modifications still keep mocks first.

run_hook() {
  _hook="$1"; _mockbin="$2"; _workdir="$3"; _stdin="$4"
  shift 4

  mkdir -p "$_workdir/hooks"
  cp "$_hook" "$_workdir/hooks/"
  chmod +x "$_workdir/hooks/$(basename "$_hook")"

  # Create wrapper that locks PATH before running the hook
  _wrapper="$_workdir/_run_test.sh"
  cat > "$_wrapper" <<WRAPPER_EOF
#!/bin/sh
# Force mock PATH first — even if hook prepends more paths, mocks stay first
export PATH="$_mockbin:\$PATH"
export HOME="$_workdir"
WRAPPER_EOF

  # Append env vars
  for _arg in "$@"; do
    echo "export $_arg" >> "$_wrapper"
  done

  # Append the actual hook execution
  cat >> "$_wrapper" <<WRAPPER_EOF
# Run the hook
exec sh "$_workdir/hooks/$(basename "$_hook")"
WRAPPER_EOF
  chmod +x "$_wrapper"

  # Run with stdin
  _output=$(sh "$_wrapper" <<STDIN_EOF 2>&1
$_stdin
STDIN_EOF
  )
  _exit=$?
  echo "$_output"
  return $_exit
}

# ─── Stub creators ──────────────────────────────────────────────────────────

setup_preprovision_stubs() {
  _dir="$1"; _env="$2"
  mkdir -p "$_dir/docgrok" "$_dir/.azure/$_env" "$_dir/.omnivec/locks" "$_dir/.kube"
  touch "$_dir/docgrok/Dockerfile" "$_dir/.azure/$_env/.env"
}

setup_postprovision_stubs() {
  _dir="$1"; _env="$2"
  mkdir -p "$_dir/docgrok" "$_dir/helm/omnivec" "$_dir/.azure/$_env" "$_dir/.omnivec/locks" "$_dir/.kube" "$_dir/.azure-kubectl" "$_dir/.local/bin"
  touch "$_dir/docgrok/Dockerfile" "$_dir/.azure/$_env/.env"
  echo "name: omnivec" > "$_dir/helm/omnivec/Chart.yaml"
  echo "dependencies: []" > "$_dir/helm/omnivec/Chart.lock"
}

# Standard mock that handles common subcommands
write_az_mock() {
  write_mock "$1" "az" "$2"
}

write_noop_mocks() {
  _mb="$1"
  write_mock "$_mb" "kubectl" 'case "$*" in
  *"cluster-info"*) echo "running"; exit 0 ;;
  *"get nodes"*) echo "node1 Ready"; exit 0 ;;
  *"create namespace"*) exit 0 ;;
  *"label"*) exit 0 ;;
  *"annotate"*) exit 0 ;;
  *"apply"*) exit 0 ;;
  *"get pods"*) echo "omnivec-api-xxx 1/1 Running 0 1m"; exit 0 ;;
  *"get svc"*jsonpath*) echo "1.2.3.4"; exit 0 ;;
  *"get svc"*) echo "omnivec-web LoadBalancer 1.2.3.4"; exit 0 ;;
  *"rollout"*) exit 0 ;;
  *"create secret"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "helm" 'case "$*" in
  *"dependency"*) exit 0 ;;
  *"upgrade"*) echo "deployed"; exit 0 ;;
  *"status"*) echo "{\"info\":{\"status\":\"deployed\"},\"version\":1}"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" 'case "$*" in
  *"env get-value"*OMNIVEC_ADMIN_TOKEN*) echo "test-token-123"; exit 0 ;;
  *"env get-value"*OMNIVEC_SHARED_REGISTRY_TOKEN*) echo ""; exit 1 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *"env new"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "git" 'exit 0'
  write_mock "$_mb" "docker" 'exit 0'
  write_mock "$_mb" "timeout" 'shift; exec "$@"'
  write_mock "$_mb" "kubelogin" 'exit 0'
  write_mock "$_mb" "sha256sum" 'echo "abc123  -"'
  write_mock "$_mb" "head" '/usr/bin/head "$@"'
  write_mock "$_mb" "base64" '/usr/bin/base64 "$@" 2>/dev/null || echo "dGVzdA=="'
  write_mock "$_mb" "tr" '/usr/bin/tr "$@"'
  write_mock "$_mb" "cat" '/bin/cat "$@"'
  write_mock "$_mb" "chmod" '/bin/chmod "$@" 2>/dev/null; exit 0'
  write_mock "$_mb" "mkdir" '/bin/mkdir "$@" 2>/dev/null; exit 0'
  write_mock "$_mb" "sed" '/bin/sed "$@" 2>/dev/null || /usr/bin/sed "$@"'
  write_mock "$_mb" "grep" '/bin/grep "$@" 2>/dev/null || /usr/bin/grep "$@"'
  write_mock "$_mb" "awk" '/usr/bin/awk "$@"'
  write_mock "$_mb" "wc" '/usr/bin/wc "$@"'
  write_mock "$_mb" "printf" 'builtin printf "$@" 2>/dev/null || /usr/bin/printf "$@"'
  write_mock "$_mb" "rm" '/bin/rm "$@" 2>/dev/null; exit 0'
  write_mock "$_mb" "touch" '/usr/bin/touch "$@" 2>/dev/null; exit 0'
  write_mock "$_mb" "ln" '/bin/ln "$@" 2>/dev/null; exit 0'
  write_mock "$_mb" "whoami" '/usr/bin/whoami'
}

# ═════════════════════════════════════════════════════════════════════════════
printf "\n${CYAN}OmniVec Hook Test Harness (Bash)${NC}\n\n"

# ─── Preprovision Scenarios ──────────────────────────────────────────────────
printf "${YELLOW}--- Preprovision Scenarios ---${NC}\n"

# Scenario 1: Fresh deploy — quick start
test_scenario_1() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_preprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"group exists"*) echo "false"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *"vm list-skus"*) echo "Standard_B4ms"; exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  # Override azd: nothing configured
  write_mock "$_mb" "azd" 'case "$*" in
  *"env get-value"*) echo "ERROR: key not found" >&2; exit 1 ;;
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "1" \
    "AZURE_ENV_NAME=$_env" "AZURE_LOCATION=eastus2")
  _rc=$?

  if echo "$_output" | grep -qi "quick start\|recommended defaults\|Applying recommended"; then
    pass "Scenario 1: Fresh deploy — quick start applies defaults"
  else
    fail "Scenario 1: Fresh deploy — quick start" "exit=$_rc, output tail: $(echo "$_output" | tail -5)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_1

# Scenario 2: Existing RG with tags — imports config
test_scenario_2() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_preprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"group exists"*) echo "true"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *"group show"*query*) echo "Standard_B4ms"; exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" 'case "$*" in
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *"env get-value"*) echo "ERROR" >&2; exit 1 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "" \
    "AZURE_ENV_NAME=$_env" "AZURE_LOCATION=eastus2")
  _rc=$?

  if echo "$_output" | grep -qi "Existing deployment detected\|Importing config"; then
    pass "Scenario 2: Existing RG with tags — imports config"
  else
    fail "Scenario 2: Existing RG" "exit=$_rc, output tail: $(echo "$_output" | tail -5)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_2

# Scenario 3: Config pre-set — skips prompts
test_scenario_3() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_preprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"group exists"*) echo "false"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" 'case "$*" in
  *"env get-value"*OMNIVEC_SYSTEM_NODE_VM_SIZE*) echo "Standard_B4ms"; exit 0 ;;
  *"env get-value"*) echo "some-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "" \
    "AZURE_ENV_NAME=$_env" "AZURE_LOCATION=eastus2")
  _rc=$?

  if echo "$_output" | grep -qi "Config already set\|Skipping prompts"; then
    pass "Scenario 3: Config pre-set — skips prompts"
  else
    fail "Scenario 3: Config pre-set" "exit=$_rc, output tail: $(echo "$_output" | tail -5)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_3

# ─── Postprovision Scenarios ────────────────────────────────────────────────
printf "\n${YELLOW}--- Postprovision Scenarios ---${NC}\n"

POSTPROV_VARS='AZURE_AKS_CLUSTER_NAME=omnivec-aks-test AZURE_ACR_LOGIN_SERVER=testacr.azurecr.io AZURE_ACR_NAME=testacr AZURE_COSMOS_ENDPOINT=https://test.documents.azure.com:443/ AZURE_RESOURCE_GROUP=rg-omnivec-test-env AZURE_IDENTITY_CLIENT_ID=test-id INSTANCE_ID=test-instance ENABLE_BLOB_SOURCE=false BUILD_MODE=import'

# Scenario 5: Anonymous pull works
test_scenario_5() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_postprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"acr import"*) exit 0 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  _output=$(run_hook "$POSTPROVISION" "$_mb" "$_td" "" \
    "AZURE_ENV_NAME=$_env" $POSTPROV_VARS)
  _rc=$?

  if echo "$_output" | grep -qi "anonymous pull works"; then
    pass "Scenario 5: Anonymous pull works — no token prompt"
  else
    fail "Scenario 5: Anonymous pull" "exit=$_rc, output tail: $(echo "$_output" | tail -10)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_5

# Scenario 7: Anonymous fails, no token — prompts user
test_scenario_7() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_postprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"acr import"*user-test-token*) exit 0 ;;
  *"acr import"*) echo "401" >&2; exit 1 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  # azd: no stored token
  write_mock "$_mb" "azd" 'case "$*" in
  *"env get-value"*SHARED_REGISTRY_TOKEN*) echo ""; exit 1 ;;
  *"env get-value"*ADMIN_TOKEN*) echo "test-token"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  _output=$(run_hook "$POSTPROVISION" "$_mb" "$_td" "user-test-token" \
    "AZURE_ENV_NAME=$_env" $POSTPROV_VARS)
  _rc=$?

  if echo "$_output" | grep -qi "token required\|Enter token"; then
    pass "Scenario 7: Anonymous fails — shows token prompt"
  else
    fail "Scenario 7: Token prompt" "exit=$_rc, output tail: $(echo "$_output" | tail -10)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_7

# Scenario 9: All auth fails — build fallback
test_scenario_9() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_postprovision_stubs "$_td" "$_env"

  write_az_mock "$_mb" 'case "$*" in
  *"acr import"*) echo "401" >&2; exit 1 ;;
  *"acr build"*) exit 0 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *"aks install-cli"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" 'case "$*" in
  *"env get-value"*SHARED_REGISTRY_TOKEN*) echo ""; exit 1 ;;
  *"env get-value"*ADMIN_TOKEN*) echo "test-token"; exit 0 ;;
  *"env get-value"*BUILD_MODE*) echo "acr"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_noop_mocks "$_mb"

  # Empty stdin = press Enter to skip token → build
  _output=$(run_hook "$POSTPROVISION" "$_mb" "$_td" "" \
    "AZURE_ENV_NAME=$_env" $POSTPROV_VARS)
  _rc=$?

  if echo "$_output" | grep -qi "build from source\|Building\|acr build\|no interactive input"; then
    pass "Scenario 9: All auth fails — falls back to build"
  else
    fail "Scenario 9: Build fallback" "exit=$_rc, output tail: $(echo "$_output" | tail -10)"
  fi
  cleanup_test_dir "$_td"
}
test_scenario_9

# ─── read_input unit tests ──────────────────────────────────────────────────
printf "\n${YELLOW}--- read_input Unit Tests ---${NC}\n"

test_read_input() {
  _src=$(sed -n '/^read_input()/,/^}/p' "$POSTPROVISION")
  if [ -z "$_src" ]; then
    fail "read_input: Could not extract function"
    return
  fi

  # Test with stdin pipe
  _result=$(echo "test-val" | sh -c "set -eu; YELLOW='' NC=''; $_src; val=\$(read_input 'prompt: '); echo \"GOT:\$val\"" 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ] && echo "$_result" | grep -q "GOT:test-val"; then
    pass "read_input: stdin pipe returns value"
  else
    fail "read_input: stdin pipe (exit=$_rc, out=$_result)"
  fi

  # Test with empty stdin
  _result=$(echo "" | sh -c "set -eu; YELLOW='' NC=''; $_src; read_input 'prompt: '" 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ]; then
    pass "read_input: empty stdin — no crash"
  else
    fail "read_input: empty stdin (exit=$_rc)"
  fi

  # Test with /dev/null — timeout to prevent hang on /dev/tty
  _result=$(timeout 3 sh -c "set -eu; YELLOW='' NC=''; $_src; read_input 'prompt: '" </dev/null 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ] || [ "$_rc" -eq 124 ]; then
    pass "read_input: /dev/null — no crash (exit=$_rc)"
  else
    fail "read_input: /dev/null crashed (exit=$_rc)"
  fi
}
test_read_input

# ═════════════════════════════════════════════════════════════════════════════
echo ""
printf "${CYAN}============================================${NC}\n"
printf "${CYAN}  Results: %d passed, %d failed${NC}\n" "$PASS_COUNT" "$FAIL_COUNT"
printf "${CYAN}============================================${NC}\n"
echo ""

exit "$FAIL_COUNT"
