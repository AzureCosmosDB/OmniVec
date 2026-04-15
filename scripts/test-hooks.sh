#!/bin/sh
# OmniVec — Hook Test Harness (Bash)
# Tests preprovision.sh and postprovision.sh by mocking external commands.
# Usage: ./scripts/test-hooks.sh
#
# Mirrors the PowerShell test-hooks.ps1 scenarios for Linux/macOS.

set +e  # Don't exit on test failures

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PREPROVISION="$REPO_ROOT/hooks/preprovision.sh"
POSTPROVISION="$REPO_ROOT/hooks/postprovision.sh"

PASS_COUNT=0
FAIL_COUNT=0

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf "  ${GREEN}✓ PASS${NC}  %s\n" "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf "  ${RED}✗ FAIL${NC}  %s\n" "$1"; [ -n "$2" ] && printf "          %s\n" "$2"; }

# ─── Test directory setup ────────────────────────────────────────────────────

new_test_dir() {
  _td=$(mktemp -d /tmp/omnivec-test.XXXXXXXX)
  echo "$_td"
}

cleanup_test_dir() {
  [ -n "$1" ] && rm -rf "$1" 2>/dev/null
}

# ─── Mock script creation ────────────────────────────────────────────────────

write_mock() {
  # $1 = dir, $2 = name, $3 = script body
  _mock="$1/$2"
  cat > "$_mock" <<ENDMOCK
#!/bin/sh
$3
ENDMOCK
  chmod +x "$_mock"
}

# ─── Hook runner ─────────────────────────────────────────────────────────────

run_hook() {
  # $1 = hook path, $2 = mock bin dir, $3 = test dir, $4 = stdin text, $5+ = env vars
  _hook="$1"; _mockbin="$2"; _workdir="$3"; _stdin="$4"
  shift 4

  # Copy hook to test dir
  mkdir -p "$_workdir/hooks"
  cp "$_hook" "$_workdir/hooks/"

  # Set env and run
  _output=$(cd "$_workdir" && \
    PATH="$_mockbin:$PATH" \
    HOME="$_workdir" \
    PSScriptRoot="$_workdir/hooks" \
    "$@" \
    sh "$_workdir/hooks/$(basename "$_hook")" <<EOF 2>&1
$_stdin
EOF
  )
  _exit=$?
  echo "$_output"
  return $_exit
}

# ─── Standard mock helpers ───────────────────────────────────────────────────

setup_preprovision_stubs() {
  _dir="$1"
  mkdir -p "$_dir/docgrok"
  touch "$_dir/docgrok/Dockerfile"
  mkdir -p "$_dir/.azure/$2"
  touch "$_dir/.azure/$2/.env"
  mkdir -p "$_dir/.omnivec/locks"
}

setup_postprovision_stubs() {
  _dir="$1"; _envname="$2"
  mkdir -p "$_dir/docgrok" "$_dir/helm/omnivec" "$_dir/.azure/$_envname" "$_dir/.omnivec/locks" "$_dir/.kube"
  touch "$_dir/docgrok/Dockerfile" "$_dir/.azure/$_envname/.env"
  # Create minimal Chart.yaml and Chart.lock
  echo "name: omnivec" > "$_dir/helm/omnivec/Chart.yaml"
  echo "dependencies: []" > "$_dir/helm/omnivec/Chart.lock"
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIOS
# ═════════════════════════════════════════════════════════════════════════════

printf "\n${CYAN}OmniVec Hook Test Harness (Bash)${NC}\n\n"

# ─── Preprovision Scenarios ──────────────────────────────────────────────────
printf "${YELLOW}--- Preprovision Scenarios ---${NC}\n"

# Scenario 1: Fresh deploy — quick start defaults
test_scenario_1() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_preprovision_stubs "$_td" "$_env"

  write_mock "$_mb" "az" '
case "$*" in
  *"group exists"*) echo "false"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *"vm list-skus"*) echo "Standard_B4ms"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env get-value"*) echo "ERROR: key not found" >&2; exit 1 ;;
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" 'exit 0'
  write_mock "$_mb" "helm" 'exit 0'
  write_mock "$_mb" "git" 'exit 0'

  # Stdin: choose "1" for quick start
  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "1" \
    AZURE_ENV_NAME="$_env" AZURE_LOCATION="eastus2")
  _rc=$?

  if echo "$_output" | grep -q "Quick start\|recommended defaults\|Applying recommended"; then
    pass "Scenario 1: Fresh deploy — quick start applies defaults"
  else
    fail "Scenario 1: Fresh deploy — quick start" "Expected quick start text. exit=$_rc"
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

  write_mock "$_mb" "az" '
