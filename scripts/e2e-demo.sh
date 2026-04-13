#!/bin/sh
# OmniVec End-to-End Demo — Fully Automated (Linux/macOS)
# Creates environment, provisions infra, registers model, creates pipeline, verifies it works
# Tests both queue mode (CFP -> jobs -> worker -> destination) and inline mode (CFP embeds directly into source)
#
# Usage:
#   ./scripts/e2e-demo.sh                    # Run all steps (1-11)
#   ./scripts/e2e-demo.sh --from-step 5      # Skip infra, start from test account creation
#   ./scripts/e2e-demo.sh --from-step 8      # Skip to pipeline + docs (assumes resources exist)
#   ./scripts/e2e-demo.sh --quiet            # Minimal output (pass/fail per step)

set -eu

# ─── Parse arguments ─────────────────────────────────────────────────────────
FROM_STEP=1
QUIET=false
AOAI_ENDPOINT="${AOAI_ENDPOINT:-}"
AOAI_KEY="${AOAI_KEY:-}"
AOAI_DEPLOYMENT="${AOAI_DEPLOYMENT:-text-embedding-3-small}"
AOAI_DIMS="${AOAI_DIMS:-1536}"
SHARED_REGISTRY_TOKEN="${OMNIVEC_SHARED_REGISTRY_TOKEN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --from-step) FROM_STEP="$2"; shift 2 ;;
    --quiet|-q) QUIET=true; shift ;;
    --endpoint) AOAI_ENDPOINT="$2"; shift 2 ;;
    --key) AOAI_KEY="$2"; shift 2 ;;
    --deployment) AOAI_DEPLOYMENT="$2"; shift 2 ;;
    --dims) AOAI_DIMS="$2"; shift 2 ;;
    --registry-token) SHARED_REGISTRY_TOKEN="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TOTAL_STEPS=11

# ─── Checkpoint: auto-resume from last successful step ──────────────────────
CHECKPOINT_FILE="${ROOT_DIR}/.e2e-checkpoint"

# Auto-detect FROM_STEP from checkpoint if user didn't explicitly set --from-step
if [ "$FROM_STEP" -eq 1 ] && [ -f "$CHECKPOINT_FILE" ]; then
  LAST_OK=$(cat "$CHECKPOINT_FILE" 2>/dev/null | tr -d '\r')
  if [ -n "$LAST_OK" ] && [ "$LAST_OK" -gt 0 ] 2>/dev/null; then
    RESUME_STEP=$((LAST_OK + 1))
    if [ "$RESUME_STEP" -le "$TOTAL_STEPS" ]; then
      printf "\033[1;33m  Previous run completed step %s/%s. Resuming from step %s.\033[0m\n" "$LAST_OK" "$TOTAL_STEPS" "$RESUME_STEP"
      printf "  (To start fresh, delete %s or pass --from-step 1)\n" "$CHECKPOINT_FILE"
      FROM_STEP=$RESUME_STEP
    fi
  fi
fi

save_checkpoint() {
  echo "$1" > "$CHECKPOINT_FILE"
}

# ─── Bootstrap: ensure everything is runnable from any directory ──────────────

# Add common tool install paths to PATH
export PATH="$HOME/.azure-kubectl:$HOME/.local/bin:$HOME/.azd/bin:$HOME/bin:$PATH"

# WSL: ensure KUBECONFIG points to a Linux-accessible path
if [ -z "${KUBECONFIG:-}" ] || [ ! -f "${KUBECONFIG:-}" ]; then
  if [ -f "$HOME/.kube/config" ]; then
    export KUBECONFIG="$HOME/.kube/config"
  fi
fi

