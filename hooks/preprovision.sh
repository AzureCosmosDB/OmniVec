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
      printf "  ${YELLOW}Take over lock and continue? [y/N]: ${NC}"
      read -r force_lock || true
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

# ── Check for existing healthy deployment ───────────────────────────────────
# If pods are already running and healthy, warn before re-deploying
EXISTING_AKS=$(azd env get-value AZURE_AKS_CLUSTER_NAME 2>/dev/null || true)
EXISTING_RG=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || true)
DEPLOYMENT_DETECTED=false
IN_PLACE_UPDATE=false

if [ -n "$EXISTING_AKS" ] && [ -n "$EXISTING_RG" ]; then
  # Try to get credentials and check pod health (silently)
  KUBE_CTX="$EXISTING_AKS"
  az aks get-credentials --resource-group "$EXISTING_RG" --name "$EXISTING_AKS" --context "$KUBE_CTX" --overwrite-existing 2>/dev/null || true

  HEALTHY_PODS=$(kubectl --context "$KUBE_CTX" get pods -n omnivec --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")

  if [ "$HEALTHY_PODS" -gt 0 ]; then
    DEPLOYMENT_DETECTED=true
    printf "\n${YELLOW}Existing healthy deployment detected (${HEALTHY_PODS} running pods in omnivec).${NC}\n"
    printf "  AKS:  ${CYAN}${EXISTING_AKS}${NC}\n"
    printf "  RG:   ${CYAN}${EXISTING_RG}${NC}\n"
    printf "\n  ${CYAN}1) Update in-place (default)${NC}\n"
    printf "  ${CYAN}2) Teardown and redeploy fresh${NC}\n"
    printf "  ${CYAN}3) Abort${NC}\n"
    printf "\n  Choice [1]: "
    read -r DEPLOY_CHOICE </dev/tty 2>/dev/null || DEPLOY_CHOICE="1"
    DEPLOY_CHOICE=${DEPLOY_CHOICE:-1}
    case "$DEPLOY_CHOICE" in
      1)
        printf "  ${GREEN}Proceeding with in-place update.${NC}\n"
        IN_PLACE_UPDATE=true
        ;;
      2)
        printf "  ${YELLOW}Tearing down existing deployment first...${NC}\n"
        azd down --force --purge
        printf "  ${GREEN}Teardown complete. Proceeding with fresh deployment.${NC}\n"
        DEPLOYMENT_DETECTED=false
        ;;
      3)
        printf "  ${RED}Aborted by user.${NC}\n"
        printf "  ${YELLOW}(The ERROR message below is expected — it is how azd stops.)${NC}\n"
        exit 1
        ;;
      *)
        printf "  ${GREEN}Proceeding with in-place update (default).${NC}\n"
        IN_PLACE_UPDATE=true
        ;;
    esac
  fi
fi

# ── In-place update: only allow node count changes, then proceed ─────────
if [ "$IN_PLACE_UPDATE" = "true" ]; then
  printf "\n${CYAN}In-place update — only node count changes allowed.${NC}\n"
  cur_sys_count=$(azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>/dev/null || true)
  cur_sys_count=$(printf '%s' "$cur_sys_count" | tr -d '\r')
  def_sys_count=${cur_sys_count:-2}
  printf "  System node count [${def_sys_count}]: "
  read -r sys_count </dev/tty 2>/dev/null || sys_count=""
  sys_count=${sys_count:-$def_sys_count}
  azd env set OMNIVEC_SYSTEM_NODE_COUNT "$sys_count"

  cur_gpu_count=$(azd env get-value OMNIVEC_GPU_NODE_COUNT 2>/dev/null || true)
  cur_gpu_count=$(printf '%s' "$cur_gpu_count" | tr -d '\r')
  def_gpu_count=${cur_gpu_count:-0}
  printf "  GPU node count [${def_gpu_count}]: "
  read -r gpu_count </dev/tty 2>/dev/null || gpu_count=""
  gpu_count=${gpu_count:-$def_gpu_count}
  azd env set OMNIVEC_GPU_NODE_COUNT "$gpu_count"

  printf "  ${GREEN}System nodes: ${sys_count}, GPU nodes: ${gpu_count}${NC}\n"
  printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
  exit 0
fi

# ── Resume detection ────────────────────────────────────────────────────────
# If config is already set (from a previous run), skip interactive prompts

