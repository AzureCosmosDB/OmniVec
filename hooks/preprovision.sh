#!/bin/sh
# OmniVec — preprovision hook
# Validates prerequisites, checks for existing installations, and collects config choices

set -eu

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

printf "${GREEN}╔══════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║     OmniVec — Pre-provision Checks       ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════╝${NC}\n"

# ── Validate AZURE_ENV_NAME (Bicep @maxLength=20, must be alphanumeric/dash) ──
# Fail fast with a clear message instead of a cryptic Bicep validation error
# 10 minutes into provisioning.
_env_name="${AZURE_ENV_NAME:-}"
_env_len=${#_env_name}
if [ -z "$_env_name" ]; then
  printf "\n${RED}ERROR: AZURE_ENV_NAME is not set.${NC}\n" >&2
  printf "  Run: ${CYAN}azd env new <name>${NC} (1-20 chars, lowercase alnum+dash)\n" >&2
  exit 1
fi
if [ "$_env_len" -gt 20 ]; then
  printf "\n${RED}ERROR: AZURE_ENV_NAME='${_env_name}' is ${_env_len} chars (max 20).${NC}\n" >&2
  printf "  Azure resource naming requires environmentName <= 20 chars.\n" >&2
  printf "  Fix with:  ${CYAN}azd env set AZURE_ENV_NAME <shorter-name>${NC}\n" >&2
  printf "  Or create a fresh env:  ${CYAN}azd env new <shorter-name>${NC}\n" >&2
  exit 1
fi
# Also match Bicep's expected pattern (lowercase letters, digits, dash; no leading/trailing dash)
case "$_env_name" in
  *[!a-z0-9-]*|-*|*-)
    printf "\n${YELLOW}WARNING: AZURE_ENV_NAME='${_env_name}' may contain invalid characters.${NC}\n" >&2
    printf "  Recommended: lowercase letters, digits, and dashes (no leading/trailing dash).\n" >&2
    ;;
esac

# ── Repair .env if prior run left embedded newlines / stray quotes ──────────
# Symptom: `loading .env: unexpected character "\"" in variable name near "...\n"`
# Cause: a previous azd env set wrote a multi-line value; subsequent runs can't parse it.
_env_file=".azure/${_env_name}/.env"
if [ -f "$_env_file" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$_env_file" <<'PYEOF' || true
import re, sys
p = sys.argv[1]
with open(p, 'r', encoding='utf-8', errors='replace') as f:
    raw = f.read()
# Collapse CR/LF/TAB inside any quoted value of form KEY="...".
def clean(m):
    k = m.group(1); v = re.sub(r'[\r\n\t]+', '', m.group(2)).strip()
    return f'{k}="{v}"'
repaired = re.sub(r'(?ms)^([A-Z_][A-Z0-9_]*)="([^"]*)"', clean, raw)
if repaired != raw:
    with open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write(repaired)
    print('Repaired corrupt .env (stripped embedded whitespace from values).')
PYEOF
fi

# ── Deployment lock: prevent concurrent azd up/down for the same env ────────
LOCK_DIR="${HOME}/.omnivec/locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="${LOCK_DIR}/${AZURE_ENV_NAME}.lock"

acquire_lock() {
  if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null | head -1)
    LOCK_HOST=$(cat "$LOCK_FILE" 2>/dev/null | tail -1)

    # Check if the locking process is still alive
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
      printf "\n${RED}ERROR: Another deployment for '${AZURE_ENV_NAME}' is already running (PID ${LOCK_PID}).${NC}\n"
      printf "  If that process is stuck, you can force-take the lock.\n"
      force_lock=$(read_input "  ${YELLOW}Take over lock and continue? [y/N]: ${NC}")
      case "$force_lock" in
        [yY]*)
          printf "  ${YELLOW}Killing PID ${LOCK_PID} and taking lock...${NC}\n"
          kill "$LOCK_PID" 2>/dev/null || true
          sleep 2
          ;;
        *)
          printf "  ${RED}Aborting. Wait for the other deployment to finish or take over the lock.${NC}\n"
          exit 1
          ;;
      esac
    else
      printf "  ${YELLOW}Stale lock found (PID ${LOCK_PID} is dead). Cleaning up.${NC}\n"
    fi
  fi

  # Write lock: PID on line 1, hostname on line 2
  printf "%s\n%s\n" "$$" "$(hostname 2>/dev/null || echo unknown)" > "$LOCK_FILE"
}