case "$*" in
  *"group exists"*) echo "true"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *"group show"*query*) echo "Standard_B4ms"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *"env get-value"*) echo "ERROR" >&2; exit 1 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" 'exit 0'
  write_mock "$_mb" "helm" 'exit 0'
  write_mock "$_mb" "git" 'exit 0'

  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "" \
    AZURE_ENV_NAME="$_env" AZURE_LOCATION="eastus2")
  _rc=$?

  if echo "$_output" | grep -q "Existing deployment detected\|Importing config"; then
    pass "Scenario 2: Existing RG with tags — imports config"
  else
    fail "Scenario 2: Existing RG" "Expected import text. exit=$_rc"
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

  write_mock "$_mb" "az" '
case "$*" in
  *"group exists"*) echo "false"; exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\",\"id\":\"00000000\"}"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env get-value"*OMNIVEC_SYSTEM_NODE_VM_SIZE*) echo "Standard_B4ms"; exit 0 ;;
  *"env get-value"*) echo "some-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *"env select"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" 'exit 0'
  write_mock "$_mb" "helm" 'exit 0'
  write_mock "$_mb" "git" 'exit 0'

  _output=$(run_hook "$PREPROVISION" "$_mb" "$_td" "" \
    AZURE_ENV_NAME="$_env" AZURE_LOCATION="eastus2")
  _rc=$?

  if echo "$_output" | grep -q "Config already set\|Skipping prompts"; then
    pass "Scenario 3: Config pre-set — skips prompts"
  else
    fail "Scenario 3: Config pre-set" "Expected skip text. exit=$_rc"
  fi

  cleanup_test_dir "$_td"
}
test_scenario_3

# ─── Postprovision Scenarios ────────────────────────────────────────────────
printf "\n${YELLOW}--- Postprovision Scenarios ---${NC}\n"

# Common postprovision env vars
postprov_env() {
  echo "AZURE_ENV_NAME=$1"
  echo "AZURE_AKS_CLUSTER_NAME=omnivec-aks-test"
  echo "AZURE_ACR_LOGIN_SERVER=testacr.azurecr.io"
  echo "AZURE_ACR_NAME=testacr"
  echo "AZURE_COSMOS_ENDPOINT=https://test.documents.azure.com:443/"
  echo "AZURE_RESOURCE_GROUP=rg-omnivec-$1"
  echo "AZURE_IDENTITY_CLIENT_ID=test-identity-id"
  echo "INSTANCE_ID=test-instance"
  echo "ENABLE_BLOB_SOURCE=false"
  echo "BUILD_MODE=import"
}

# Scenario 5: Anonymous pull works
test_scenario_5() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_postprovision_stubs "$_td" "$_env"

  write_mock "$_mb" "az" '
case "$*" in
  *"acr import"*) exit 0 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *"account show"*) echo "{\"name\":\"TestSub\"}"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env get-value"*ADMIN_TOKEN*) echo "test-token"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" '
case "$*" in
  *"get nodes"*) echo "node1 Ready"; exit 0 ;;
  *"create namespace"*) exit 0 ;;
  *"label"*) exit 0 ;;
  *"annotate"*) exit 0 ;;
  *"get pods"*) echo "omnivec-api-xxx Running 0 1m"; exit 0 ;;
  *"get svc"*) echo "1.2.3.4"; exit 0 ;;
  *"rollout"*) exit 0 ;;
  *"cluster-info"*) echo "running"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "helm" '
case "$*" in
  *"dependency"*) exit 0 ;;
  *"upgrade"*) echo "deployed"; exit 0 ;;
  *"status"*) echo "{\"info\":{\"status\":\"deployed\"},\"version\":1}"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "git" 'exit 0'
  write_mock "$_mb" "timeout" 'shift; exec "$@"'  # pass-through

  _output=$(eval $(postprov_env "$_env") run_hook "$POSTPROVISION" "$_mb" "$_td" "" \
    $(postprov_env "$_env"))
  _rc=$?

  if echo "$_output" | grep -q "anonymous pull works"; then
    pass "Scenario 5: Anonymous pull works — no token prompt"
  else
    fail "Scenario 5: Anonymous pull" "Expected 'anonymous pull works'. exit=$_rc"
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

  write_mock "$_mb" "az" '
case "$*" in
  *"acr import"*user-provided-token*) exit 0 ;;
  *"acr import"*) echo "401 Unauthorized" >&2; exit 1 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env get-value"*SHARED_REGISTRY_TOKEN*) echo ""; exit 1 ;;
  *"env get-value"*ADMIN_TOKEN*) echo "test-token"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" '
case "$*" in
  *"get nodes"*) echo "node1 Ready"; exit 0 ;;
  *"create namespace"*) exit 0 ;;
  *"label"*) exit 0 ;;
  *"annotate"*) exit 0 ;;
  *"get pods"*) echo "omnivec-api-xxx Running 0 1m"; exit 0 ;;
  *"get svc"*) echo "1.2.3.4"; exit 0 ;;
  *"rollout"*) exit 0 ;;
  *"cluster-info"*) echo "running"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "helm" '