EXISTING_CONFIG=$(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>/dev/null || true)
if [ -n "$EXISTING_CONFIG" ]; then
  printf "\n${CYAN}Configuration for environment '${AZURE_ENV_NAME}':${NC}\n"
  echo "  System SKU:      $(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>/dev/null)"
  echo "  System nodes:    $(azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>/dev/null)"
  echo "  GPU SKU:         $(azd env get-value OMNIVEC_GPU_NODE_VM_SIZE 2>/dev/null)"
  echo "  GPU nodes:       $(azd env get-value OMNIVEC_GPU_NODE_COUNT 2>/dev/null)"
  echo "  Blob source:     $(azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>/dev/null)"
  echo "  Metadata store:  $(azd env get-value OMNIVEC_METADATA_STORE 2>/dev/null)"
  echo ""
  printf "  ${YELLOW}Keep these settings? [Y/n] (n = reconfigure from scratch): ${NC}"
  read -r reuse || true
  reuse=${reuse:-Y}
  case "$reuse" in
    [nN]*)
      printf "  ${GREEN}Reconfiguring — current values shown as defaults, press Enter to keep.${NC}\n"
      ;;
    *)
      printf "  ${GREEN}Using existing settings, skipping configuration prompts.${NC}\n"
      exit 0
      ;;
  esac
fi

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

# ── Check for existing OmniVec installations (skip if already detected above) ─

if [ "$DEPLOYMENT_DETECTED" = "false" ]; then
  printf "\n${YELLOW}Checking for existing OmniVec installations in subscription...${NC}\n"

EXISTING=$(az resource list --query "[?tags.\"omnivec-instance\" != null].{name:name, type:type, rg:resourceGroup, instance:tags.\"omnivec-instance\"}" -o json 2>/dev/null || echo "[]")

INSTANCES=$(echo "$EXISTING" | python3 -c "
import sys, json
resources = json.load(sys.stdin)
instances = {}
for r in resources:
    iid = r['instance']
    instances.setdefault(iid, []).append(r)
for iid, res in sorted(instances.items()):
    rg = res[0]['rg']
    types = set(r['type'].split('/')[-1] for r in res)
    print(f'{iid}\t{rg}\t{len(res)} resources ({\", \".join(sorted(types))})')
" 2>/dev/null || true)

INSTANCE_COUNT=$(echo "$INSTANCES" | grep -c '[^[:space:]]' || true)

if [ "$INSTANCE_COUNT" -gt 0 ]; then
  printf "${CYAN}Found ${INSTANCE_COUNT} existing OmniVec installation(s):${NC}\n"
  echo ""
  echo "$INSTANCES" | while IFS='	' read -r iid rg summary; do
    printf "  ${CYAN}${iid}${NC}  (rg: ${rg}, ${summary})\n"
  done
  echo ""
  printf "${YELLOW}What would you like to do?${NC}\n"
  echo "  1) Launch a NEW OmniVec installation (unique resources alongside existing)"
  echo "  2) Cancel deployment"
  echo ""
  printf "Choice [1/2]: "
  read -r choice || true
  case "$choice" in
    1) printf "${GREEN}Creating new installation with environment '${AZURE_ENV_NAME}'.${NC}\n" ;;
    *) printf "${RED}Deployment cancelled.${NC}\n"; exit 1 ;;
  esac
else
  printf "${GREEN}No existing OmniVec installations found. This will be a fresh deployment.${NC}\n"
fi
fi

# ── Metadata storage selection ──────────────────────────────────────────────

cur_meta=$(azd env get-value OMNIVEC_METADATA_STORE 2>/dev/null || true)
cur_meta=$(printf '%s' "$cur_meta" | tr -d '\r')
def_meta_num=1
meta_mark1=" (current)"
meta_mark2=""
if [ "$cur_meta" = "cosmosdb-provisioned" ]; then
  def_meta_num=2; meta_mark1=""; meta_mark2=" (current)"
fi

echo ""
printf "${YELLOW}Select metadata storage backend:${NC}\n"
echo "  1) Azure CosmosDB (Serverless NoSQL)${meta_mark1}"
echo "  2) Azure CosmosDB (Provisioned throughput)${meta_mark2}"
echo ""
printf "Choice [${def_meta_num}]: "
read -r meta_choice || true
meta_choice=${meta_choice:-$def_meta_num}