# Ensure all scripts are executable (git clone may strip +x)
chmod +x "$ROOT_DIR"/hooks/*.sh "$ROOT_DIR"/scripts/*.sh 2>/dev/null || true

# Check and install required tools
check_install() {
  tool=$1; install_cmd=$2; url=$3
  if command -v "$tool" >/dev/null 2>&1; then return 0; fi
  printf "\033[1;33m  %s not found — installing...\033[0m\n" "$tool"
  eval "$install_cmd" 2>/dev/null
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf "\033[0;31m  Failed to install %s. Install manually: %s\033[0m\n" "$tool" "$url"
    exit 1
  fi
  printf "\033[0;32m  %s installed.\033[0m\n" "$tool"
}

echo "Checking prerequisites..."
check_install "az" "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash" "https://aka.ms/install-azure-cli"
check_install "azd" "curl -fsSL https://aka.ms/install-azd.sh | bash" "https://aka.ms/install-azd"
check_install "kubectl" \
  "mkdir -p \$HOME/.azure-kubectl && az aks install-cli --install-location \$HOME/.azure-kubectl/kubectl --kubelogin-install-location \$HOME/.azure-kubectl/kubelogin 2>/dev/null && chmod +x \$HOME/.azure-kubectl/kubectl \$HOME/.azure-kubectl/kubelogin 2>/dev/null" \
  "https://aka.ms/install-kubectl"
check_install "helm" \
  "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | HELM_INSTALL_DIR=\$HOME/.local/bin USE_SUDO=false bash 2>/dev/null" \
  "https://helm.sh/docs/intro/install/"
check_install "curl" "echo 'curl is required'" "https://curl.se"

# Verify Azure login
if ! az account show >/dev/null 2>&1; then
  printf "\033[0;31m  Not logged into Azure. Run 'az login' first.\033[0m\n"
  exit 1
fi

# Init submodules if needed (docgrok images require submodule content)
if [ ! -f "$ROOT_DIR/docgrok/Dockerfile" ]; then
  printf "\033[1;33m  Initializing git submodules...\033[0m\n"
  (cd "$ROOT_DIR" && git submodule update --init --recursive 2>/dev/null) || true
fi

# Detect CLI binary name (omnivec on Linux/macOS, omnivec.exe on Windows/WSL)
if [ -f "$ROOT_DIR/bin/omnivec" ]; then
  CLI="$ROOT_DIR/bin/omnivec"
elif [ -f "$ROOT_DIR/bin/omnivec.exe" ]; then
  CLI="$ROOT_DIR/bin/omnivec.exe"
else
  CLI="$ROOT_DIR/bin/omnivec"
fi

# ─── Colors & logging ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()      { if [ "$QUIET" = "false" ]; then echo "$1"; fi; }
log_step() { printf "${YELLOW}[Step %s/%s] %s${NC}\n" "$1" "$TOTAL_STEPS" "$2"; }
log_ok()   { printf "  ${GREEN}%s${NC}\n" "$1"; }
log_warn() { printf "  ${YELLOW}%s${NC}\n" "$1"; }
log_err()  { printf "  ${RED}%s${NC}\n" "$1"; }

# ─── Helper: safely capture azd/az output (strips \r, suppresses errors) ────
# On WSL the Windows az/azd CLIs output \r\n and may send errors to stdout.
# Pattern: run command, only emit stdout through tr on success, else empty.
azd_get() {
  val=$(azd env get-value "$1" 2>/dev/null) && printf '%s' "$val" | tr -d '\r' || true
}

az_query() {
  val=$(az "$@" 2>/dev/null) && printf '%s' "$val" | tr -d '\r' || true
}

# ─── Helper: run Python on API pod via stdin ─────────────────────────────────
pod_python() {
  echo "$1" | kubectl --context "$KUBE_CONTEXT" exec -i deployment/omnivec-api -n omnivec -- python3 -
}

# ─── Helper: HTTP calls via curl ─────────────────────────────────────────────
api_get() {
  curl -sfS -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" "$SERVER_URL$1"
}

api_post() {
  curl -sfS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$2" "$SERVER_URL$1"
}

api_delete() {
  curl -sfS -X DELETE -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" "$SERVER_URL$1" 2>/dev/null || true
}

# ─── Auto-download CLI if not present ────────────────────────────────────────
if [ ! -f "$CLI" ]; then
  mkdir -p "$ROOT_DIR/bin"
  downloaded=false

  # Try download first
  log_warn "CLI not found — downloading from GitHub release..."
  GH_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if [ -z "$GH_TOKEN" ] && command -v gh >/dev/null 2>&1; then
    GH_TOKEN=$(gh auth token 2>/dev/null || true)
  fi

  GH_AUTH=""
  if [ -n "$GH_TOKEN" ]; then GH_AUTH="-H \"Authorization: token $GH_TOKEN\""; fi

  # Detect platform
  OS=$(uname -s | tr '[:upper:]' '[:lower:]')
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
  esac
  CLI_ASSET="omnivec-${OS}-${ARCH}"

  RELEASE_URL="https://api.github.com/repos/AzureCosmosDB/OmniVec/releases/latest"
  ASSET_URL=$(eval curl -sfS -H "Accept: application/vnd.github.v3+json" $GH_AUTH "$RELEASE_URL" 2>/dev/null | \
    grep -o "\"browser_download_url\":\"[^\"]*${CLI_ASSET}[^\"]*\"" | head -1 | cut -d'"' -f4) || true

  if [ -n "$ASSET_URL" ]; then
    eval curl -sfSL $GH_AUTH -o "$CLI" "$ASSET_URL" 2>/dev/null && chmod +x "$CLI" && downloaded=true
  fi

  # Fallback: build from source
  if [ "$downloaded" = "false" ]; then
    log_warn "Download failed, building CLI from source..."
    GO_EXE=$(command -v go 2>/dev/null || true)
    if [ -n "$GO_EXE" ]; then
      (cd "$ROOT_DIR/cli" && go build -o "$CLI" .)
      log_ok "Built: $CLI"
    else
      log_err "Cannot obtain CLI. Install Go (https://go.dev/dl/) or place omnivec binary in bin/"
      exit 1
    fi
  else
    log_ok "Downloaded: $CLI"
  fi
fi

# ─── Banner ──────────────────────────────────────────────────────────────────
if [ "$QUIET" = "false" ]; then
  printf "\n${GREEN}╔══════════════════════════════════════════════════════╗${NC}\n"
  printf "${GREEN}║  OmniVec End-to-End Demo — Zero Manual Intervention  ║${NC}\n"
  printf "${GREEN}╚══════════════════════════════════════════════════════╝${NC}\n\n"
fi

# ─── Configuration ───────────────────────────────────────────────────────────
ENV_NAME="omnivec-e2e-demo"
LOCATION="eastus2"
SUBSCRIPTION="074d02eb-4d74-486a-b299-b262264d1536"

if [ -z "$AOAI_ENDPOINT" ]; then
  log_warn "Azure OpenAI endpoint not set."
  log "  Example: https://<resource>.openai.azure.com"
  printf "  Enter Azure OpenAI endpoint: "
  read -r AOAI_ENDPOINT
  if [ -z "$AOAI_ENDPOINT" ]; then log_err "Endpoint required."; exit 1; fi
fi
if [ -z "$AOAI_KEY" ]; then
  log_warn "Azure OpenAI API key not set."
  printf "  Enter Azure OpenAI API key: "
  read -r AOAI_KEY
  if [ -z "$AOAI_KEY" ]; then log_err "API key required."; exit 1; fi
fi
log_ok "Embedding: $AOAI_DEPLOYMENT (${AOAI_DIMS}d) @ $AOAI_ENDPOINT"

# ─── Helper: load azd env values ────────────────────────────────────────────
load_azd_values() {
  ADMIN_TOKEN=$(azd_get OMNIVEC_ADMIN_TOKEN)
  AKS_CLUSTER=$(azd_get AZURE_AKS_CLUSTER_NAME)
  RESOURCE_GROUP=$(azd_get AZURE_RESOURCE_GROUP)
  IDENTITY_CLIENT_ID=$(azd_get AZURE_IDENTITY_CLIENT_ID)
  COSMOS_ENDPOINT=$(azd_get AZURE_COSMOS_ENDPOINT)
  INSTANCE_TOKEN=$(echo "$AKS_CLUSTER" | tr -d '\r' | sed 's/omnivec-aks-//')
  TEST_COSMOS_ACCOUNT="omnivec-test-${INSTANCE_TOKEN}"
}

# ─── Helper: symlink kubeconfig from Windows home on WSL ─────────────────────
ensure_kubeconfig() {
  if [ -f "$HOME/.kube/config" ]; then return 0; fi
  # Try common Windows home path
  WIN_USER=$(cmd.exe /C "echo %USERNAME%" 2>/dev/null | tr -d '\r') || true
  if [ -n "$WIN_USER" ] && [ -f "/mnt/c/Users/$WIN_USER/.kube/config" ]; then
    mkdir -p "$HOME/.kube"
    ln -sf "/mnt/c/Users/$WIN_USER/.kube/config" "$HOME/.kube/config"
    export KUBECONFIG="$HOME/.kube/config"
    return 0
  fi
  # Fallback: try whoami
  if [ -f "/mnt/c/Users/$(whoami)/.kube/config" ] 2>/dev/null; then
    mkdir -p "$HOME/.kube"
    ln -sf "/mnt/c/Users/$(whoami)/.kube/config" "$HOME/.kube/config"
    export KUBECONFIG="$HOME/.kube/config"
  fi
}

# =============================================================================
# STEP 1: Create azd environment
# =============================================================================
if [ "$FROM_STEP" -le 1 ]; then
  log_step 1 "Creating azd environment: $ENV_NAME"
  if ! azd env new "$ENV_NAME" --location "$LOCATION" --subscription "$SUBSCRIPTION" 2>/dev/null; then
    if azd env select "$ENV_NAME" >/dev/null 2>&1; then
      log_warn "Environment already exists. Reusing: $ENV_NAME"
    else
      log_err "Failed to create/select environment: $ENV_NAME"
      exit 1
    fi
  fi
  azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
  azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
  azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_D4ds_v6"
  azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
  azd env set OMNIVEC_GPU_NODE_VM_SIZE "Standard_NC6s_v3"
  azd env set OMNIVEC_GPU_NODE_COUNT 0
  azd env set OMNIVEC_BUILD_MODE "acr"
  if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
    azd env set OMNIVEC_SHARED_REGISTRY_TOKEN "$SHARED_REGISTRY_TOKEN"
  else
    log_warn "Shared registry token not set (needed for image import)."
    printf "  Enter shared registry token (omnivecregistry.azurecr.io): "
    read -r SHARED_REGISTRY_TOKEN
    if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
      azd env set OMNIVEC_SHARED_REGISTRY_TOKEN "$SHARED_REGISTRY_TOKEN"
    fi
  fi
  log_ok "Environment configured."
  save_checkpoint 1
fi

# =============================================================================
# STEP 2: Provision infrastructure
# =============================================================================
if [ "$FROM_STEP" -le 2 ]; then
  log_step 2 "Provisioning infrastructure (azd up ~15 min)..."
  if ! azd up --no-prompt; then
    log_err "azd up failed. Resolve the deployment error and re-run."
    exit 1
  fi
  save_checkpoint 2
fi

# =============================================================================
# STEP 3: Get connection details + wait for API
# =============================================================================
if [ "$FROM_STEP" -le 3 ]; then
  log_step 3 "Retrieving connection details..."
fi
# Always load azd values (needed by all subsequent steps)
load_azd_values
if [ -z "${AKS_CLUSTER:-}" ] || [ -z "${RESOURCE_GROUP:-}" ] || [ -z "${ADMIN_TOKEN:-}" ]; then
  log_err "Missing required azd outputs (AKS cluster/resource group/admin token)."
  exit 1
fi
KUBE_CONTEXT="${AKS_CLUSTER}"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --context "$KUBE_CONTEXT" --overwrite-existing >/dev/null
ensure_kubeconfig

log "  Admin Token: $ADMIN_TOKEN"
log "  AKS:         $AKS_CLUSTER"

# Wait for external IP
log "  Waiting for external IP..."
SERVER=""
i=0
while [ $i -lt 60 ]; do
  SERVER=$(kubectl --context "$KUBE_CONTEXT" get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null | tr -d '\r' || true)
  if [ -n "$SERVER" ]; then break; fi
  sleep 5
  i=$((i + 1))
done
if [ -z "$SERVER" ]; then log_err "Failed to get external IP"; exit 1; fi
SERVER_URL="http://$SERVER"
log_ok "Server: $SERVER_URL"

# Wait for API
log "  Waiting for API health..."
i=0
api_healthy=false
while [ $i -lt 30 ]; do
  health=$(curl -sf --max-time 5 "$SERVER_URL/health" 2>/dev/null || true)
  if echo "$health" | grep -q '"healthy"'; then
    api_healthy=true
    break
  fi
  sleep 5
  i=$((i + 1))
done
if [ "$api_healthy" != "true" ]; then
  log_err "API did not become healthy in time."
  exit 1
fi
log_ok "API healthy."
save_checkpoint 3

# =============================================================================
# STEP 4: Configure CLI
# =============================================================================
if [ "$FROM_STEP" -le 4 ]; then
  log_step 4 "Configuring CLI..."
  "$CLI" config set server "$SERVER_URL"
  "$CLI" config set token "$ADMIN_TOKEN"
  "$CLI" status
  save_checkpoint 4
fi

# =============================================================================
# STEP 5: Create test CosmosDB account + containers
# =============================================================================
if [ "$FROM_STEP" -le 5 ]; then
  log_step 5 "Creating test CosmosDB account..."
  TEST_COSMOS_ENDPOINT=$(az_query cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv)

  if [ -z "$TEST_COSMOS_ENDPOINT" ]; then
    log "  Creating account: $TEST_COSMOS_ACCOUNT"
    ARM_FILE=$(mktemp)
    cat > "$ARM_FILE" <<ARMEOF
{
  "location": "$LOCATION",
  "kind": "GlobalDocumentDB",
  "properties": {
    "databaseAccountOfferType": "Standard",
    "disableLocalAuth": true,
    "enableAutomaticFailover": false,
    "consistencyPolicy": { "defaultConsistencyLevel": "Session" },
    "locations": [{ "locationName": "$LOCATION", "failoverPriority": 0, "isZoneRedundant": false }],
    "capabilities": [{ "name": "EnableServerless" }, { "name": "EnableNoSQLVectorSearch" }]
  }
}
ARMEOF
    ARM_URL="https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.DocumentDB/databaseAccounts/${TEST_COSMOS_ACCOUNT}?api-version=2024-05-15"
    az rest --method PUT --url "$ARM_URL" --body "@$ARM_FILE" -o none
    rm -f "$ARM_FILE"

    log "  Waiting for provisioning..."
    i=0
    while [ $i -lt 40 ]; do
      st=$(az_query cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query provisioningState -o tsv)
      if [ "$st" = "Succeeded" ]; then break; fi
      sleep 10
      i=$((i + 1))
    done
    TEST_COSMOS_ENDPOINT=$(az_query cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv)
  fi
  log_ok "Endpoint: $TEST_COSMOS_ENDPOINT"

  # Grant RBAC
  log "  Granting RBAC..."
  PRINCIPAL_ID=$(az_query identity show --name "omnivec-identity-$INSTANCE_TOKEN" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)
  if [ -z "$PRINCIPAL_ID" ]; then
    PRINCIPAL_ID=$(az_query identity list --resource-group "$RESOURCE_GROUP" --query "[0].principalId" -o tsv)
  fi

  # MSYS_NO_PATHCONV=1 prevents Git Bash from mangling "/" into "C:/Program Files/Git/"
  MSYS_NO_PATHCONV=1 az cosmosdb sql role assignment create --account-name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --role-definition-id "00000000-0000-0000-0000-000000000002" --principal-id "$PRINCIPAL_ID" --scope "/" -o none 2>/dev/null || true
  # ARM role: Cosmos DB Account Reader (required for readMetadata)
  # Use az rest because az role assignment create has API version bugs in some az CLI versions
  ROLE_ASSIGN_ID=$(powershell -Command "[guid]::NewGuid().ToString()" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())")
  SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.DocumentDB/databaseAccounts/$TEST_COSMOS_ACCOUNT"
  az rest --method PUT \
    --url "${SCOPE}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGN_ID}?api-version=2022-04-01" \
    --body "{\"properties\":{\"roleDefinitionId\":\"/subscriptions/$SUBSCRIPTION/providers/Microsoft.Authorization/roleDefinitions/fbdf93bf-df7d-467e-a4d2-9458aa1360c8\",\"principalId\":\"$PRINCIPAL_ID\",\"principalType\":\"ServicePrincipal\"}}" \
    -o none 2>/dev/null || true
  log_ok "RBAC assigned (Data Contributor + Account Reader). Waiting 120s for propagation..."
  sleep 120

  # Create database + containers
  # MSYS_NO_PATHCONV=1 prevents Git Bash from mangling "/id" into "C:/Program Files/Git/id"
  log "  Creating containers..."
  az cosmosdb sql database create --account-name "$TEST_COSMOS_ACCOUNT" --name testdb --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null || true
  MSYS_NO_PATHCONV=1 az cosmosdb sql container create --account-name "$TEST_COSMOS_ACCOUNT" --database-name testdb --name test-documents \
    --resource-group "$RESOURCE_GROUP" --partition-key-path "/id" -o none 2>/dev/null || true
  log_ok "test-documents created."

  # Vectors container with vector policy (via API pod)
  # Retry up to 5 times — RBAC propagation or transient connectivity can delay
  vectors_ok=false
  for attempt in 1 2 3 4 5; do
    vectors_output=$(pod_python "
import os, time
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosHttpResponseError
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred, connection_timeout=30)
db = client.get_database_client('testdb')
vp = {'vectorEmbeddings': [{'path': '/embedding', 'dataType': 'float32', 'distanceFunction': 'cosine', 'dimensions': $AOAI_DIMS}]}
ip = {'includedPaths': [{'path': '/*'}], 'excludedPaths': [{'path': '/embedding/*'}], 'vectorIndexes': [{'path': '/embedding', 'type': 'quantizedFlat'}]}
try:
    db.create_container(id='vectors', partition_key={'paths': ['/id'], 'kind': 'Hash'}, vector_embedding_policy=vp, indexing_policy=ip)
    print('OK: vectors created (${AOAI_DIMS}d, cosine, quantizedFlat)')
except CosmosResourceExistsError:
    print('OK: vectors container already exists')
except CosmosHttpResponseError as e:
    if 'Forbidden' in str(e) or '403' in str(e):
        print('RBAC_WAIT')
    else:
        raise
except Exception as e:
    if 'timed out' in str(e).lower() or 'timeout' in str(e).lower():
        print('RETRY_TIMEOUT')
    else:
        raise
" 2>&1) && true
    echo "$vectors_output"
    if echo "$vectors_output" | grep -q "^OK:"; then
      vectors_ok=true
      break
    elif echo "$vectors_output" | grep -q "RBAC_WAIT"; then
      log_warn "RBAC not yet propagated, waiting 30s (attempt $attempt/5)..."
      sleep 30
    elif echo "$vectors_output" | grep -q "RETRY_TIMEOUT\|timed out\|Timeout"; then
      log_warn "Connection timed out, retrying in 30s (attempt $attempt/5)..."
      sleep 30
    else
      break
    fi
  done
  if [ "$vectors_ok" != "true" ]; then
    log_err "Failed to create vectors container after retries"
    exit 1
  fi
  log_ok "All containers ready."
  save_checkpoint 5
else
  # Load test endpoint for later steps
  TEST_COSMOS_ENDPOINT=$(az_query cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv)
fi

# =============================================================================
# STEP 6: Register embedding model
# =============================================================================
if [ "$FROM_STEP" -le 6 ]; then
  log_step 6 "Registering Azure OpenAI embedding model..."
  MODEL_BODY=$(cat <<MEOF
{"name":"azure-openai-embed","type":"azure-openai","endpoint":"$AOAI_ENDPOINT","api_key":"$AOAI_KEY","model":"$AOAI_DEPLOYMENT","deployment":"$AOAI_DEPLOYMENT","dimensions":$AOAI_DIMS,"api_version":"2024-06-01"}
MEOF
  )
  MODEL_RESULT=$(api_post "/api/models" "$MODEL_BODY")
  MODEL_ID=$(echo "$MODEL_RESULT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Model: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)"
  save_checkpoint 6
else
  MODELS_RESULT=$(api_get "/api/models")
  MODEL_ID=$(echo "$MODELS_RESULT" | grep -o '"id":"mdl-[^"]*"' | head -1 | cut -d'"' -f4)
fi

# =============================================================================
# STEP 7: Create source + destination
# =============================================================================
if [ "$FROM_STEP" -le 7 ]; then
  log_step 7 "Creating source and destination..."

  # Clean up any existing resources from previous runs
  for pip_id in $(api_get "/api/pipelines" | grep -o '"id":"pip-[^"]*"' | cut -d'"' -f4); do
    api_delete "/api/pipelines/$pip_id" >/dev/null
  done
  for src_id in $(api_get "/api/sources" | grep -o '"id":"src-[^"]*"' | cut -d'"' -f4); do
    api_delete "/api/sources/$src_id" >/dev/null
  done
  for dst_id in $(api_get "/api/destinations" | grep -o '"id":"dst-[^"]*"' | cut -d'"' -f4); do
    api_delete "/api/destinations/$dst_id" >/dev/null
  done

  SRC_BODY=$(cat <<SEOF
{"name":"demo-cosmosdb-source","type":"cosmosdb","config":{"endpoint":"$TEST_COSMOS_ENDPOINT","database":"testdb","container":"test-documents","auth_type":"managed-identity","client_id":"$IDENTITY_CLIENT_ID"}}
SEOF
  )
  SRC_RESULT=$(api_post "/api/sources" "$SRC_BODY")
  SOURCE_ID=$(echo "$SRC_RESULT" | grep -o '"id":"src-[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Source: $SOURCE_ID"

  DST_BODY=$(cat <<DEOF
{"name":"demo-vector-store","type":"cosmosdb-vector","config":{"endpoint":"$TEST_COSMOS_ENDPOINT","database":"testdb","container":"vectors","auth_type":"managed-identity","client_id":"$IDENTITY_CLIENT_ID","vector_dimensions":$AOAI_DIMS}}
DEOF
  )
  DST_RESULT=$(api_post "/api/destinations" "$DST_BODY")
  DEST_ID=$(echo "$DST_RESULT" | grep -o '"id":"dst-[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Destination: $DEST_ID"
  save_checkpoint 7
else
  SRCS_RESULT=$(api_get "/api/sources")
  SOURCE_ID=$(echo "$SRCS_RESULT" | grep -o '"id":"src-[^"]*"' | head -1 | cut -d'"' -f4)
  DSTS_RESULT=$(api_get "/api/destinations")
  DEST_ID=$(echo "$DSTS_RESULT" | grep -o '"id":"dst-[^"]*"' | head -1 | cut -d'"' -f4)
fi

# =============================================================================
# STEP 8: Create pipeline (queue mode), insert docs, activate
# =============================================================================
PIP_ID=""
if [ "$FROM_STEP" -le 8 ]; then
  log_step 8 "Creating pipeline (queue mode), inserting docs, activating..."

  PIP_BODY=$(cat <<PEOF
{"name":"demo-pipeline","sources":[{"source_id":"$SOURCE_ID","filters":{},"content_fields":["content"]}],"destination_id":"$DEST_ID","docgrok_pipeline":"$MODEL_ID","process_existing":true,"processing_mode":"queue"}
PEOF
  )
  PIP_RESULT=$(api_post "/api/pipelines" "$PIP_BODY")
  PIP_ID=$(echo "$PIP_RESULT" | grep -o '"id":"pip-[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Pipeline created (queue mode): $PIP_ID"

  # Insert test documents (retry up to 3 times for transient connectivity)
  log "  Inserting test documents..."
  docs_ok=false
  for doc_attempt in 1 2 3; do
    docs_output=$(pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred, connection_timeout=30)
c = client.get_database_client('testdb').get_container_client('test-documents')
docs = [
    {'id': 'doc-001', 'title': 'Azure Cosmos DB', 'content': 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.', 'category': 'database'},
    {'id': 'doc-002', 'title': 'Azure Kubernetes Service', 'content': 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.', 'category': 'compute'},
    {'id': 'doc-003', 'title': 'Azure Blob Storage', 'content': 'Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.', 'category': 'storage'},
]
for doc in docs:
    c.upsert_item(doc)
    print(f'  Inserted: {doc[\"id\"]} - {doc[\"title\"]}')
print('DOCS_OK')
" 2>&1) && true
    echo "$docs_output"
    if echo "$docs_output" | grep -q "DOCS_OK"; then
      docs_ok=true
      break
    fi
    log_warn "Doc insert failed, retrying in 30s (attempt $doc_attempt/3)..."
    sleep 30
  done
  if [ "$docs_ok" != "true" ]; then
    log_err "Failed to insert test documents after retries"
    exit 1
  fi

  # Resume pipeline
  log "  Resuming pipeline..."
  api_post "/api/pipelines/$PIP_ID/resume" "{}" >/dev/null
  api_post "/api/pipelines/$PIP_ID/run" "{}" >/dev/null
  log_ok "Pipeline activated (queue mode). Waiting for processing..."
  queue_embedded=false
  i=0
  while [ $i -lt 12 ]; do
    POLL=$(api_get "/api/pipelines/$PIP_ID" 2>/dev/null || true)
    POLL_EMB=$(echo "$POLL" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
    if [ -n "$POLL_EMB" ] && [ "$POLL_EMB" -gt 0 ] 2>/dev/null; then
      queue_embedded=true
      break
    fi
    sleep 10
    i=$((i + 1))
  done
  if [ "$queue_embedded" != "true" ]; then
    log_err "Queue mode did not produce embeddings within timeout."
    exit 1
  fi
  save_checkpoint 8
else
  PIPS_RESULT=$(api_get "/api/pipelines")
  PIP_ID=$(echo "$PIPS_RESULT" | grep -o '"id":"pip-[^"]*"' | head -1 | cut -d'"' -f4)
fi

# =============================================================================
# STEP 9: Verify queue mode results
# =============================================================================
if [ -n "$PIP_ID" ]; then
  log_step 9 "Verifying queue mode results..."
  if [ "$QUIET" = "false" ]; then
    "$CLI" pipeline show "$PIP_ID" || true
  fi

  STATS_RESULT=$(api_get "/api/pipelines/$PIP_ID")
  EMBEDDED=$(echo "$STATS_RESULT" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
  COMPLETION=$(echo "$STATS_RESULT" | grep -o '"completion_pct":[0-9.]*' | cut -d: -f2)
  log "  Embedded:   $EMBEDDED"
  log "  Completion: ${COMPLETION}%"

  if [ -n "$EMBEDDED" ] && [ "$EMBEDDED" -gt 0 ] 2>/dev/null; then
    log_ok "Queue mode: $EMBEDDED documents embedded to destination!"
  else
    log_err "Queue mode verification failed: embedded_count is 0."
    exit 1
  fi
  save_checkpoint 9
else
  log_err "No pipeline found for queue-mode verification."
  exit 1
fi

# =============================================================================
# STEP 10: Switch to inline mode, reset, reprocess same docs
# =============================================================================
if [ "$FROM_STEP" -le 10 ] && [ -n "$PIP_ID" ]; then
  log_step 10 "Switching pipeline to inline mode, resetting..."

  # Pause pipeline before switching mode
  api_post "/api/pipelines/$PIP_ID/pause" "{}" >/dev/null 2>&1 || true

  # Switch processing mode to inline
  api_post "/api/pipelines/$PIP_ID/processing-mode/inline" "{}" >/dev/null
  log_ok "Switched to inline mode"

  # Reset pipeline — forces CFP to reprocess all docs from the beginning
  api_post "/api/pipelines/$PIP_ID/reset" "{}" >/dev/null
  log_ok "Pipeline reset — will reprocess all docs in inline mode"

  # Resume pipeline
  api_post "/api/pipelines/$PIP_ID/resume" "{}" >/dev/null
  api_post "/api/pipelines/$PIP_ID/run" "{}" >/dev/null
  log_ok "Pipeline resumed (inline mode). Waiting for reprocessing..."

  # Poll source container until embeddings appear or 120s timeout
  inline_ready=false
  i=0
  while [ $i -lt 12 ]; do
    poll_check=$(pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred, connection_timeout=30)
c = client.get_database_client('testdb').get_container_client('test-documents')
count = sum(1 for d in c.query_items('SELECT c.id FROM c WHERE IS_DEFINED(c.embedding)', enable_cross_partition_query=True))
print(count)
" 2>/dev/null || echo "0")
    if echo "$poll_check" | grep -q "3"; then
      inline_ready=true
      break
    fi
    sleep 10
    i=$((i + 1))
  done
  if [ "$inline_ready" != "true" ]; then
    log_err "Inline mode did not reprocess all documents within timeout."
    exit 1
  fi
  save_checkpoint 10
elif [ "$FROM_STEP" -le 10 ]; then
  log_err "No pipeline found for inline-mode reset."
  exit 1
fi

# =============================================================================
# STEP 11: Verify inline mode results
# =============================================================================
log_step 11 "Verifying inline mode results..."
if [ -z "$PIP_ID" ]; then
  log_err "No pipeline found for inline-mode verification."
  exit 1
fi
if [ "$QUIET" = "false" ] && [ -n "$PIP_ID" ]; then
  "$CLI" pipeline show "$PIP_ID" || true
fi

# Inline mode embeds directly into the source container — check for embedding field
log "  Checking source container for inline embeddings..."
INLINE_CHECK=""
INLINE_CHECK=$(pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred, connection_timeout=30)
c = client.get_database_client('testdb').get_container_client('test-documents')
embedded = 0
checked = 0
for doc in c.query_items('SELECT c.id, IS_DEFINED(c.embedding) as has_emb FROM c', enable_cross_partition_query=True):
    checked += 1
    if doc.get('has_emb'):
        embedded += 1
        print(f'  {doc[\"id\"]}: embedding present')
    else:
        print(f'  {doc[\"id\"]}: no embedding yet')
print(f'INLINE_RESULT:{embedded}/{checked}')
" 2>/dev/null || true)
echo "$INLINE_CHECK"

# For inline mode, the source container is the source of truth
INLINE_EMBEDDED=0
INLINE_TOTAL=0
if echo "$INLINE_CHECK" | grep -q "INLINE_RESULT:"; then
  INLINE_EMBEDDED=$(echo "$INLINE_CHECK" | grep -o 'INLINE_RESULT:[0-9]*/[0-9]*' | cut -d: -f2 | cut -d/ -f1)
  INLINE_TOTAL=$(echo "$INLINE_CHECK" | grep -o 'INLINE_RESULT:[0-9]*/[0-9]*' | cut -d: -f2 | cut -d/ -f2)
