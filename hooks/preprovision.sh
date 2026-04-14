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

# Helper: safely read an azd env value, returns empty string on failure
azd_get() {
  _val=$(azd env get-value "$1" 2>/dev/null) && printf '%s' "$_val" | tr -d '\r' || printf ''
}

# Helper: read user input from TTY (works in hook context where stdin may be redirected)
read_input() {
  _prompt=$1
  printf '%b' "$_prompt" >/dev/tty 2>/dev/null || printf '%b' "$_prompt" >&2
  _input=""
  if [ -t 0 ]; then
    read -r _input || true
  elif [ -e /dev/tty ]; then
    read -r _input </dev/tty || true
  fi
  printf '%s' "$_input"
}

DEPLOYMENT_DETECTED=false
IN_PLACE_UPDATE=false



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
  az aks install-cli --install-location "$KUBECTL_DIR/kubectl" --kubelogin-install-location "$KUBECTL_DIR/kubelogin" 2>/dev/null || true
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
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | HELM_INSTALL_DIR="$HELM_INSTALL_DIR" USE_SUDO="false" sh 2>/dev/null || true
  if ! command -v helm >/dev/null 2>&1; then
    printf "  ${RED}Failed to install helm. Install manually: https://helm.sh/docs/intro/install/${NC}\n"
    exit 1
  fi
  printf "  ${GREEN}helm installed.${NC}\n"
else
  printf "  ${GREEN}helm found.${NC}\n"
fi

printf "${GREEN}All prerequisites met.${NC}\n"

# Init submodules if needed
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ ! -f "$REPO_ROOT/docgrok/Dockerfile" ]; then
  printf "  ${YELLOW}Initializing git submodules...${NC}\n"
  (cd "$REPO_ROOT" && git submodule update --init --recursive 2>/dev/null) || true
fi

# ── Validate Azure login ────────────────────────────────────────────────────

printf "\n${YELLOW}Checking Azure login...${NC}\n"
if ! az account show >/dev/null 2>&1; then
  printf "${RED}Not logged into Azure. Run 'az login' first.${NC}\n"
  exit 1
fi

SUBSCRIPTION=$(az account show --query name -o tsv)
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
printf "${GREEN}Logged in to subscription: ${SUBSCRIPTION} (${SUBSCRIPTION_ID})${NC}\n"

# ── Check for existing deployment (RG exists + config present = update in-place) ─
RG_NAME="rg-omnivec-${AZURE_ENV_NAME}"
RG_EXISTS=$(az group exists --name "$RG_NAME" 2>/dev/null || echo "false")
RG_EXISTS=$(printf '%s' "$RG_EXISTS" | tr -d '\r\n ')

if [ "$RG_EXISTS" = "true" ]; then
  printf "\n${GREEN}Existing deployment detected (RG: ${RG_NAME}). Importing config from tags...${NC}\n"
  _tags=$(az group show --name "$RG_NAME" --query "tags" -o json 2>/dev/null || echo "{}")
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
    _val=$(echo "$_tags" | python3 -c "import sys,json; t=json.load(sys.stdin); print(t.get('$_tag',''))" 2>/dev/null || true)
    if [ -n "$_val" ]; then
      azd env set "$_env" "$_val" 2>/dev/null
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
echo ""
printf "${YELLOW}No configuration found. Choose setup mode:${NC}\n"
echo "  1) Quick start — use recommended defaults (fastest, no GPU)"
echo "  2) Custom     — choose VM sizes, GPU, metadata store"
echo ""
setup_mode=$(read_input "Choice [1]: ")
setup_mode=${setup_mode:-1}

if [ "$setup_mode" = "1" ]; then
  printf "\n${GREEN}Applying recommended defaults:${NC}\n"
  azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_B4ms"
  azd env set OMNIVEC_SYSTEM_NODE_COUNT   "2"
  azd env set OMNIVEC_GPU_NODE_VM_SIZE    ""
  azd env set OMNIVEC_GPU_NODE_COUNT      "0"
  azd env set OMNIVEC_METADATA_STORE      "cosmosdb-serverless"
  azd env set OMNIVEC_ENABLE_BLOB_SOURCE  "true"
  echo "  OMNIVEC_SYSTEM_NODE_VM_SIZE = Standard_B4ms"
  echo "  OMNIVEC_SYSTEM_NODE_COUNT   = 2"
  echo "  OMNIVEC_GPU_NODE_VM_SIZE    = (none)"
  echo "  OMNIVEC_GPU_NODE_COUNT      = 0"
  echo "  OMNIVEC_METADATA_STORE      = cosmosdb-serverless"
  echo "  OMNIVEC_ENABLE_BLOB_SOURCE  = true"
  echo ""
  echo "  System pool: 2x Standard_B4ms (4 vCPU, 16 GB each)"
  echo "  GPU pool: none (use Azure OpenAI for embeddings)"
  echo "  Metadata: CosmosDB Serverless"
  echo "  Blob source: enabled"
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
      azd env set OMNIVEC_METADATA_STORE "cosmosdb-provisioned"
      ;;
    *)
      printf "${GREEN}Using CosmosDB Serverless for metadata storage.${NC}\n"
      azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
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
      azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
      ;;
    *)
      printf "${GREEN}Blob source disabled.${NC}\n"
      azd env set OMNIVEC_ENABLE_BLOB_SOURCE "false"
      ;;
  esac
fi

# ── Node provisioning ───────────────────────────────────────────────────────

echo ""
printf "${YELLOW}Configure AKS node pools:${NC}\n"
echo ""

LOCATION="${AZURE_LOCATION:-centralus}"