case "$meta_choice" in
  1)
    printf "${GREEN}Using CosmosDB Serverless for metadata storage.${NC}\n"
    azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
    ;;
  2)
    printf "${GREEN}Using CosmosDB Provisioned for metadata storage.${NC}\n"
    azd env set OMNIVEC_METADATA_STORE "cosmosdb-provisioned"
    ;;
  *)
    printf "${YELLOW}Invalid choice, defaulting to CosmosDB Serverless.${NC}\n"
    azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
    ;;
esac

# ── Blob storage source ─────────────────────────────────────────────────────

cur_blob=$(azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>/dev/null || true)
cur_blob=$(printf '%s' "$cur_blob" | tr -d '\r')
def_blob_num=1
blob_mark1=" (current)"
blob_mark2=""
if [ "$cur_blob" = "false" ]; then
  def_blob_num=2; blob_mark1=""; blob_mark2=" (current)"
fi

echo ""
printf "${YELLOW}Will you use Azure Blob Storage as a document source?${NC}\n"
echo "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
echo "  will be created alongside the Storage Account."
echo ""
echo "  1) Yes — enable blob source ingestion${blob_mark1}"
echo "  2) No  — CosmosDB sources only (skip Service Bus + Event Grid)${blob_mark2}"
echo ""
printf "Choice [${def_blob_num}]: "
read -r blob_choice || true
blob_choice=${blob_choice:-$def_blob_num}

case "$blob_choice" in
  1)
    printf "${GREEN}Blob source enabled — will create Storage Account, Service Bus, and Event Grid.${NC}\n"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
    ;;
  *)
    printf "${GREEN}Blob source disabled — skipping Service Bus and Event Grid.${NC}\n"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "false"
    ;;
esac

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
  [ -n "$_result" ] && [ "$_result" = "$_sku" ]
}

# -- System node pool --
printf "${CYAN}System node pool (API, controller, worker, changefeed):${NC}\n"
cur_sys_sku=$(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>/dev/null || true)
cur_sys_sku=$(printf '%s' "$cur_sys_sku" | tr -d '\r')

SYS_CANDIDATES="Standard_D4s_v3:4 vCPU, 16 GB
Standard_D4ds_v5:4 vCPU, 16 GB (v5)
Standard_D8s_v3:8 vCPU, 32 GB
Standard_B4ms:4 vCPU, 16 GB (burstable)
Standard_D2s_v3:2 vCPU, 8 GB (dev)"
SYS_TOTAL=$(echo "$SYS_CANDIDATES" | wc -l | tr -d ' ')

printf "  Common options:\n"
DEF_SYS_IDX=1
i=0
echo "$SYS_CANDIDATES" | while IFS=: read -r sku desc; do
  i=$((i + 1))
  mark=""
  if [ -n "$cur_sys_sku" ] && [ "$sku" = "$cur_sys_sku" ]; then mark=" (current)"; fi
  printf "    ${i}) ${sku} - ${desc}${mark}\n"
done
# Recompute default index outside subshell
i=0
echo "$SYS_CANDIDATES" | while IFS=: read -r sku desc; do
  i=$((i + 1))
  if [ -n "$cur_sys_sku" ] && [ "$sku" = "$cur_sys_sku" ]; then echo "$i"; fi
done > /tmp/_omnivec_def_sys_idx
_def=$(cat /tmp/_omnivec_def_sys_idx 2>/dev/null | head -1)
DEF_SYS_IDX=${_def:-1}
rm -f /tmp/_omnivec_def_sys_idx

CUSTOM_IDX=$((SYS_TOTAL + 1))
printf "    ${CUSTOM_IDX}) Enter custom SKU\n"
echo ""