release_lock() {
  rm -f "$LOCK_FILE"
}

# Release lock on exit (success or failure)
trap 'release_lock' EXIT INT TERM

acquire_lock

# Helper: safely read an azd env value, returns empty string on failure.
# azd env get-value returns exit 0 even for missing keys and prints
# "ERROR: key not found..." to stdout, so we must filter that out.
azd_get() {
  _val=$(azd env get-value "$1" < /dev/null 2>/dev/null) || _val=""
  _val=$(printf '%s' "$_val" | tr -d '\r')
  case "$_val" in
    ERROR*|*"not found"*) _val="" ;;
  esac
  printf '%s' "$_val"
}

# Helper: can we actually prompt the user right now?
# Tries /dev/tty first (most reliable: even if stdin is redirected, the hook
# may still have a controlling terminal we can write prompts to). Falls back
# to stdin-is-a-tty. Respects OMNIVEC_FORCE_NO_TTY for tests / CI simulation.
_can_prompt() {
  [ -n "${OMNIVEC_FORCE_NO_TTY:-}" ] && return 1
  if [ -e /dev/tty ] && ( : >/dev/tty ) 2>/dev/null && ( : </dev/tty ) 2>/dev/null; then
    return 0
  fi
  [ -t 0 ] && return 0
  return 1
}

# Helper: read user input from TTY (works in hook context where stdin may be redirected)
# Returns empty string when no TTY is available — callers must fall back to
# defaults OR pre-check with _can_prompt to fast-fail before calling this.
read_input() {
  _prompt=$1
  _input=""
  if _can_prompt; then
    if [ -e /dev/tty ] && ( : >/dev/tty ) 2>/dev/null; then
      printf '%b' "$_prompt" > /dev/tty
      read -r _input < /dev/tty 2>/dev/null || true
    else
      printf '%b' "$_prompt"
      read -r _input || true
    fi
  fi
  printf '%s' "$_input"
}

# a2: Detect non-interactive mode from several common sources. Users (and CI)
# commonly set one of these; we honor any of them.
is_noninteractive() {
  case "${OMNIVEC_NONINTERACTIVE:-}${AZD_NONINTERACTIVE:-}${CI:-}${GITHUB_ACTIONS:-}" in
    '') return 1 ;;
    *) return 0 ;;
  esac
}

# a2: Apply Quick-start defaults to azd env. Used when non-interactive and no
# preset config exists, to give a predictable fallback instead of silently
# accepting empty/garbage input.
apply_quickstart_defaults() {
  printf "  ${GREEN}Applying Quick-start defaults (non-interactive mode).${NC}\n"
  azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_B4ms" < /dev/null
  azd env set OMNIVEC_SYSTEM_NODE_COUNT   "2" < /dev/null
  azd env set OMNIVEC_GPU_NODE_VM_SIZE    "" < /dev/null
  azd env set OMNIVEC_GPU_NODE_COUNT      "0" < /dev/null
  azd env set OMNIVEC_METADATA_STORE      "cosmosdb-serverless" < /dev/null
  azd env set OMNIVEC_ENABLE_BLOB_SOURCE  "true" < /dev/null
}