# Helper: validate a single SKU in the location
validate_sku() {
  _sku=$1
  _result=$(az vm list-skus --location "$LOCATION" --size "$_sku" --resource-type virtualMachines \
    --query "[?name=='$_sku' && (restrictions==null || restrictions[0]==null)].name" -o tsv 2>/dev/null || true)
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
  printf "${CYAN}System node pool (API, controller, worker, changefeed):${NC}\n"
  printf "  Common options:\n"
  printf "    1) Standard_D4s_v3 - 4 vCPU, 16 GB\n"
  printf "    2) Standard_D4ds_v5 - 4 vCPU, 16 GB (v5)\n"
  printf "    3) Standard_D8s_v3 - 8 vCPU, 32 GB\n"
  printf "    4) Standard_B4ms - 4 vCPU, 16 GB (burstable)\n"
  printf "    5) Standard_D2s_v3 - 2 vCPU, 8 GB (dev)\n"
  printf "    6) Enter custom SKU\n"
  echo ""
  printf "  System VM SKU [4]: " >/dev/tty 2>/dev/null || printf "  System VM SKU [4]: " >&2
  sys_pick=""
  if [ -t 0 ]; then read -r sys_pick || true; elif [ -e /dev/tty ]; then read -r sys_pick </dev/tty || true; fi
  sys_pick=$(printf '%s' "${sys_pick:-4}" | tr -d ' \r\n')

  case "$sys_pick" in
    1) SYS_SKU="Standard_D4s_v3" ;;
    2) SYS_SKU="Standard_D4ds_v5" ;;
    3) SYS_SKU="Standard_D8s_v3" ;;
    4) SYS_SKU="Standard_B4ms" ;;
    5) SYS_SKU="Standard_D2s_v3" ;;
    6)
      def_manual=${cur_sys_sku:-Standard_B4ms}
      printf "  Enter SKU name [${def_manual}]: " >/dev/tty 2>/dev/null || printf "  Enter SKU name [${def_manual}]: " >&2
      custom_sku=""
      if [ -t 0 ]; then read -r custom_sku || true; elif [ -e /dev/tty ]; then read -r custom_sku </dev/tty || true; fi
      SYS_SKU=$(printf '%s' "${custom_sku:-$def_manual}" | tr -d ' \r\n')
      ;;
    *) SYS_SKU="Standard_B4ms" ;;
  esac

  printf "  ${CYAN}Validating ${SYS_SKU} in ${LOCATION}...${NC}"
  if validate_sku "$SYS_SKU"; then
    printf " ${GREEN}✓ available${NC}\n"
  else
    printf " ${YELLOW}⚠ could not confirm availability (proceeding anyway)${NC}\n"
  fi
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
  printf "  GPU node count (0 = no GPU pool) [0]: " >/dev/tty 2>/dev/null || printf "  GPU node count (0 = no GPU pool) [0]: " >&2
  gpu_count=""
  if [ -t 0 ]; then read -r gpu_count || true; elif [ -e /dev/tty ]; then read -r gpu_count </dev/tty || true; fi
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
    printf "  GPU VM SKU [1]: " >/dev/tty 2>/dev/null || printf "  GPU VM SKU [1]: " >&2
    gpu_pick=""
    if [ -t 0 ]; then read -r gpu_pick || true; elif [ -e /dev/tty ]; then read -r gpu_pick </dev/tty || true; fi
    gpu_pick=$(printf '%s' "${gpu_pick:-1}" | tr -d ' \r\n')

    case "$gpu_pick" in
      1) GPU_SKU="Standard_NC4as_T4_v3" ;;
      2) GPU_SKU="Standard_NC6s_v3" ;;
      3) GPU_SKU="Standard_NC8as_T4_v3" ;;
      4) GPU_SKU="Standard_NC12s_v3" ;;
      5) GPU_SKU="Standard_NC24ads_A100_v4" ;;
      6)
        def_gpu_manual=${cur_gpu_sku:-Standard_NC4as_T4_v3}
        printf "  Enter SKU name [${def_gpu_manual}]: " >/dev/tty 2>/dev/null || printf "  Enter SKU name [${def_gpu_manual}]: " >&2
        custom_gpu=""
        if [ -t 0 ]; then read -r custom_gpu || true; elif [ -e /dev/tty ]; then read -r custom_gpu </dev/tty || true; fi
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
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "$SYS_SKU"
azd env set OMNIVEC_SYSTEM_NODE_COUNT "$sys_count"
azd env set OMNIVEC_GPU_NODE_VM_SIZE "$GPU_SKU"
azd env set OMNIVEC_GPU_NODE_COUNT "$gpu_count"

# ── Sanitize env values: strip BOM, tabs, carriage returns ──────────────────
printf "\n${CYAN}Sanitizing environment values...${NC}\n"
for key in OMNIVEC_SYSTEM_NODE_VM_SIZE OMNIVEC_SYSTEM_NODE_COUNT OMNIVEC_GPU_NODE_VM_SIZE OMNIVEC_GPU_NODE_COUNT OMNIVEC_ENABLE_BLOB_SOURCE OMNIVEC_METADATA_STORE; do
  raw=$(azd_get "$key")
  if [ -n "$raw" ]; then
    clean=$(printf '%s' "$raw" | tr -d '\r\t' | sed 's/^\xEF\xBB\xBF//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ "$raw" != "$clean" ]; then
      azd env set "$key" "$clean"
      printf "  ${YELLOW}Cleaned ${key}: removed hidden characters${NC}\n"
    fi
  fi
done

printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
printf "${CYAN}Environment: ${AZURE_ENV_NAME}${NC}\n"
printf "${CYAN}Each installation gets a unique resource token derived from (subscription + resource group + env name).${NC}\n"