SYS_SKU=""
FAILED_SYS_SKUS=""
while [ -z "$SYS_SKU" ]; do
  # Re-display with failed markers
  printf "  Common options:\n"
  next_default=""
  i=0
  echo "$SYS_CANDIDATES" | while IFS=: read -r sku desc; do
    i=$((i + 1))
    mark=""
    if [ -n "$cur_sys_sku" ] && [ "$sku" = "$cur_sys_sku" ]; then mark=" (current)"; fi
    if echo "$FAILED_SYS_SKUS" | grep -q "|${sku}|" 2>/dev/null; then mark=" ${RED}[✗ unavailable]${NC}"; fi
    printf "    ${i}) ${sku} - ${desc}${mark}\n"
  done
  # Find next untried default
  i=0
  echo "$SYS_CANDIDATES" | while IFS=: read -r sku desc; do
    i=$((i + 1))
    if ! echo "$FAILED_SYS_SKUS" | grep -q "|${sku}|" 2>/dev/null; then echo "$i"; fi
  done > /tmp/_omnivec_next_def
  next_default=$(head -1 /tmp/_omnivec_next_def 2>/dev/null)
  next_default=${next_default:-$DEF_SYS_IDX}
  rm -f /tmp/_omnivec_next_def

  printf "    ${CUSTOM_IDX}) Enter custom SKU\n"
  echo ""
  printf "  System VM SKU [${next_default}]: "
  read -r sys_pick || true
  sys_pick=${sys_pick:-$next_default}

  if [ "$sys_pick" = "$CUSTOM_IDX" ]; then
    def_manual=${cur_sys_sku:-Standard_D4s_v3}
    printf "  Enter SKU name [${def_manual}]: "
    read -r candidate || true
    candidate=${candidate:-$def_manual}
  else
    candidate=$(echo "$SYS_CANDIDATES" | sed -n "${sys_pick}p" | cut -d: -f1)
    if [ -z "$candidate" ]; then
      candidate=$(echo "$SYS_CANDIDATES" | sed -n "${next_default}p" | cut -d: -f1)
    fi
  fi

  # Skip already-failed SKUs
  if echo "$FAILED_SYS_SKUS" | grep -q "|${candidate}|" 2>/dev/null; then
    printf "  ${RED}${candidate} already checked — not available. Pick another.${NC}\n"
    continue
  fi

  printf "  ${CYAN}Validating ${candidate} in ${LOCATION}...${NC}"
  if validate_sku "$candidate"; then
    printf " ${GREEN}✓ available${NC}\n"
    SYS_SKU="$candidate"
  else
    printf " ${RED}✗ not available in ${LOCATION}${NC}\n"
    FAILED_SYS_SKUS="${FAILED_SYS_SKUS}|${candidate}|"
  fi
done
printf "  ${GREEN}System VM SKU: ${SYS_SKU}${NC}\n"

cur_sys_count=$(azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>/dev/null || true)
cur_sys_count=$(printf '%s' "$cur_sys_count" | tr -d '\r')
def_sys_count=${cur_sys_count:-2}
printf "  System node count [${def_sys_count}]: "
read -r sys_count || true
sys_count=${sys_count:-$def_sys_count}
printf "  ${GREEN}System nodes: ${sys_count}${NC}\n"

echo ""

# -- GPU node pool --
printf "${CYAN}GPU node pool (ML models — dse-qwen2, clip, bge, bge-small):${NC}\n"
echo "  Enter 0 nodes to skip GPU pool (use external models only)."
cur_gpu_sku=$(azd env get-value OMNIVEC_GPU_NODE_VM_SIZE 2>/dev/null || true)
cur_gpu_sku=$(printf '%s' "$cur_gpu_sku" | tr -d '\r')

cur_gpu_count=$(azd env get-value OMNIVEC_GPU_NODE_COUNT 2>/dev/null || true)
cur_gpu_count=$(printf '%s' "$cur_gpu_count" | tr -d '\r')
def_gpu_count=${cur_gpu_count:-0}

printf "  GPU node count (0 = no GPU pool) [${def_gpu_count}]: "
read -r gpu_count || true
gpu_count=${gpu_count:-$def_gpu_count}

if [ "$gpu_count" != "0" ]; then
  GPU_CANDIDATES="Standard_NC4as_T4_v3:4 vCPU, 28 GB, 1x T4 16GB