# a2: Fail fast when we would need to prompt but can't. Far better than a
# silent 10-minute deploy with surprise defaults.
require_tty_or_preset() {
  if _can_prompt; then
    return 0
  fi
  if is_noninteractive; then
    apply_quickstart_defaults
    printf "\n${GREEN}Pre-provision checks passed (non-interactive). Proceeding with Bicep deployment...${NC}\n"
    exit 0
  fi
  printf "\n${RED}ERROR: No TTY and no configuration found.${NC}\n" >&2
  printf "  azd hooks run this script without an interactive terminal (common in CI,\n" >&2
  printf "  Docker, nohup, or piped shells), and no configuration has been pre-set.\n" >&2
  printf "\n  Fix with ONE of:\n" >&2
  printf "    1) Run interactively:  azd up  (from a real terminal)\n" >&2
  printf "    2) Accept defaults:    OMNIVEC_NONINTERACTIVE=1 azd up\n" >&2
  printf "    3) Pre-set config, e.g.:\n" >&2
  printf "         azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE Standard_B4ms\n" >&2
  printf "         azd env set OMNIVEC_SYSTEM_NODE_COUNT 2\n" >&2
  printf "         azd env set OMNIVEC_GPU_NODE_COUNT 0\n" >&2
  printf "         azd env set OMNIVEC_ENABLE_BLOB_SOURCE true\n" >&2
  printf "         azd env set OMNIVEC_METADATA_STORE cosmosdb-serverless\n" >&2
  exit 1
}

DEPLOYMENT_DETECTED=false
IN_PLACE_UPDATE=false

# ── Source hardening libraries ──────────────────────────────────────────────
SCRIPT_DIR_INIT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/heartbeat.sh
. "$SCRIPT_DIR_INIT/lib/heartbeat.sh" 2>/dev/null || true
# shellcheck source=lib/preflight.sh
. "$SCRIPT_DIR_INIT/lib/preflight.sh" 2>/dev/null || true

# Extend the existing EXIT trap to also emit a slowest-step summary on failure.
_preprov_exit() {
  _rc=$?
  [ "$_rc" -ne 0 ] && command -v hb_slowest_summary >/dev/null 2>&1 && hb_slowest_summary
  release_lock
  exit "$_rc"
}
trap '_preprov_exit' EXIT INT TERM



# ── Validate required tools ──────────────────────────────────────────────────

printf "\n${YELLOW}Checking prerequisites...${NC}\n"

if ! command -v az >/dev/null 2>&1; then
  printf "${RED}Missing required tool: az (Azure CLI). Install from https://aka.ms/install-azure-cli${NC}\n"
  exit 1
fi
printf "  ${GREEN}az CLI found.${NC}\n"

KUBECTL_DIR="${HOME}/.azure-kubectl"
if ! command -v kubectl >/dev/null 2>&1; then
  printf "  ${YELLOW}kubectl not found — installing to ${KUBECTL_DIR}...${NC}\n"
  mkdir -p "$KUBECTL_DIR"
  if command -v wait_with_heartbeat >/dev/null 2>&1; then
    wait_with_heartbeat "Installing kubectl (az aks install-cli)" \
      az aks install-cli --install-location "$KUBECTL_DIR/kubectl" --kubelogin-install-location "$KUBECTL_DIR/kubelogin" </dev/null 2>/dev/null || true
  else
    az aks install-cli --install-location "$KUBECTL_DIR/kubectl" --kubelogin-install-location "$KUBECTL_DIR/kubelogin" < /dev/null 2>/dev/null || true
  fi
  chmod +x "$KUBECTL_DIR/kubectl" "$KUBECTL_DIR/kubelogin" 2>/dev/null || true
  export PATH="$KUBECTL_DIR:$PATH"
  if ! command -v kubectl >/dev/null 2>&1; then
    printf "  ${RED}Failed to install kubectl. Install manually: https://aka.ms/install-kubectl${NC}\n"
    exit 1
  fi
  printf "  ${GREEN}kubectl installed.${NC}\n"
else
  printf "  ${GREEN}kubectl found.${NC}\n"
fi

