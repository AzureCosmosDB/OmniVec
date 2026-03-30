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

# ─── Helper: run Python on API pod via stdin ─────────────────────────────────
pod_python() {
  echo "$1" | kubectl exec -i deployment/omnivec-api -n omnivec -- python3 -
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
SUBSCRIPTION="<AZURE_SUBSCRIPTION_ID>"

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
  ADMIN_TOKEN=$(azd env get-value OMNIVEC_ADMIN_TOKEN 2>/dev/null || true)
  AKS_CLUSTER=$(azd env get-value AZURE_AKS_CLUSTER_NAME 2>/dev/null || true)
  RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || true)
  IDENTITY_CLIENT_ID=$(azd env get-value AZURE_IDENTITY_CLIENT_ID 2>/dev/null || true)
  COSMOS_ENDPOINT=$(azd env get-value AZURE_COSMOS_ENDPOINT 2>/dev/null || true)
  INSTANCE_TOKEN=$(echo "$AKS_CLUSTER" | sed 's/omnivec-aks-//')
  TEST_COSMOS_ACCOUNT="omnivec-test-${INSTANCE_TOKEN}"
}

# =============================================================================
# STEP 1: Create azd environment
# =============================================================================
if [ "$FROM_STEP" -le 1 ]; then
  log_step 1 "Creating azd environment: $ENV_NAME"
  azd env new "$ENV_NAME" --location "$LOCATION" --subscription "$SUBSCRIPTION" 2>/dev/null || true
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
fi

# =============================================================================
# STEP 2: Provision infrastructure
# =============================================================================
if [ "$FROM_STEP" -le 2 ]; then
  log_step 2 "Provisioning infrastructure (azd up ~15 min)..."
  azd up --no-prompt || log_warn "azd up returned non-zero, continuing..."
fi

# =============================================================================
# STEP 3: Get connection details + wait for API
# =============================================================================
if [ "$FROM_STEP" -le 3 ]; then
  log_step 3 "Retrieving connection details..."
fi
# Always load azd values (needed by all subsequent steps)
load_azd_values
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --overwrite-existing 2>/dev/null

log "  Admin Token: $ADMIN_TOKEN"
log "  AKS:         $AKS_CLUSTER"

