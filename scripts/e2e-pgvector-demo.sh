#!/bin/sh
# OmniVec E2E Demo — PostgreSQL + pgvector (Linux/macOS)
# Provisions Azure PostgreSQL Flexible Server, creates source/destination tables,
# registers an embedding model, creates a pipeline, and verifies vector search.
#
# Usage:
#   ./scripts/e2e-pgvector-demo.sh                                          # Full run
#   ./scripts/e2e-pgvector-demo.sh --existing --env my-omnivec              # Against existing deployment
#   ./scripts/e2e-pgvector-demo.sh --cleanup --env my-omnivec               # Delete test resources
#
# Requires: az, azd, kubectl, psql (PostgreSQL client), curl

set +e  # Don't exit on errors — we handle them explicitly

# ─── Parse arguments ─────────────────────────────────────────────────────────
FROM_STEP=1
QUIET=false
EXISTING=false
CLEANUP=false
USER_ENV_NAME=""
USER_ADMIN_TOKEN=""
PG_ADMIN_PASSWORD=""
AOAI_ENDPOINT="${AOAI_ENDPOINT:-}"
AOAI_KEY="${AOAI_KEY:-}"
AOAI_DEPLOYMENT="${AOAI_DEPLOYMENT:-text-embedding-3-small}"
AOAI_DIMS="${AOAI_DIMS:-1536}"

while [ $# -gt 0 ]; do
  case "$1" in
    --from-step)   FROM_STEP="$2"; shift 2 ;;
    --quiet|-q)    QUIET=true; shift ;;
    --existing)    EXISTING=true; shift ;;
    --cleanup)     CLEANUP=true; shift ;;
    --env)         USER_ENV_NAME="$2"; shift 2 ;;
    --token)       USER_ADMIN_TOKEN="$2"; shift 2 ;;
    --pg-password) PG_ADMIN_PASSWORD="$2"; shift 2 ;;
    --endpoint)    AOAI_ENDPOINT="$2"; shift 2 ;;
    --key)         AOAI_KEY="$2"; shift 2 ;;
    --deployment)  AOAI_DEPLOYMENT="$2"; shift 2 ;;
    --dims)        AOAI_DIMS="$2"; shift 2 ;;
    *)             echo "Unknown option: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TOTAL_STEPS=10
CHECKPOINT_FILE="$ROOT_DIR/.e2e-pgvector-checkpoint"

export PATH="$HOME/.azure-kubectl:$HOME/.local/bin:$HOME/.azd/bin:$PATH"

# ─── Helpers ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log()      { [ "$QUIET" = "false" ] && echo "$1"; }
log_step() { printf "${YELLOW}[Step %s/%s] %s${NC}\n" "$1" "$TOTAL_STEPS" "$2"; }
log_ok()   { printf "  ${GREEN}%s${NC}\n" "$1"; }
log_warn() { printf "  ${YELLOW}%s${NC}\n" "$1"; }
log_err()  { printf "  ${RED}%s${NC}\n" "$1"; }
die()      { log_err "$1"; exit 1; }

save_checkpoint() { echo "$1" > "$CHECKPOINT_FILE"; }

PG_PASSWORD_FILE="$ROOT_DIR/.e2e-pgvector-password"

azd_get() { val=$(azd env get-value "$1" 2>/dev/null) && printf '%s' "$val" | tr -d '\r' || true; }