if ! command -v helm >/dev/null 2>&1; then
  printf "  ${YELLOW}helm not found — installing...${NC}\n"
  HELM_INSTALL_DIR="${HOME}/.local/bin"
  mkdir -p "$HELM_INSTALL_DIR"
  export PATH="$HELM_INSTALL_DIR:$PATH"
  # d4: Pin helm version by default for deterministic installs. Override via
  # HELM_VERSION=v3.x.y. Unset/empty ⇒ get-helm-3 grabs "latest" (legacy).
  HELM_VERSION="${HELM_VERSION:-v3.16.2}"
  if command -v wait_with_heartbeat >/dev/null 2>&1; then
    wait_with_heartbeat "Installing helm ${HELM_VERSION}" sh -c \
      "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 </dev/null | HELM_INSTALL_DIR='$HELM_INSTALL_DIR' DESIRED_VERSION='$HELM_VERSION' USE_SUDO=false sh </dev/null 2>/dev/null" || true
  else
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 </dev/null | HELM_INSTALL_DIR="$HELM_INSTALL_DIR" DESIRED_VERSION="$HELM_VERSION" USE_SUDO="false" sh </dev/null 2>/dev/null || true
  fi
  if ! command -v helm >/dev/null 2>&1; then
    printf "  ${RED}Failed to install helm. Install manually: https://helm.sh/docs/intro/install/${NC}\n"
    exit 1
  fi
  printf "  ${GREEN}helm installed.${NC}\n"
else
  printf "  ${GREEN}helm found.${NC}\n"
fi

printf "${GREEN}All prerequisites met.${NC}\n"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Validate Azure login ────────────────────────────────────────────────────

printf "\n${YELLOW}Checking Azure login...${NC}\n"
if ! az account show < /dev/null >/dev/null 2>&1; then
  printf "${RED}Not logged into Azure. Run 'az login' first.${NC}\n"
  exit 1
fi

SUBSCRIPTION=$(az account show --query name -o tsv < /dev/null)
SUBSCRIPTION_ID=$(az account show --query id -o tsv < /dev/null)
printf "${GREEN}Logged in to subscription: ${SUBSCRIPTION} (${SUBSCRIPTION_ID})${NC}\n"

# ── c1: Ensure required resource providers are registered ───────────────────
if command -v preflight_require_providers >/dev/null 2>&1; then
  preflight_require_providers || true
fi

# ── Check for existing deployment (RG exists + config present = update in-place) ─
RG_NAME="rg-omnivec-${AZURE_ENV_NAME}"
RG_EXISTS=$(az group exists --name "$RG_NAME" < /dev/null 2>/dev/null || echo "false")
RG_EXISTS=$(printf '%s' "$RG_EXISTS" | tr -d '\r\n ')

if [ "$RG_EXISTS" = "true" ]; then
  printf "\n${GREEN}Existing deployment detected (RG: ${RG_NAME}). Importing config from tags...${NC}\n"

  # Snapshot user's blob-source override BEFORE tag import overwrites azd env.
  # User can request a flip via either `azd env set OMNIVEC_ENABLE_BLOB_SOURCE true`
  # or `azd env set AZURE_ENABLE_BLOB_SOURCE true`. OMNIVEC_ takes precedence.
  _user_blob_override=$(azd_get OMNIVEC_ENABLE_BLOB_SOURCE)
  if [ -z "$_user_blob_override" ]; then
    _user_blob_override=$(azd_get AZURE_ENABLE_BLOB_SOURCE)
  fi
  _user_blob_override=$(printf '%s' "$_user_blob_override" | tr -d '\r\n ' | tr '[:upper:]' '[:lower:]')

  for _pair in \
    "omnivec-sys-sku:OMNIVEC_SYSTEM_NODE_VM_SIZE" \
    "omnivec-sys-count:OMNIVEC_SYSTEM_NODE_COUNT" \
    "omnivec-gpu-sku:OMNIVEC_GPU_NODE_VM_SIZE" \
    "omnivec-gpu-count:OMNIVEC_GPU_NODE_COUNT" \
    "omnivec-metadata:OMNIVEC_METADATA_STORE" \
    "omnivec-blob:OMNIVEC_ENABLE_BLOB_SOURCE" \
    "omnivec-build:OMNIVEC_BUILD_MODE"; do
    _tag=$(echo "$_pair" | cut -d: -f1)
    _env=$(echo "$_pair" | cut -d: -f2)
    _val=$(az group show --name "$RG_NAME" --query "tags.\"$_tag\"" -o tsv < /dev/null 2>/dev/null || true)
    _val=$(printf '%s' "$_val" | tr -d '\r\n')
    # Honor user-intended blob-source flip instead of re-importing the stale tag.
    if [ "$_env" = "OMNIVEC_ENABLE_BLOB_SOURCE" ] && [ -n "$_user_blob_override" ] && [ "$_user_blob_override" != "$(printf '%s' "$_val" | tr '[:upper:]' '[:lower:]')" ]; then
      # Validate flip direction (blocks destructive on->off; allows off->on).
      if command -v preflight_blob_flip_guard >/dev/null 2>&1; then
        if ! preflight_blob_flip_guard "$RG_NAME" "$_user_blob_override"; then
          exit 1
        fi
      fi
      azd env set "$_env" "$_user_blob_override" < /dev/null 2>/dev/null
      printf "  ${_env} = ${_user_blob_override} ${YELLOW}(user override; tag was '${_val}')${NC}\n"
      continue
    fi
    if [ -n "$_val" ]; then
      azd env set "$_env" "$_val" < /dev/null 2>/dev/null
      printf "  ${_env} = ${_val}\n"
    fi
  done
  printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
  exit 0
