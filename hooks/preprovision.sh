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

# ── Resume detection ────────────────────────────────────────────────────────
# If config is already set (from a previous run), skip interactive prompts

EXISTING_CONFIG=$(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>/dev/null || true)
if [ -n "$EXISTING_CONFIG" ]; then
  printf "\n${CYAN}Resuming previous configuration for environment '${AZURE_ENV_NAME}':${NC}\n"
  echo "  System SKU:      $(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>/dev/null)"
  echo "  System nodes:    $(azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>/dev/null)"
  echo "  GPU SKU:         $(azd env get-value OMNIVEC_GPU_NODE_VM_SIZE 2>/dev/null)"
  echo "  GPU nodes:       $(azd env get-value OMNIVEC_GPU_NODE_COUNT 2>/dev/null)"
  echo "  Blob source:     $(azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>/dev/null)"
  echo "  Metadata store:  $(azd env get-value OMNIVEC_METADATA_STORE 2>/dev/null)"
  echo ""
  printf "  ${YELLOW}Use existing config? [Y/n]: ${NC}"
  read -r reuse || true
  reuse=${reuse:-Y}
  case "$reuse" in
    [nN]*)
      printf "  ${GREEN}Reconfiguring...${NC}\n"
      ;;
    *)
      printf "  ${GREEN}Resuming with existing config.${NC}\n"
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

# ── Check for existing OmniVec installations ────────────────────────────────

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

# ── Metadata storage selection ──────────────────────────────────────────────

echo ""
printf "${YELLOW}Select metadata storage backend:${NC}\n"
echo "  1) Azure CosmosDB (Serverless NoSQL) — recommended"
echo "  2) Azure CosmosDB (Provisioned throughput)"
echo ""
printf "Choice [1]: "
read -r meta_choice || true
meta_choice=${meta_choice:-1}

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

echo ""
printf "${YELLOW}Will you use Azure Blob Storage as a document source?${NC}\n"
echo "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
echo "  will be created alongside the Storage Account."
echo ""
echo "  1) Yes — enable blob source ingestion (recommended)"
echo "  2) No  — CosmosDB sources only (skip Service Bus + Event Grid)"
echo ""
printf "Choice [1]: "
read -r blob_choice || true
blob_choice=${blob_choice:-1}

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

# Check VM SKU availability (parallel queries)
LOCATION="${AZURE_LOCATION:-centralus}"
printf "${YELLOW}Checking VM SKU availability in ${LOCATION}...${NC}\n"

# Run both queries in parallel
SYS_TMP=$(mktemp)
GPU_TMP=$(mktemp)

az vm list-skus --location "$LOCATION" --size Standard_D --resource-type virtualMachines \
  --query "[?(name=='Standard_D4s_v3' || name=='Standard_D4ds_v5' || name=='Standard_D8s_v3' || name=='Standard_D8ds_v5' || name=='Standard_D2s_v3' || name=='Standard_D2ds_v5') && (restrictions==null || restrictions[0]==null)].name" \
  -o tsv >"$SYS_TMP" 2>/dev/null &

az vm list-skus --location "$LOCATION" --size Standard_NC --resource-type virtualMachines \
  --query "[?(name=='Standard_NC6s_v3' || name=='Standard_NC12s_v3' || name=='Standard_NC4as_T4_v3' || name=='Standard_NC8as_T4_v3' || name=='Standard_NC24ads_A100_v4') && (restrictions==null || restrictions[0]==null)].name" \
  -o tsv >"$GPU_TMP" 2>/dev/null &

wait

SYS_SKUS=$(cat "$SYS_TMP" || true)
GPU_SKUS=$(cat "$GPU_TMP" || true)
rm -f "$SYS_TMP" "$GPU_TMP"

AVAILABLE_SKUS="${SYS_SKUS}
${GPU_SKUS}"

# Helper: check if a SKU is available
sku_available() {
  echo "$AVAILABLE_SKUS" | grep -qx "$1" 2>/dev/null
}