Standard_NC6s_v3:6 vCPU, 112 GB, 1x V100 16GB
Standard_NC8as_T4_v3:8 vCPU, 56 GB, 1x T4 16GB
Standard_NC12s_v3:12 vCPU, 224 GB, 2x V100
Standard_NC24ads_A100_v4:24 vCPU, 220 GB, 1x A100 80GB"
  GPU_TOTAL=$(echo "$GPU_CANDIDATES" | wc -l | tr -d ' ')

  i=0
  echo "$GPU_CANDIDATES" | while IFS=: read -r sku desc; do
    i=$((i + 1))
    if [ -n "$cur_gpu_sku" ] && [ "$sku" = "$cur_gpu_sku" ]; then echo "$i"; fi
  done > /tmp/_omnivec_def_gpu_idx
  _gdef=$(cat /tmp/_omnivec_def_gpu_idx 2>/dev/null | head -1)
  DEF_GPU_IDX=${_gdef:-1}
  rm -f /tmp/_omnivec_def_gpu_idx

  GPU_CUSTOM_IDX=$((GPU_TOTAL + 1))

  GPU_SKU=""
  FAILED_GPU_SKUS=""
  while [ -z "$GPU_SKU" ]; do
    printf "  Common GPU options:\n"
    next_gpu_default=""
    i=0
    echo "$GPU_CANDIDATES" | while IFS=: read -r sku desc; do
      i=$((i + 1))
      mark=""
      if [ -n "$cur_gpu_sku" ] && [ "$sku" = "$cur_gpu_sku" ]; then mark=" (current)"; fi
      if echo "$FAILED_GPU_SKUS" | grep -q "|${sku}|" 2>/dev/null; then mark=" ${RED}[✗ unavailable]${NC}"; fi
      printf "    ${i}) ${sku} - ${desc}${mark}\n"
    done
    i=0
    echo "$GPU_CANDIDATES" | while IFS=: read -r sku desc; do
      i=$((i + 1))
      if ! echo "$FAILED_GPU_SKUS" | grep -q "|${sku}|" 2>/dev/null; then echo "$i"; fi
    done > /tmp/_omnivec_next_gpu_def
    next_gpu_default=$(head -1 /tmp/_omnivec_next_gpu_def 2>/dev/null)
    next_gpu_default=${next_gpu_default:-$DEF_GPU_IDX}
    rm -f /tmp/_omnivec_next_gpu_def

    printf "    ${GPU_CUSTOM_IDX}) Enter custom SKU\n"
    echo ""
    printf "  GPU VM SKU [${next_gpu_default}]: "
    read -r gpu_pick || true
    gpu_pick=${gpu_pick:-$next_gpu_default}

    if [ "$gpu_pick" = "$GPU_CUSTOM_IDX" ]; then
      def_gpu_manual=${cur_gpu_sku:-Standard_NC4as_T4_v3}
      printf "  Enter SKU name [${def_gpu_manual}]: "
      read -r candidate || true
      candidate=${candidate:-$def_gpu_manual}
    else
      candidate=$(echo "$GPU_CANDIDATES" | sed -n "${gpu_pick}p" | cut -d: -f1)
      if [ -z "$candidate" ]; then
        candidate=$(echo "$GPU_CANDIDATES" | sed -n "${next_gpu_default}p" | cut -d: -f1)
      fi
    fi

    if echo "$FAILED_GPU_SKUS" | grep -q "|${candidate}|" 2>/dev/null; then
      printf "  ${RED}${candidate} already checked — not available. Pick another.${NC}\n"
      continue
    fi

    printf "  ${CYAN}Validating ${candidate} in ${LOCATION}...${NC}"
    if validate_sku "$candidate"; then
      printf " ${GREEN}✓ available${NC}\n"
      GPU_SKU="$candidate"
    else
      printf " ${RED}✗ not available in ${LOCATION}${NC}\n"
      FAILED_GPU_SKUS="${FAILED_GPU_SKUS}|${candidate}|"
    fi
  done
  printf "  ${GREEN}GPU VM: ${GPU_SKU}, nodes: ${gpu_count}${NC}\n"
else
  printf "  ${YELLOW}GPU pool disabled — using external embedding models only.${NC}\n"
  GPU_SKU=${cur_gpu_sku:-}
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

# ── Check image build capability ────────────────────────────────────────────

printf "\n${YELLOW}Checking image build capability...${NC}\n"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  printf "${GREEN}Docker daemon available — will use local builds.${NC}\n"
  azd env set OMNIVEC_BUILD_MODE "docker"
else
  printf "${YELLOW}No Docker daemon — will use 'az acr build' for remote builds.${NC}\n"
  azd env set OMNIVEC_BUILD_MODE "acr"
fi

# ── Check for soft-deleted Key Vault with the expected name ────────────────
# The vault name uses the same prefix-resourceToken pattern as other resources.
# We check if a soft-deleted vault with that name exists so Bicep can recover
# it instead of failing with a "vault already exists in deleted state" error.

# ── Sanitize env values: strip BOM, tabs, carriage returns ──────────────────
printf "\n${CYAN}Sanitizing environment values...${NC}\n"
for key in OMNIVEC_SYSTEM_NODE_VM_SIZE OMNIVEC_SYSTEM_NODE_COUNT OMNIVEC_GPU_NODE_VM_SIZE OMNIVEC_GPU_NODE_COUNT OMNIVEC_ENABLE_BLOB_SOURCE OMNIVEC_METADATA_STORE OMNIVEC_BUILD_MODE; do
  raw=$(azd env get-value "$key" 2>/dev/null || true)
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