fi

# ── Config already set (e.g. via azd env set before azd up) — skip prompts ──
_existing_vm=$(azd_get OMNIVEC_SYSTEM_NODE_VM_SIZE)
if [ -n "$_existing_vm" ]; then
  printf "\n${GREEN}Config already set. Skipping prompts.${NC}\n"
  printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
  exit 0
fi

# ── Fresh deploy: offer auto-defaults or interactive ─────────────────────────
# a2: If we have no preset config AND no TTY, fail fast with a clear error
# (or auto-apply Quick-start if OMNIVEC_NONINTERACTIVE / CI is set).
# Must run BEFORE the first read_input.
require_tty_or_preset

echo ""
printf "${YELLOW}No configuration found. Choose setup mode:${NC}\n"
echo "  1) Quick start — use recommended defaults (fastest, no GPU)"
echo "  2) Custom     — choose VM sizes, GPU, metadata store"
echo ""
setup_mode=$(read_input "Choice [1]: ")
setup_mode=${setup_mode:-1}

if [ "$setup_mode" = "1" ]; then
  printf "\n${GREEN}Applying recommended defaults:${NC}\n"
  # Honor a pre-set blob-source preference (either var name) so users can opt out
  # of blob ingestion via `azd env set` before `azd up`.
  _qs_blob=$(azd_get OMNIVEC_ENABLE_BLOB_SOURCE)
  if [ -z "$_qs_blob" ]; then
    _qs_blob=$(azd_get AZURE_ENABLE_BLOB_SOURCE)
  fi
  _qs_blob=${_qs_blob:-true}
  azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_B4ms" < /dev/null
  azd env set OMNIVEC_SYSTEM_NODE_COUNT   "2" < /dev/null
  azd env set OMNIVEC_GPU_NODE_VM_SIZE    "" < /dev/null
  azd env set OMNIVEC_GPU_NODE_COUNT      "0" < /dev/null
  azd env set OMNIVEC_METADATA_STORE      "cosmosdb-serverless" < /dev/null
  azd env set OMNIVEC_ENABLE_BLOB_SOURCE  "$_qs_blob" < /dev/null
  echo "  OMNIVEC_SYSTEM_NODE_VM_SIZE = Standard_B4ms"
  echo "  OMNIVEC_SYSTEM_NODE_COUNT   = 2"
  echo "  OMNIVEC_GPU_NODE_VM_SIZE    = (none)"
  echo "  OMNIVEC_GPU_NODE_COUNT      = 0"
  echo "  OMNIVEC_METADATA_STORE      = cosmosdb-serverless"
  echo "  OMNIVEC_ENABLE_BLOB_SOURCE  = $_qs_blob"
  echo ""
  echo "  System pool: 2x Standard_B4ms (4 vCPU, 16 GB each)"
  echo "  GPU pool: none (use Azure OpenAI for embeddings)"
  echo "  Metadata: CosmosDB Serverless"
  if [ "$_qs_blob" = "true" ]; then
    echo "  Blob source: enabled"
  else
    echo "  Blob source: disabled (CosmosDB sources only)"
  fi
  printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
  exit 0