fi

INLINE_STATS=$(api_get "/api/pipelines/$PIP_ID")
INLINE_PROCESSED=$(echo "$INLINE_STATS" | grep -o '"documents_processed":[0-9]*' | cut -d: -f2)
INLINE_STATS_EMBEDDED=$(echo "$INLINE_STATS" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
log "  Pipeline stats — Processed: $INLINE_PROCESSED, Embedded: $INLINE_STATS_EMBEDDED"
log "  Source container — Embedded: $INLINE_EMBEDDED/$INLINE_TOTAL"

if [ "$INLINE_EMBEDDED" -gt 0 ] 2>/dev/null; then
  log_ok "Inline mode working — $INLINE_EMBEDDED/$INLINE_TOTAL documents embedded directly into source container!"
else
  log_err "Inline mode verification failed: no embeddings detected in source container."
  exit 1
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
printf "${GREEN}╔══════════════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║           End-to-End Demo Complete!                  ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════════════════╝${NC}\n"
echo ""
printf "  Server:          ${CYAN}${SERVER_URL}${NC}\n"
printf "  Admin Token:     ${CYAN}${ADMIN_TOKEN}${NC}\n"
printf "  Source:          ${CYAN}${SOURCE_ID}${NC}\n"
printf "  Destination:     ${CYAN}${DEST_ID}${NC}\n"
printf "  Pipeline:        ${CYAN}${PIP_ID}${NC}\n"
printf "  Model:           ${CYAN}${MODEL_ID} (${AOAI_DEPLOYMENT})${NC}\n"
echo ""
printf "  Tested both modes on the same pipeline and same documents:\n"
printf "  ${CYAN}Queue mode:${NC}  CFP -> Service Bus -> .NET worker -> destination container\n"
printf "  ${CYAN}Inline mode:${NC} CFP -> embed directly -> patch back to source container\n"
echo ""

# Clean up checkpoint on successful completion
rm -f "$CHECKPOINT_FILE"
save_checkpoint 11