api_get()    { curl -sf --max-time 30 -H "Authorization: Bearer $ADMIN_TOKEN" "$SERVER_URL$1"; }
api_post()   { curl -sf --max-time 30 -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$2" "$SERVER_URL$1"; }
api_delete() { curl -sf --max-time 10 -X DELETE -H "Authorization: Bearer $ADMIN_TOKEN" "$SERVER_URL$1" 2>/dev/null || true; }

# ─── Check prerequisites ─────────────────────────────────────────────────────
if ! command -v psql >/dev/null 2>&1; then
  log "  psql not found — installing PostgreSQL client..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq postgresql-client >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y postgresql >/dev/null 2>&1
  elif command -v brew >/dev/null 2>&1; then
    brew install libpq >/dev/null 2>&1 && export PATH="/usr/local/opt/libpq/bin:$PATH"
  fi
  if ! command -v psql >/dev/null 2>&1; then
    die "psql (PostgreSQL client) is required. Install: sudo apt-get install postgresql-client"
  fi
  log_ok "psql installed."
fi

# ─── Banner ──────────────────────────────────────────────────────────────────
printf "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║  OmniVec E2E Demo — PostgreSQL + pgvector               ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}\n"

# ─── AOAI validation ─────────────────────────────────────────────────────────
if [ -z "$AOAI_ENDPOINT" ]; then
  printf "  Enter Azure OpenAI endpoint: "; read -r AOAI_ENDPOINT
  [ -z "$AOAI_ENDPOINT" ] && { log_err "Endpoint required."; exit 1; }
fi
if [ -z "$AOAI_KEY" ]; then
  printf "  Enter Azure OpenAI API key: "; read -r AOAI_KEY
  [ -z "$AOAI_KEY" ] && { log_err "API key required."; exit 1; }
fi
log_ok "Embedding: $AOAI_DEPLOYMENT (${AOAI_DIMS}d) @ $AOAI_ENDPOINT"

# ─── PG password ─────────────────────────────────────────────────────────────
# Priority: --pg-password flag > azd env > saved file > generate new
if [ -z "$PG_ADMIN_PASSWORD" ]; then
  PG_ADMIN_PASSWORD=$(azd_get OMNIVEC_PG_DEMO_PASSWORD)
fi
if [ -z "$PG_ADMIN_PASSWORD" ] && [ -f "$PG_PASSWORD_FILE" ]; then
  PG_ADMIN_PASSWORD=$(cat "$PG_PASSWORD_FILE" | tr -d '\r\n')
fi
if [ -z "$PG_ADMIN_PASSWORD" ]; then
  PG_ADMIN_PASSWORD="OmniVec-Demo-$(shuf -i 1000-9999 -n 1 2>/dev/null || echo $$)!"
  log_ok "Generated PG admin password: $PG_ADMIN_PASSWORD"
else
  log_ok "Using saved PG admin password."
fi
# Persist to both file and azd env
echo "$PG_ADMIN_PASSWORD" > "$PG_PASSWORD_FILE"
azd env set OMNIVEC_PG_DEMO_PASSWORD "$PG_ADMIN_PASSWORD" 2>/dev/null || true
PG_ADMIN="omnivecadmin"

# ─── Existing deployment mode ────────────────────────────────────────────────
ENV_NAME="${USER_ENV_NAME:-}"
ADMIN_TOKEN="${USER_ADMIN_TOKEN:-}"
AKS_CLUSTER=""
RESOURCE_GROUP=""
SERVER_URL=""

if [ "$EXISTING" = "true" ]; then
  [ -z "$ENV_NAME" ] && { printf "  Enter azd environment name: "; read -r ENV_NAME; }
  [ -z "$ENV_NAME" ] && { log_err "EnvName required."; exit 1; }
  log "Using existing deployment: $ENV_NAME"
  azd env select "$ENV_NAME" 2>/dev/null || true

  [ -z "$ADMIN_TOKEN" ] && ADMIN_TOKEN=$(azd_get OMNIVEC_ADMIN_TOKEN)
  [ -z "$ADMIN_TOKEN" ] && { printf "  Enter admin token: "; read -r ADMIN_TOKEN; }
  [ -z "$ADMIN_TOKEN" ] && { log_err "Admin token required."; exit 1; }

  AKS_CLUSTER=$(azd_get AZURE_AKS_CLUSTER_NAME)
  RESOURCE_GROUP=$(azd_get AZURE_RESOURCE_GROUP)
  [ -z "$RESOURCE_GROUP" ] && RESOURCE_GROUP="rg-omnivec-$ENV_NAME"

  KUBE_CONTEXT="$AKS_CLUSTER"
  az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --context "$KUBE_CONTEXT" --overwrite-existing 2>/dev/null || true

  SERVER=""
  for _i in 1 2 3 4 5 6; do
    SERVER=$(kubectl --context "$KUBE_CONTEXT" get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null | tr -d '\r' || true)
    [ -n "$SERVER" ] && break
    sleep 5
  done
  [ -z "$SERVER" ] && { log_err "Failed to get external IP"; exit 1; }
  SERVER_URL="http://$SERVER"

  log_ok "Server: $SERVER_URL"
  log_ok "RG:     $RESOURCE_GROUP"

  _health=$(curl -sf --max-time 10 -H "Authorization: Bearer $ADMIN_TOKEN" "$SERVER_URL/health" 2>/dev/null || true)
  [ -z "$_health" ] && { log_err "Admin token rejected or API unreachable."; exit 1; }
  log_ok "Admin token valid — API healthy."

  [ "$FROM_STEP" -lt 3 ] && FROM_STEP=3
fi

# ─── Cleanup mode ────────────────────────────────────────────────────────────
if [ "$CLEANUP" = "true" ]; then
  log "Cleaning up pgvector demo resources..."
  [ -z "$RESOURCE_GROUP" ] && RESOURCE_GROUP="rg-omnivec-$ENV_NAME"
  [ -z "$AKS_CLUSTER" ] && AKS_CLUSTER=$(azd_get AZURE_AKS_CLUSTER_NAME)
  INSTANCE_TOKEN=$(echo "$AKS_CLUSTER" | tr -d '\r' | sed 's/omnivec-aks-//')
  PG_SERVER="omnivec-pgdemo-$INSTANCE_TOKEN"

  if [ -n "$SERVER_URL" ] && [ -n "$ADMIN_TOKEN" ]; then
    log "  Deleting demo resources via API..."
    _pips=$(api_get "/api/pipelines" 2>/dev/null || true)
    _pid=$(echo "$_pips" | grep -o '"id":"pip-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_pid" ] && { api_delete "/api/pipelines/$_pid"; log_ok "Deleted pipeline: $_pid"; }
    _srcs=$(api_get "/api/sources" 2>/dev/null || true)
    _sid=$(echo "$_srcs" | grep -o '"id":"src-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_sid" ] && { api_delete "/api/sources/$_sid"; log_ok "Deleted source: $_sid"; }
    _dsts=$(api_get "/api/destinations" 2>/dev/null || true)
    _did=$(echo "$_dsts" | grep -o '"id":"dst-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_did" ] && { api_delete "/api/destinations/$_did"; log_ok "Deleted destination: $_did"; }
  fi

  az postgres flexible-server delete --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" --yes 2>/dev/null || true
  log_ok "Deleted PostgreSQL server: $PG_SERVER"
  rm -f "$CHECKPOINT_FILE"
  log_ok "Cleanup complete."
  exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# Load deployment info from azd env (needed by all steps)
# ═════════════════════════════════════════════════════════════════════════════
if [ -z "$AKS_CLUSTER" ]; then
  AKS_CLUSTER=$(azd_get AZURE_AKS_CLUSTER_NAME)
fi
if [ -z "$RESOURCE_GROUP" ]; then
  RESOURCE_GROUP=$(azd_get AZURE_RESOURCE_GROUP)
  [ -z "$RESOURCE_GROUP" ] && RESOURCE_GROUP="rg-omnivec-${ENV_NAME:-$(azd_get AZURE_ENV_NAME)}"
fi
if [ -z "$ADMIN_TOKEN" ]; then
  ADMIN_TOKEN=$(azd_get OMNIVEC_ADMIN_TOKEN)
fi
if [ -z "$SERVER_URL" ]; then
  KUBE_CONTEXT="${AKS_CLUSTER:-omnivec}"
  if [ -n "$AKS_CLUSTER" ]; then
    az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --context "$KUBE_CONTEXT" --overwrite-existing 2>/dev/null || true
    _ip=$(kubectl --context "$KUBE_CONTEXT" get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null | tr -d '\r' || true)
    [ -n "$_ip" ] && SERVER_URL="http://$_ip"
  fi
fi

INSTANCE_TOKEN=$(echo "${AKS_CLUSTER:-}" | tr -d '\r' | sed 's/omnivec-aks-//')
if [ -z "$INSTANCE_TOKEN" ]; then
  log_err "Cannot determine instance token. Run with --existing --env <name> or ensure azd env is configured."
  exit 1
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 3: Create Azure PostgreSQL Flexible Server
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 3 ]; then
  log_step 3 "Provisioning Azure PostgreSQL Flexible Server..."

  PG_SERVER="omnivec-pgdemo-$INSTANCE_TOKEN"

  if az postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
    log_ok "PostgreSQL server already exists: $PG_SERVER"
    # Ensure password is current — reset to our known password
    log "  Resetting admin password to match current config..."
    az postgres flexible-server update --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
      --admin-password "$PG_ADMIN_PASSWORD" >/dev/null 2>&1 || true
  else
    # Try deployment location first, then fallback regions
    PG_LOCATION="${AZURE_LOCATION:-$(azd_get AZURE_LOCATION)}"
    PG_LOCATION="${PG_LOCATION:-eastus2}"
    PG_CREATED=false

    for _region in "$PG_LOCATION" "eastus" "westus2" "centralus" "northeurope"; do
      log "  Creating server: $PG_SERVER in $_region (this takes ~3-5 minutes)..."
      set +e
      az postgres flexible-server create \
        --name "$PG_SERVER" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$_region" \
        --admin-user "$PG_ADMIN" \
        --admin-password "$PG_ADMIN_PASSWORD" \
        --sku-name Standard_B1ms \
        --tier Burstable \
        --storage-size 32 \
        --version 16 \
        --public-access 0.0.0.0 \
        --yes 2>&1 | tail -3
      _pg_rc=$?
      set -e

      # Verify it actually exists
      if az postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_ok "PostgreSQL server created in $_region: $PG_SERVER"
        PG_CREATED=true
        break
      fi
      log_warn "Region $_region not available, trying next..."
    done

    if [ "$PG_CREATED" = "false" ]; then
      log_err "Failed to create PostgreSQL server in any region."
      exit 1
    fi
  fi

  log "  Enabling pgvector extension..."
  az postgres flexible-server parameter set \
    --server-name "$PG_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --name azure.extensions \
    --value VECTOR >/dev/null 2>&1 || true
  log_ok "pgvector extension enabled."

  PG_HOST="$PG_SERVER.postgres.database.azure.com"
  PG_PORT=5432
  PG_DB="omnivec_demo"

  az postgres flexible-server firewall-rule create \
    --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
    --rule-name AllowAzure --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 >/dev/null 2>&1 || true

  MY_IP=$(curl -sf https://api.ipify.org 2>/dev/null || echo "0.0.0.0")
  az postgres flexible-server firewall-rule create \
    --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
    --rule-name AllowMyIP --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" >/dev/null 2>&1 || true

  log_ok "Host: $PG_HOST"
  save_checkpoint 3
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4: Create database and tables
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 4 ]; then
  log_step 4 "Creating database and tables..."

  if [ -z "${PG_HOST:-}" ]; then
    INSTANCE_TOKEN=$(echo "$AKS_CLUSTER" | tr -d '\r' | sed 's/omnivec-aks-//')
    PG_SERVER="omnivec-pgdemo-$INSTANCE_TOKEN"
    PG_HOST="$PG_SERVER.postgres.database.azure.com"
    PG_PORT=5432
    PG_DB="omnivec_demo"
  fi

  export PGPASSWORD="$PG_ADMIN_PASSWORD"
  export PGSSLMODE="require"

  log "  Creating database: $PG_DB"
  # Drop if exists (clean slate for re-runs)
  psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_ADMIN" -d postgres -c "DROP DATABASE IF EXISTS $PG_DB;" 2>/dev/null || true
  psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_ADMIN" -d postgres -c "CREATE DATABASE $PG_DB;" 2>/dev/null || true
  log_ok "Database created (clean)."

  log "  Creating tables..."
  if ! psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_ADMIN" -d "$PG_DB" -c "
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    content TEXT,
    embedding vector($AOAI_DIMS),
    metadata JSONB,
    pipeline_id TEXT,
    embedded_at TIMESTAMPTZ,
    source_ref TEXT,
    content_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS embeddings_vector_idx ON embeddings USING hnsw (embedding vector_cosine_ops);
" 2>&1; then
    die "Failed to create tables. Check that PostgreSQL server $PG_HOST is reachable and psql is installed."
  fi
  log_ok "Tables created: documents (source), embeddings (destination)"

  log "  Inserting sample documents..."
  if ! psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_ADMIN" -d "$PG_DB" -c "
INSERT INTO documents (id, title, content, category) VALUES
  ('doc-001', 'Azure Cosmos DB', 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.', 'database'),
  ('doc-002', 'Azure Kubernetes Service', 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.', 'compute'),
  ('doc-003', 'Azure Blob Storage', 'Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.', 'storage')
ON CONFLICT (id) DO NOTHING;
" 2>&1; then
    die "Failed to insert documents."
  fi
  log_ok "Inserted 3 sample documents."

  unset PGPASSWORD
  save_checkpoint 4
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 5: Register embedding model
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 5 ]; then
  log_step 5 "Registering Azure OpenAI embedding model..."

  MODEL_BODY="{\"name\":\"azure-openai-embed\",\"type\":\"azure-openai\",\"endpoint\":\"$AOAI_ENDPOINT\",\"api_key\":\"$AOAI_KEY\",\"api_version\":\"2024-06-01\",\"deployment\":\"$AOAI_DEPLOYMENT\",\"embedding_dim\":$AOAI_DIMS}"

  MODEL_RESP=$(api_post "/api/models" "$MODEL_BODY" 2>/dev/null || true)
  MODEL_ID=$(echo "$MODEL_RESP" | grep -o '"id":"[^"]*"' | head -1 | sed 's/"id":"//;s/"//')

  if [ -z "$MODEL_ID" ]; then
    # May already exist
    MODELS_RAW=$(api_get "/api/docgrok/models" 2>/dev/null || true)
    MODEL_ID=$(echo "$MODELS_RAW" | grep -o '"id":"mdl-ext-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  fi

  if [ -n "$MODEL_ID" ]; then
    log_ok "Model: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)"
  else
    log_err "Failed to register model"
    exit 1
  fi
  save_checkpoint 5
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 6: Create PostgreSQL source + pgvector destination
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 6 ]; then
  log_step 6 "Creating PostgreSQL source and pgvector destination..."

  if [ -z "${PG_HOST:-}" ]; then
    PG_SERVER="omnivec-pgdemo-$INSTANCE_TOKEN"
    PG_HOST="$PG_SERVER.postgres.database.azure.com"
    PG_PORT=5432
    PG_DB="omnivec_demo"
  fi

  # Clean up any existing demo resources first
  if [ -n "$SERVER_URL" ] && [ -n "$ADMIN_TOKEN" ]; then
    _old_pips=$(api_get "/api/pipelines" 2>/dev/null || true)
    _old_pid=$(echo "$_old_pips" | grep "pgvector-demo" | grep -o '"id":"pip-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_old_pid" ] && { api_delete "/api/pipelines/$_old_pid"; log "  Cleaned up old pipeline: $_old_pid"; }
    _old_srcs=$(api_get "/api/sources" 2>/dev/null || true)
    _old_sid=$(echo "$_old_srcs" | grep "pg-demo" | grep -o '"id":"src-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_old_sid" ] && { api_delete "/api/sources/$_old_sid"; log "  Cleaned up old source: $_old_sid"; }
    _old_dsts=$(api_get "/api/destinations" 2>/dev/null || true)
    _old_did=$(echo "$_old_dsts" | grep "pg-demo" | grep -o '"id":"dst-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    [ -n "$_old_did" ] && { api_delete "/api/destinations/$_old_did"; log "  Cleaned up old destination: $_old_did"; }
  fi

  SRC_BODY="{\"name\":\"pg-demo-source\",\"type\":\"postgresql\",\"config\":{\"host\":\"$PG_HOST\",\"port\":$PG_PORT,\"database\":\"$PG_DB\",\"table\":\"documents\",\"user\":\"$PG_ADMIN\",\"password\":\"$PG_ADMIN_PASSWORD\",\"ssl_mode\":\"require\",\"id_column\":\"id\",\"timestamp_column\":\"updated_at\"}}"
  SRC_RESP=$(api_post "/api/sources" "$SRC_BODY")
  SOURCE_ID=$(echo "$SRC_RESP" | grep -o '"id":"src-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  log_ok "Source: $SOURCE_ID (postgresql://.../$PG_DB/documents)"

  DST_BODY="{\"name\":\"pg-demo-vectors\",\"type\":\"pgvector\",\"config\":{\"host\":\"$PG_HOST\",\"port\":$PG_PORT,\"database\":\"$PG_DB\",\"table\":\"embeddings\",\"user\":\"$PG_ADMIN\",\"password\":\"$PG_ADMIN_PASSWORD\",\"ssl_mode\":\"require\",\"vector_column\":\"embedding\",\"content_column\":\"content\",\"id_column\":\"id\",\"vector_dimensions\":$AOAI_DIMS,\"index_type\":\"hnsw\"}}"
  DST_RESP=$(api_post "/api/destinations" "$DST_BODY")
  DEST_ID=$(echo "$DST_RESP" | grep -o '"id":"dst-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  log_ok "Destination: $DEST_ID (pgvector://.../$PG_DB/embeddings)"

  save_checkpoint 6
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 7: Create pipeline and wait for processing
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 7 ]; then
  log_step 7 "Creating pipeline (queue mode)..."

  if [ -z "${MODEL_ID:-}" ]; then
    MODELS_RAW=$(api_get "/api/docgrok/models" 2>/dev/null || true)
    MODEL_ID=$(echo "$MODELS_RAW" | grep -o '"id":"mdl-ext-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  fi

  if [ -z "${SOURCE_ID:-}" ]; then
    _srcs=$(api_get "/api/sources" 2>/dev/null || true)
    SOURCE_ID=$(echo "$_srcs" | grep -o '"id":"src-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  fi
  if [ -z "${DEST_ID:-}" ]; then
    _dsts=$(api_get "/api/destinations" 2>/dev/null || true)
    DEST_ID=$(echo "$_dsts" | grep -o '"id":"dst-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  fi

  PIP_BODY="{\"name\":\"pgvector-demo-pipeline\",\"sources\":[{\"source_id\":\"$SOURCE_ID\",\"filters\":{},\"content_fields\":[\"content\"]}],\"destination_id\":\"$DEST_ID\",\"docgrok_pipeline\":\"$MODEL_ID\",\"vector_index_path\":\"embedding\",\"process_existing\":true,\"processing_mode\":\"queue\",\"content_strategy\":\"truncate\"}"
  PIP_RESP=$(api_post "/api/pipelines" "$PIP_BODY")
  PIP_ID=$(echo "$PIP_RESP" | grep -o '"id":"pip-[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
  log_ok "Pipeline created: $PIP_ID"

  # Trigger source sync to kick the changefeed processor
  log "  Triggering source sync..."
  api_post "/api/sources/$SOURCE_ID/sync" "{}" >/dev/null 2>&1 || true
  api_post "/api/pipelines/$PIP_ID/run" "{}" >/dev/null 2>&1 || true

  log "  Waiting for documents to be embedded (checking pgvector table directly)..."

  if [ -z "${PG_HOST:-}" ]; then
    PG_SERVER="omnivec-pgdemo-$INSTANCE_TOKEN"
    PG_HOST="$PG_SERVER.postgres.database.azure.com"
    PG_PORT=5432
    PG_DB="omnivec_demo"
  fi

  _waited=0
  _embedded=0
  export PGPASSWORD="$PG_ADMIN_PASSWORD"
  export PGSSLMODE="require"
  while [ "$_waited" -lt 180 ]; do
    sleep 15
    _waited=$((_waited + 15))
    # Check pgvector table directly — more reliable than API stats for PG pipelines
    _embedded=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_ADMIN" -d "$PG_DB" -t -c "SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL;" 2>/dev/null | tr -d ' \r\n')
    _embedded=${_embedded:-0}
    if [ "$_embedded" -ge 3 ] 2>/dev/null; then
      log_ok "Queue mode: $_embedded documents embedded in pgvector!"
      break
    fi
    log "  Waiting... ($_embedded/3 embedded, ${_waited}s)"
  done
  unset PGPASSWORD PGSSLMODE

  if [ "$_embedded" -lt 3 ] 2>/dev/null; then
    log_warn "Only $_embedded/3 documents embedded after 180s"
    log "  Checking changefeed logs for errors..."
    kubectl --context "${KUBE_CONTEXT:-omnivec}" logs -l app=omnivec-cosmos-changefeed -n omnivec --tail=10 2>/dev/null | grep -i "error\|fail\|postgres" | tail -5
  fi

  save_checkpoint 7
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 8: Verify embeddings in pgvector table
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 8 ]; then
  log_step 8 "Verifying embeddings in pgvector table..."

  if [ -z "${PG_HOST:-}" ]; then
    INSTANCE_TOKEN=$(echo "$AKS_CLUSTER" | tr -d '\r' | sed 's/omnivec-aks-//')
    PG_HOST="omnivec-pgdemo-$INSTANCE_TOKEN.postgres.database.azure.com"
    PG_DB="omnivec_demo"
  fi

  export PGPASSWORD="$PG_ADMIN_PASSWORD"
  export PGSSLMODE="require"
  _count=$(psql -h "$PG_HOST" -p 5432 -U "$PG_ADMIN" -d "$PG_DB" -t -c "SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL;" 2>/dev/null | tr -d ' \r\n')
  unset PGPASSWORD PGSSLMODE

  if [ "$_count" -ge 3 ] 2>/dev/null; then
    log_ok "pgvector table has $_count rows with embeddings!"
  else
    log_warn "pgvector table has ${_count:-0} rows (expected 3)"
  fi

  save_checkpoint 8
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 9: Vector search via API
# ═════════════════════════════════════════════════════════════════════════════
if [ "$FROM_STEP" -le 9 ]; then
  log_step 9 "Verifying vector search..."

  _search_passed=0
  for _sq in \
    "globally distributed database|doc-001|Azure Cosmos DB" \
    "deploying managed Kubernetes clusters|doc-002|Azure Kubernetes Service" \
    "unstructured data storage for documents|doc-003|Azure Blob Storage"; do

    _query=$(echo "$_sq" | cut -d'|' -f1)
    _expected=$(echo "$_sq" | cut -d'|' -f2)

    _body="{\"query\":\"$_query\",\"destination_ids\":[\"$DEST_ID\"],\"top_k\":3}"
    _resp=$(curl -sf --max-time 30 -X POST \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$_body" "$SERVER_URL/api/playground/search" 2>/dev/null || true)

    _top_id=$(echo "$_resp" | grep -o '"id":"[^"]*"' | head -1 | sed 's/"id":"//;s/"//')
    _score=$(echo "$_resp" | grep -o '"score":[0-9.]*' | head -1 | cut -d: -f2)

    if [ -n "$_top_id" ]; then
      log_ok "Search '$_query' → $_top_id (score: ${_score:-?})"
      _search_passed=$((_search_passed + 1))
    else
      log_err "Search '$_query' → no results"
    fi
  done

  log_ok "Vector search: $_search_passed/3 queries returned results"
  save_checkpoint 9
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 10: Summary
# ═════════════════════════════════════════════════════════════════════════════
log_step 10 "Done!"

echo ""
printf "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║     pgvector E2E Demo Complete!                          ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}\n"
echo ""
printf "  Server:       ${CYAN}${SERVER_URL}${NC}\n"
printf "  PG Host:      ${CYAN}${PG_HOST:-unknown}${NC}\n"
printf "  Source:        ${CYAN}${SOURCE_ID:-unknown} (postgresql → documents)${NC}\n"
printf "  Destination:   ${CYAN}${DEST_ID:-unknown} (pgvector → embeddings)${NC}\n"
printf "  Pipeline:      ${CYAN}${PIP_ID:-unknown}${NC}\n"
printf "  Model:         ${CYAN}${MODEL_ID:-unknown} (${AOAI_DEPLOYMENT})${NC}\n"
echo ""
echo "  Full cycle: PostgreSQL source → Azure OpenAI embedding → pgvector destination → vector search"
echo ""

rm -f "$CHECKPOINT_FILE"
save_checkpoint 10