fi

# ── Metadata storage selection ──────────────────────────────────────────────

cur_meta=$(azd_get OMNIVEC_METADATA_STORE)
if [ -n "$cur_meta" ]; then
  printf "\n${GREEN}Metadata store: ${cur_meta} (already set)${NC}\n"
else
  echo ""
  printf "${YELLOW}Select metadata storage backend:${NC}\n"
  echo "  1) Azure CosmosDB (Serverless NoSQL)"
  echo "  2) Azure CosmosDB (Provisioned throughput)"
  echo ""
  meta_choice=$(read_input "Choice [1]: ")
  meta_choice=${meta_choice:-1}
  case "$meta_choice" in
    2)
      printf "${GREEN}Using CosmosDB Provisioned for metadata storage.${NC}\n"
      azd env set OMNIVEC_METADATA_STORE "cosmosdb-provisioned" < /dev/null
      ;;
    *)
      printf "${GREEN}Using CosmosDB Serverless for metadata storage.${NC}\n"
      azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless" < /dev/null
      ;;
  esac
fi

# ── Blob storage source ─────────────────────────────────────────────────────

cur_blob=$(azd_get OMNIVEC_ENABLE_BLOB_SOURCE)
if [ -n "$cur_blob" ]; then
  printf "${GREEN}Blob source: ${cur_blob} (already set)${NC}\n"
else
  echo ""
  printf "${YELLOW}Will you use Azure Blob Storage as a document source?${NC}\n"
  echo "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
  echo "  will be created alongside the Storage Account."
  echo ""
  echo "  1) Yes — enable blob source ingestion"
  echo "  2) No  — CosmosDB sources only (skip Service Bus + Event Grid)"
  echo ""
  blob_choice=$(read_input "Choice [1]: ")
  blob_choice=${blob_choice:-1}
  case "$blob_choice" in
    1)
      printf "${GREEN}Blob source enabled.${NC}\n"
      azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true" < /dev/null
      ;;
    *)
      printf "${GREEN}Blob source disabled.${NC}\n"
      azd env set OMNIVEC_ENABLE_BLOB_SOURCE "false" < /dev/null
      ;;
  esac
fi

# ── Node provisioning ───────────────────────────────────────────────────────

echo ""
printf "${YELLOW}Configure AKS node pools:${NC}\n"
echo ""

LOCATION="${AZURE_LOCATION:-eastus2}"

# Helper: validate a single SKU in the location
validate_sku() {
  _sku=$1
  _result=$(az vm list-skus --location "$LOCATION" --size "$_sku" --resource-type virtualMachines \
    --query "[?name=='$_sku' && (restrictions==null || restrictions[0]==null)].name" -o tsv < /dev/null 2>/dev/null || true)
  _result=$(printf '%s' "$_result" | tr -d '\r\n ')
  [ -n "$_result" ] && [ "$_result" = "$_sku" ]
}

# -- System node pool --
cur_sys_sku=$(azd_get OMNIVEC_SYSTEM_NODE_VM_SIZE)
cur_sys_count=$(azd_get OMNIVEC_SYSTEM_NODE_COUNT)

if [ -n "$cur_sys_sku" ]; then
  printf "${GREEN}System VM SKU: ${cur_sys_sku} (already set)${NC}\n"
  SYS_SKU="$cur_sys_sku"