# System nodes — find available SKUs
printf "${CYAN}System node pool (API, controller, worker, changefeed):${NC}\n"
printf "  Available VM SKUs:\n"
SYS_OPTIONS=""
SYS_COUNT=0
for sku in Standard_D4s_v3 Standard_D4ds_v5 Standard_D8s_v3 Standard_D8ds_v5 Standard_D2s_v3 Standard_D2ds_v5; do
  if sku_available "$sku"; then
    SYS_COUNT=$((SYS_COUNT + 1))
    SYS_OPTIONS="${SYS_OPTIONS}${SYS_COUNT}:${sku}\n"
    printf "    ${SYS_COUNT}) ${sku}\n"
  fi
done

if [ "$SYS_COUNT" = "0" ]; then
  printf "  ${RED}No suitable system VM SKUs found in ${LOCATION}!${NC}\n"
  printf "  Enter a VM SKU manually: "
  read -r SYS_SKU || true
else
  echo ""
  printf "  System VM SKU [1]: "
  read -r sys_sku_choice || true
  sys_sku_choice=${sys_sku_choice:-1}
  SYS_SKU=$(printf "$SYS_OPTIONS" | grep "^${sys_sku_choice}:" | cut -d: -f2)
  if [ -z "$SYS_SKU" ]; then
    SYS_SKU=$(printf "$SYS_OPTIONS" | head -1 | cut -d: -f2)
  fi
fi
printf "  ${GREEN}System VM SKU: ${SYS_SKU}${NC}\n"

printf "  System node count [2]: "
read -r sys_count || true
sys_count=${sys_count:-2}
printf "  ${GREEN}System nodes: ${sys_count}${NC}\n"

echo ""

# GPU nodes — find available SKUs
printf "${CYAN}GPU node pool (ML models — dse-qwen2, clip, bge, bge-small):${NC}\n"
echo "  Enter 0 nodes to skip GPU pool (use external models only)."
GPU_OPTIONS=""
GPU_COUNT=0
for sku in Standard_NC6s_v3 Standard_NC12s_v3 Standard_NC4as_T4_v3 Standard_NC8as_T4_v3 Standard_NC24ads_A100_v4; do
  if sku_available "$sku"; then
    GPU_COUNT=$((GPU_COUNT + 1))
    GPU_OPTIONS="${GPU_OPTIONS}${GPU_COUNT}:${sku}\n"
    printf "    ${GPU_COUNT}) ${sku}\n"
  fi
done

if [ "$GPU_COUNT" = "0" ]; then
  printf "  ${YELLOW}No GPU VM SKUs available in ${LOCATION}. GPU pool will be skipped.${NC}\n"
  GPU_SKU="Standard_NC6s_v3"
  gpu_count="0"
else
  echo ""
  printf "  GPU VM SKU [1]: "
  read -r gpu_sku_choice || true
  gpu_sku_choice=${gpu_sku_choice:-1}
  GPU_SKU=$(printf "$GPU_OPTIONS" | grep "^${gpu_sku_choice}:" | cut -d: -f2)
  if [ -z "$GPU_SKU" ]; then
    GPU_SKU=$(printf "$GPU_OPTIONS" | head -1 | cut -d: -f2)
  fi

  printf "  GPU node count (0 = no GPU pool) [4]: "
  read -r gpu_count || true
  gpu_count=${gpu_count:-4}
fi

if [ "$gpu_count" = "0" ]; then
  printf "  ${YELLOW}GPU pool disabled — using external embedding models only.${NC}\n"
else
  printf "  ${GREEN}GPU VM: ${GPU_SKU}, nodes: ${gpu_count}${NC}\n"
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

printf "\n${GREEN}Pre-provision checks passed. Proceeding with Bicep deployment...${NC}\n"
printf "${CYAN}Environment: ${AZURE_ENV_NAME}${NC}\n"
printf "${CYAN}Each installation gets a unique resource token derived from (subscription + resource group + env name).${NC}\n"