case "$*" in
  *"dependency"*) exit 0 ;;
  *"upgrade"*) echo "deployed"; exit 0 ;;
  *"status"*) echo "{\"info\":{\"status\":\"deployed\"},\"version\":1}"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "git" 'exit 0'
  write_mock "$_mb" "timeout" 'shift; exec "$@"'

  # Provide token via stdin
  _output=$(eval $(postprov_env "$_env") run_hook "$POSTPROVISION" "$_mb" "$_td" \
    "user-provided-token" \
    $(postprov_env "$_env"))
  _rc=$?

  if echo "$_output" | grep -q "token required\|Enter token"; then
    pass "Scenario 7: Anonymous fails, no token — shows token prompt"
  else
    fail "Scenario 7: Token prompt" "Expected prompt text. exit=$_rc"
  fi

  cleanup_test_dir "$_td"
}
test_scenario_7

# Scenario 9: All auth fails — falls back to build
test_scenario_9() {
  _td=$(new_test_dir)
  _mb="$_td/mockbin"; mkdir -p "$_mb"
  _env="test-env"
  setup_postprovision_stubs "$_td" "$_env"

  write_mock "$_mb" "az" '
case "$*" in
  *"acr import"*) echo "401 Unauthorized" >&2; exit 1 ;;
  *"acr build"*) exit 0 ;;
  *"acr repository"*) echo "latest"; exit 0 ;;
  *"acr manifest"*) echo "sha256:abc123"; exit 0 ;;
  *"aks get-credentials"*) exit 0 ;;
  *"tag update"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "azd" '
case "$*" in
  *"env get-value"*SHARED_REGISTRY_TOKEN*) echo ""; exit 1 ;;
  *"env get-value"*ADMIN_TOKEN*) echo "test-token"; exit 0 ;;
  *"env get-value"*BUILD_MODE*) echo "acr"; exit 0 ;;
  *"env get-value"*) echo "test-value"; exit 0 ;;
  *"env set"*) exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "kubectl" '
case "$*" in
  *"get nodes"*) echo "node1 Ready"; exit 0 ;;
  *"create namespace"*) exit 0 ;;
  *"label"*) exit 0 ;;
  *"annotate"*) exit 0 ;;
  *"get pods"*) echo "omnivec-api-xxx Running 0 1m"; exit 0 ;;
  *"get svc"*) echo "1.2.3.4"; exit 0 ;;
  *"rollout"*) exit 0 ;;
  *"cluster-info"*) echo "running"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "helm" '
case "$*" in
  *"dependency"*) exit 0 ;;
  *"upgrade"*) echo "deployed"; exit 0 ;;
  *"status"*) echo "{\"info\":{\"status\":\"deployed\"},\"version\":1}"; exit 0 ;;
  *) exit 0 ;;
esac'

  write_mock "$_mb" "git" 'exit 0'
  write_mock "$_mb" "docker" 'exit 0'
  write_mock "$_mb" "timeout" 'shift; exec "$@"'

  # Stdin: press Enter (empty) to skip token → build
  _output=$(eval $(postprov_env "$_env") run_hook "$POSTPROVISION" "$_mb" "$_td" \
    "" \
    $(postprov_env "$_env"))
  _rc=$?

  if echo "$_output" | grep -qi "build from source\|Building\|acr build\|no interactive input"; then
    pass "Scenario 9: All auth fails — falls back to build"
  else
    fail "Scenario 9: Build fallback" "Expected build text. exit=$_rc"
  fi

  cleanup_test_dir "$_td"
}
test_scenario_9

# Scenario: read_input doesn't crash under set -eu
test_read_input() {
  # Extract read_input function and test it directly
  _src=$(sed -n '/^read_input()/,/^}/p' "$POSTPROVISION")
  if [ -z "$_src" ]; then
    fail "read_input: Could not extract function"
    return
  fi

  # Test with stdin input
  _result=$(echo "test-val" | sh -c "set -eu; YELLOW='' NC=''; $_src; read_input 'prompt: '" 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ] && echo "$_result" | grep -q "test-val"; then
    pass "read_input: stdin pipe works under set -eu"
  else
    fail "read_input: stdin pipe failed (exit=$_rc)"
  fi

  # Test with empty stdin
  _result=$(echo "" | sh -c "set -eu; YELLOW='' NC=''; $_src; read_input 'prompt: '" 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ]; then
    pass "read_input: empty stdin — no crash under set -eu"
  else
    fail "read_input: empty stdin crashed (exit=$_rc)"
  fi

  # Test with /dev/null (no TTY)
  _result=$(sh -c "set -eu; YELLOW='' NC=''; $_src; read_input 'prompt: '" </dev/null 2>&1)
  _rc=$?
  if [ "$_rc" -eq 0 ]; then
    pass "read_input: /dev/null stdin — no crash under set -eu"
  else
    fail "read_input: /dev/null stdin crashed (exit=$_rc)"
  fi
}
test_read_input

# ═════════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════════

echo ""
printf "${CYAN}============================================${NC}\n"
printf "${CYAN}  Results: %d passed, %d failed${NC}\n" "$PASS_COUNT" "$FAIL_COUNT"
printf "${CYAN}============================================${NC}\n"
echo ""

exit "$FAIL_COUNT"