else
  SYS_SKU=""
  while [ -z "$SYS_SKU" ]; do
    printf "${CYAN}System node pool (API, controller, worker, changefeed):${NC}\n"
    printf "  Common options:\n"
    printf "    1) Standard_D4s_v3 - 4 vCPU, 16 GB\n"
    printf "    2) Standard_D4ds_v5 - 4 vCPU, 16 GB (v5)\n"
    printf "    3) Standard_D8s_v3 - 8 vCPU, 32 GB\n"
    printf "    4) Standard_B4ms - 4 vCPU, 16 GB (burstable)\n"
    printf "    5) Standard_D2s_v3 - 2 vCPU, 8 GB (dev)\n"
    printf "    6) Enter custom SKU\n"
    echo ""
    sys_pick=$(read_input "  System VM SKU [4]: ")
    sys_pick=$(printf '%s' "${sys_pick:-4}" | tr -d ' \r\n')

    case "$sys_pick" in
      1) _candidate="Standard_D4s_v3" ;;
      2) _candidate="Standard_D4ds_v5" ;;
      3) _candidate="Standard_D8s_v3" ;;
      4) _candidate="Standard_B4ms" ;;
      5) _candidate="Standard_D2s_v3" ;;
      6)
        def_manual=${cur_sys_sku:-Standard_B4ms}
        custom_sku=$(read_input "  Enter SKU name [${def_manual}]: ")
        _candidate=$(printf '%s' "${custom_sku:-$def_manual}" | tr -d ' \r\n')
        ;;
      *) _candidate="Standard_B4ms" ;;
    esac

    printf "  ${CYAN}Validating ${_candidate} in ${LOCATION}...${NC}"
    if validate_sku "$_candidate"; then
      printf " ${GREEN}✓ available${NC}\n"
      SYS_SKU="$_candidate"
    else
      printf " ${RED}✗ not available in ${LOCATION}. Pick another.${NC}\n"
    fi
  done
  printf "  ${GREEN}System VM SKU: ${SYS_SKU}${NC}\n"
fi

if [ -n "$cur_sys_count" ]; then
  printf "${GREEN}System nodes: ${cur_sys_count} (already set)${NC}\n"
  sys_count="$cur_sys_count"
else
  sys_count=$(read_input "  System node count [2]: ")
  sys_count=${sys_count:-2}
  printf "  ${GREEN}System nodes: ${sys_count}${NC}\n"
fi

echo ""

# -- GPU node pool --
cur_gpu_sku=$(azd_get OMNIVEC_GPU_NODE_VM_SIZE)
cur_gpu_count=$(azd_get OMNIVEC_GPU_NODE_COUNT)

if [ -n "$cur_gpu_count" ]; then
  printf "${GREEN}GPU nodes: ${cur_gpu_count} (already set)${NC}\n"
  gpu_count="$cur_gpu_count"
  GPU_SKU=${cur_gpu_sku:-}
else
  printf "${CYAN}GPU node pool (ML models — dse-qwen2, clip, bge, bge-small):${NC}\n"
  echo "  Enter 0 nodes to skip GPU pool (use external models only)."
  gpu_count=$(read_input "  GPU node count (0 = no GPU pool) [0]: ")
  gpu_count=$(printf '%s' "${gpu_count:-0}" | tr -d ' \r\n')

  if [ "$gpu_count" != "0" ]; then
    printf "  Common GPU options:\n"
    printf "    1) Standard_NC4as_T4_v3 - 4 vCPU, 28 GB, 1x T4 16GB\n"
    printf "    2) Standard_NC6s_v3 - 6 vCPU, 112 GB, 1x V100 16GB\n"
    printf "    3) Standard_NC8as_T4_v3 - 8 vCPU, 56 GB, 1x T4 16GB\n"
    printf "    4) Standard_NC12s_v3 - 12 vCPU, 224 GB, 2x V100\n"
    printf "    5) Standard_NC24ads_A100_v4 - 24 vCPU, 220 GB, 1x A100 80GB\n"
    printf "    6) Enter custom SKU\n"
    echo ""
    gpu_pick=$(read_input "  GPU VM SKU [1]: ")
    gpu_pick=$(printf '%s' "${gpu_pick:-1}" | tr -d ' \r\n')

    case "$gpu_pick" in
      1) GPU_SKU="Standard_NC4as_T4_v3" ;;
      2) GPU_SKU="Standard_NC6s_v3" ;;
      3) GPU_SKU="Standard_NC8as_T4_v3" ;;
      4) GPU_SKU="Standard_NC12s_v3" ;;
      5) GPU_SKU="Standard_NC24ads_A100_v4" ;;
      6)
        def_gpu_manual=${cur_gpu_sku:-Standard_NC4as_T4_v3}
        custom_gpu=$(read_input "  Enter SKU name [${def_gpu_manual}]: ")
        GPU_SKU=$(printf '%s' "${custom_gpu:-$def_gpu_manual}" | tr -d ' \r\n')
        ;;
      *) GPU_SKU="Standard_NC4as_T4_v3" ;;
    esac

    printf "  ${CYAN}Validating ${GPU_SKU} in ${LOCATION}...${NC}"
    if validate_sku "$GPU_SKU"; then
      printf " ${GREEN}✓ available${NC}\n"
    else
      printf " ${YELLOW}⚠ could not confirm availability (proceeding anyway)${NC}\n"
    fi
    printf "  ${GREEN}GPU VM: ${GPU_SKU}, nodes: ${gpu_count}${NC}\n"
  else
    printf "  ${YELLOW}GPU pool disabled — using external embedding models only.${NC}\n"
    GPU_SKU=${cur_gpu_sku:-}
  fi