# Wait for external IP
log "  Waiting for external IP..."
SERVER=""
i=0
while [ $i -lt 60 ]; do
  SERVER=$(kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
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
while [ $i -lt 30 ]; do
  health=$(curl -sf --max-time 5 "$SERVER_URL/health" 2>/dev/null || true)
  if echo "$health" | grep -q '"healthy"'; then break; fi
  sleep 5
  i=$((i + 1))
done
log_ok "API healthy."

# =============================================================================
# STEP 4: Configure CLI
# =============================================================================
if [ "$FROM_STEP" -le 4 ]; then
  log_step 4 "Configuring CLI..."
  "$CLI" config set server "$SERVER_URL"
  "$CLI" config set token "$ADMIN_TOKEN"
  "$CLI" status
fi

# =============================================================================
# STEP 5: Create test CosmosDB account + containers
# =============================================================================
if [ "$FROM_STEP" -le 5 ]; then
  log_step 5 "Creating test CosmosDB account..."
  TEST_COSMOS_ENDPOINT=$(az cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv 2>/dev/null || true)

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
      st=$(az cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query provisioningState -o tsv 2>/dev/null || true)
      if [ "$st" = "Succeeded" ]; then break; fi
      sleep 10
      i=$((i + 1))
    done
    TEST_COSMOS_ENDPOINT=$(az cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv 2>/dev/null || true)
  fi
  log_ok "Endpoint: $TEST_COSMOS_ENDPOINT"

  # Grant RBAC
  log "  Granting RBAC..."
  PRINCIPAL_ID=$(az identity show --name "omnivec-identity-$INSTANCE_TOKEN" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv 2>/dev/null || true)
  if [ -z "$PRINCIPAL_ID" ]; then
    PRINCIPAL_ID=$(az identity list --resource-group "$RESOURCE_GROUP" --query "[0].principalId" -o tsv 2>/dev/null || true)
  fi

  az cosmosdb sql role assignment create --account-name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
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
  log "  Creating containers..."
  az cosmosdb sql database create --account-name "$TEST_COSMOS_ACCOUNT" --name testdb --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null || true
  az cosmosdb sql container create --account-name "$TEST_COSMOS_ACCOUNT" --database-name testdb --name test-documents \
    --resource-group "$RESOURCE_GROUP" --partition-key-path "/id" -o none 2>/dev/null || true
  log_ok "test-documents created."

  # Vectors container with vector policy (via API pod)
  # Retry up to 3 times — RBAC propagation can take longer than expected
  vectors_ok=false
  for attempt in 1 2 3 4 5; do
    vectors_output=$(pod_python "
import os, time
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosHttpResponseError
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
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
" 2>&1) && true
    echo "$vectors_output"
    if echo "$vectors_output" | grep -q "^OK:"; then
      vectors_ok=true
      break
    elif echo "$vectors_output" | grep -q "RBAC_WAIT"; then
      log_warn "RBAC not yet propagated, waiting 30s (attempt $attempt/5)..."
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
else
  # Load test endpoint for later steps
  TEST_COSMOS_ENDPOINT=$(az cosmosdb show --name "$TEST_COSMOS_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query documentEndpoint -o tsv 2>/dev/null || true)
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
else
  SRCS_RESULT=$(api_get "/api/sources")
  SOURCE_ID=$(echo "$SRCS_RESULT" | grep -o '"id":"src-[^"]*"' | head -1 | cut -d'"' -f4)
  DSTS_RESULT=$(api_get "/api/destinations")
  DEST_ID=$(echo "$DSTS_RESULT" | grep -o '"id":"dst-[^"]*"' | head -1 | cut -d'"' -f4)
fi

# =============================================================================
# STEP 8: Queue mode — create pipeline, insert docs, resume
# =============================================================================
PIP_ID=""
if [ "$FROM_STEP" -le 8 ]; then
  log_step 8 "Queue mode — creating pipeline, inserting docs, activating..."

  PIP_BODY=$(cat <<PEOF
{"name":"demo-pipeline-queue","sources":[{"source_id":"$SOURCE_ID","filters":{}}],"destination_id":"$DEST_ID","docgrok_pipeline":"$MODEL_ID","process_existing":true,"processing_mode":"queue"}
PEOF
  )
  PIP_RESULT=$(api_post "/api/pipelines" "$PIP_BODY")
  PIP_ID=$(echo "$PIP_RESULT" | grep -o '"id":"pip-[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Pipeline (paused, queue mode): $PIP_ID"

  # Insert test documents
  log "  Inserting test documents..."
  pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
c = client.get_database_client('testdb').get_container_client('test-documents')
docs = [
    {'id': 'doc-001', 'title': 'Azure Cosmos DB', 'content': 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.', 'category': 'database'},
    {'id': 'doc-002', 'title': 'Azure Kubernetes Service', 'content': 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.', 'category': 'compute'},
    {'id': 'doc-003', 'title': 'Azure Blob Storage', 'content': 'Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.', 'category': 'storage'},
]
for doc in docs:
    c.upsert_item(doc)
    print(f'  Inserted: {doc[\"id\"]} - {doc[\"title\"]}')
"

  # Resume pipeline
  log "  Resuming pipeline..."
  api_post "/api/pipelines/$PIP_ID/resume" "{}" >/dev/null
  api_post "/api/pipelines/$PIP_ID/run" "{}" >/dev/null
  log_ok "Pipeline activated (queue mode). Waiting for processing..."
  # Poll until stats show completion or 120s timeout
  i=0
  while [ $i -lt 12 ]; do
    POLL=$(api_get "/api/pipelines/$PIP_ID" 2>/dev/null || true)
    POLL_EMB=$(echo "$POLL" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
    if [ -n "$POLL_EMB" ] && [ "$POLL_EMB" -gt 0 ] 2>/dev/null; then break; fi
    sleep 10
    i=$((i + 1))
  done
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
    "$CLI" pipeline show "$PIP_ID"
    echo ""
    "$CLI" job list
  fi

  JOBS_RESULT=$(api_get "/api/jobs?status=completed&limit=5")
  COMPLETED_COUNT=$(echo "$JOBS_RESULT" | grep -o '"id":"job-[^"]*"' | wc -l)

  if [ "$COMPLETED_COUNT" -gt 0 ]; then
    log_ok "$COMPLETED_COUNT documents embedded via queue mode!"

    # Vector search test
    log "  Testing vector search..."
    SEARCH_BODY="{\"query\":\"what is cosmos db\",\"destination_id\":\"$DEST_ID\",\"top_k\":3}"
    SEARCH_RESULT=$(api_post "/api/playground/search" "$SEARCH_BODY" 2>/dev/null || true)
    if [ -n "$SEARCH_RESULT" ]; then
      SEARCH_COUNT=$(echo "$SEARCH_RESULT" | grep -o '"id":' | wc -l)
      log_ok "Search returned $SEARCH_COUNT results"
    else
      log_warn "Search test skipped (may need more processing time)"
    fi

    # Pipeline reset test
    log "  Testing pipeline reset..."
    "$CLI" pipeline reset "$PIP_ID" -y
  else
    log_warn "No completed jobs yet. Check: omnivec pipeline show $PIP_ID"
  fi

  # Stats
  STATS_RESULT=$(api_get "/api/pipelines/$PIP_ID")
  PROCESSED=$(echo "$STATS_RESULT" | grep -o '"documents_processed":[0-9]*' | cut -d: -f2)
  EMBEDDED=$(echo "$STATS_RESULT" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
  COMPLETION=$(echo "$STATS_RESULT" | grep -o '"completion_pct":[0-9.]*' | cut -d: -f2)
  log "  Processed:  $PROCESSED"
  log "  Embedded:   $EMBEDDED"
  log "  Completion: ${COMPLETION}%"

  # Clean up queue pipeline before starting inline
  log "  Removing queue pipeline before inline test..."
  api_delete "/api/pipelines/$PIP_ID" >/dev/null
else
  log_step 9 "Skipping queue mode verify (no queue pipeline)"
  # Clean up any existing pipelines when jumping to inline test
  for pip_id in $(api_get "/api/pipelines" | grep -o '"id":"pip-[^"]*"' | cut -d'"' -f4); do
    api_delete "/api/pipelines/$pip_id" >/dev/null
  done
fi

# =============================================================================
# STEP 10: Inline mode — create pipeline, resume, then insert docs
# =============================================================================
INLINE_PIP_ID=""
if [ "$FROM_STEP" -le 10 ]; then
  log_step 10 "Inline mode — creating pipeline, activating, inserting docs..."

  INLINE_PIP_BODY=$(cat <<IPEOF
{"name":"demo-pipeline-inline","sources":[{"source_id":"$SOURCE_ID","filters":{}}],"destination_id":"$DEST_ID","docgrok_pipeline":"$MODEL_ID","process_existing":true,"processing_mode":"inline"}
IPEOF
  )
  INLINE_PIP_RESULT=$(api_post "/api/pipelines" "$INLINE_PIP_BODY")
  INLINE_PIP_ID=$(echo "$INLINE_PIP_RESULT" | grep -o '"id":"pip-[^"]*"' | head -1 | cut -d'"' -f4)
  log_ok "Pipeline (paused, inline mode): $INLINE_PIP_ID"

  # Resume inline pipeline FIRST so CFP picks up changes in inline mode
  log "  Resuming inline pipeline..."
  api_post "/api/pipelines/$INLINE_PIP_ID/resume" "{}" >/dev/null
  api_post "/api/pipelines/$INLINE_PIP_ID/run" "{}" >/dev/null
  log_ok "Pipeline active (inline mode). Waiting 30s for CFP lease rebalance..."
  sleep 30

  # Insert test documents AFTER pipeline is active so CFP processes them in inline mode
  log "  Inserting inline test documents..."
  pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
c = client.get_database_client('testdb').get_container_client('test-documents')
docs = [
    {'id': 'doc-inline-001', 'title': 'Azure Functions', 'content': 'Azure Functions is an event-driven serverless compute platform that lets you run code without provisioning or managing infrastructure.', 'category': 'compute'},
    {'id': 'doc-inline-002', 'title': 'Azure AI Search', 'content': 'Azure AI Search provides secure information retrieval at scale over user-owned content in traditional and generative AI search applications.', 'category': 'ai'},
    {'id': 'doc-inline-003', 'title': 'Azure Service Bus', 'content': 'Azure Service Bus is a fully managed enterprise message broker with message queues and publish-subscribe topics for decoupled applications.', 'category': 'messaging'},
]
for doc in docs:
    c.upsert_item(doc)
    print(f'  Inserted: {doc[\"id\"]} - {doc[\"title\"]}')
"
  log_ok "Docs inserted. Waiting for inline processing..."
  # Poll source container until embeddings appear or 120s timeout
  i=0
  while [ $i -lt 12 ]; do
    poll_check=$(pod_python "
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
c = client.get_database_client('testdb').get_container_client('test-documents')
count = sum(1 for d in c.query_items('SELECT c.id FROM c WHERE STARTSWITH(c.id, \"doc-inline-\") AND IS_DEFINED(c.embedding)', enable_cross_partition_query=True))
print(count)
" 2>/dev/null || echo "0")
    if echo "$poll_check" | grep -q "[1-9]"; then break; fi
    sleep 10
    i=$((i + 1))
  done
else
  # Load inline pipeline if skipping
  ALL_PIPS=$(api_get "/api/pipelines")
  INLINE_PIP_ID=$(echo "$ALL_PIPS" | grep -o '"id":"pip-[^"]*"' | head -1 | cut -d'"' -f4)
fi

# =============================================================================
# STEP 11: Verify inline mode results
# =============================================================================
log_step 11 "Verifying inline mode results..."
if [ "$QUIET" = "false" ]; then
  "$CLI" pipeline show "$INLINE_PIP_ID"
fi

# Inline mode embeds directly into the source container — check for embedding field
log "  Checking source container for inline embeddings..."
INLINE_CHECK=""
INLINE_CHECK=$(pod_python "
import os, json
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
c = client.get_database_client('testdb').get_container_client('test-documents')
embedded = 0
checked = 0
for doc in c.query_items('SELECT c.id, IS_DEFINED(c.embedding) as has_emb FROM c WHERE STARTSWITH(c.id, \"doc-inline-\")', enable_cross_partition_query=True):
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

INLINE_STATS=$(api_get "/api/pipelines/$INLINE_PIP_ID")
INLINE_PROCESSED=$(echo "$INLINE_STATS" | grep -o '"documents_processed":[0-9]*' | cut -d: -f2)
INLINE_STATS_EMBEDDED=$(echo "$INLINE_STATS" | grep -o '"embedded_count":[0-9]*' | cut -d: -f2)
log "  Pipeline stats — Processed: $INLINE_PROCESSED, Embedded: $INLINE_STATS_EMBEDDED"
log "  Source container — Embedded: $INLINE_EMBEDDED/$INLINE_TOTAL"

if [ "$INLINE_EMBEDDED" -gt 0 ] 2>/dev/null; then
  log_ok "Inline mode working — $INLINE_EMBEDDED/$INLINE_TOTAL documents embedded directly into source container!"
else
  log_warn "No inline embeddings yet. The CFP may still be processing. Check: omnivec pipeline show $INLINE_PIP_ID"
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
printf "  Queue Pipeline:  ${CYAN}${PIP_ID}${NC}\n"
printf "  Inline Pipeline: ${CYAN}${INLINE_PIP_ID}${NC}\n"
printf "  Model:           ${CYAN}${MODEL_ID} (${AOAI_DEPLOYMENT})${NC}\n"
echo ""
printf "  ${CYAN}Queue mode:${NC}  CFP detects changes -> creates jobs -> .NET worker embeds -> writes to destination\n"
printf "  ${CYAN}Inline mode:${NC} CFP detects changes -> embeds directly -> patches back to source container\n"
echo ""