fi

# Validate before storing
if [ -z "$SYS_SKU" ]; then
  printf "${RED}No system VM SKU selected. Cannot proceed.${NC}\n"
  exit 1
fi

# Store in azd env for Bicep parameter substitution
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "$SYS_SKU" < /dev/null
azd env set OMNIVEC_SYSTEM_NODE_COUNT "$sys_count" < /dev/null
azd env set OMNIVEC_GPU_NODE_VM_SIZE "$GPU_SKU" < /dev/null
azd env set OMNIVEC_GPU_NODE_COUNT "$gpu_count" < /dev/null

# ── Sanitize env values: strip BOM, tabs, carriage returns ──────────────────
printf "\n${CYAN}Sanitizing environment values...${NC}\n"
for key in OMNIVEC_SYSTEM_NODE_VM_SIZE OMNIVEC_SYSTEM_NODE_COUNT OMNIVEC_GPU_NODE_VM_SIZE OMNIVEC_GPU_NODE_COUNT OMNIVEC_ENABLE_BLOB_SOURCE OMNIVEC_METADATA_STORE; do
  raw=$(azd_get "$key")
  if [ -n "$raw" ]; then
    clean=$(printf '%s' "$raw" | tr -d '\r\t' | sed 's/^\xEF\xBB\xBF//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ "$raw" != "$clean" ]; then
      azd env set "$key" "$clean" < /dev/null
      printf "  ${YELLOW}Cleaned ${key}: removed hidden characters${NC}\n"
    fi
  fi
done

printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
printf "${CYAN}Environment: ${AZURE_ENV_NAME}${NC}\n"
printf "${CYAN}Each installation gets a unique resource token derived from (subscription + resource group + env name).${NC}\n"

# ── b4/c2: Final preflight — quota + blob-flip guard + re-sanitize ──────────
if command -v preflight_blob_flip_guard >/dev/null 2>&1; then
  _want_blob=$(azd_get OMNIVEC_ENABLE_BLOB_SOURCE)
  preflight_blob_flip_guard "$RG_NAME" "${_want_blob:-true}" || exit 1
fi
if command -v preflight_vcpu_quota >/dev/null 2>&1; then
  _sys_sku=$(azd_get OMNIVEC_SYSTEM_NODE_VM_SIZE)
  _sys_count=$(azd_get OMNIVEC_SYSTEM_NODE_COUNT)
  _gpu_sku=$(azd_get OMNIVEC_GPU_NODE_VM_SIZE)
  _gpu_count=$(azd_get OMNIVEC_GPU_NODE_COUNT)
  preflight_vcpu_quota "$LOCATION" "$_sys_sku" "$_sys_count" "$_gpu_sku" "$_gpu_count" || true
fi
if command -v preflight_sanitize_env >/dev/null 2>&1; then
  for _k in OMNIVEC_SYSTEM_NODE_VM_SIZE OMNIVEC_SYSTEM_NODE_COUNT OMNIVEC_GPU_NODE_VM_SIZE OMNIVEC_GPU_NODE_COUNT OMNIVEC_ENABLE_BLOB_SOURCE OMNIVEC_METADATA_STORE; do
    preflight_sanitize_env "$_k" || true
  done
fi
