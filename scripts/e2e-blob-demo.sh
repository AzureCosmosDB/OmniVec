#!/usr/bin/env bash
# OmniVec E2E Demo — Azure Blob (txt) → Cosmos DB (vectors)
#
# Exercises the full pipeline against an existing azd deployment:
#   1. Upload sample .txt files to a blob container
#   2. Register an embedding model (Azure OpenAI)
#   3. Create an azure-blob source + cosmosdb-vector destination
#   4. Create + activate a pipeline (queue mode by default, inline with --skip-queue)
#   5. Poll until vectors land in the destination container
#   6. Run a semantic query via the omnivec-search service
#
# Prereqs:
#   - azd environment already provisioned (azd up) — pass --env <name>
#   - Azure CLI signed in to the same subscription
#   - Azure OpenAI resource with a text-embedding deployment
#
# Usage:
#   ./scripts/e2e-blob-demo.sh --env my-omnivec \
#       --endpoint https://my-aoai.openai.azure.com --key $AOAI_KEY

set -eo pipefail

# ─── Defaults / args ────────────────────────────────────────────────────────
ENV_NAME=""
ADMIN_TOKEN="${OMNIVEC_ADMIN_TOKEN:-}"
AOAI_ENDPOINT="${AOAI_ENDPOINT:-}"
AOAI_KEY="${AOAI_KEY:-}"
AOAI_DEPLOYMENT="${AOAI_DEPLOYMENT:-text-embedding-3-small}"
AOAI_DIMS="${AOAI_DIMS:-1536}"
CONTAINER="e2e-blob-txt"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAMPLES_DIR="$SCRIPT_DIR/samples/blob-txt"
CLEANUP=false
NO_SEARCH=false
SKIP_QUEUE=false

show_help() {
  cat <<EOF
OmniVec E2E Demo — Azure Blob (txt) → Cosmos DB Vectors (Linux/macOS/WSL)

OPTIONS:
  --env NAME            azd environment name.
  --token TOKEN         OmniVec admin token (skips auto-discovery).
  --endpoint URL        Azure OpenAI endpoint.
  --key KEY             Azure OpenAI API key.
  --deployment NAME     Embedding deployment (default: text-embedding-3-small).
  --dims N              Embedding dimensions (default: 1536).
  --container NAME      Blob container name (default: e2e-blob-txt).
  --samples-dir PATH    Directory of .txt samples to upload.
  --skip-queue          Create pipeline in inline mode (bypass queue flow).
  --cleanup             Delete demo objects + blob container at end.
  --no-search           Skip the semantic-search validation step.
  -h, --help            Show this help.

ENVIRONMENT VARIABLES (used when flag not passed):
  AZURE_ENV_NAME, OMNIVEC_ADMIN_TOKEN, AOAI_ENDPOINT, AOAI_KEY,
  AOAI_DEPLOYMENT, AOAI_DIMS

Windows users: use the PowerShell variant instead:
  pwsh scripts/e2e-blob-demo.ps1 -h
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) show_help; exit 0 ;;
    --env) ENV_NAME="$2"; shift 2 ;;
    --token) ADMIN_TOKEN="$2"; shift 2 ;;
    --endpoint) AOAI_ENDPOINT="$2"; shift 2 ;;
    --key) AOAI_KEY="$2"; shift 2 ;;
    --deployment) AOAI_DEPLOYMENT="$2"; shift 2 ;;
    --dims) AOAI_DIMS="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --samples-dir) SAMPLES_DIR="$2"; shift 2 ;;
    --skip-queue) SKIP_QUEUE=true; shift ;;
    --cleanup) CLEANUP=true; shift ;;
    --no-search) NO_SEARCH=true; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ─── Logging ────────────────────────────────────────────────────────────────
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
log()      { printf "  %s\n" "$*"; }
log_step() { printf "\n${CYAN}─── Step %s : %s${NC}\n" "$1" "$2"; }
log_ok()   { printf "  ${GREEN}✓${NC} %s\n" "$*"; }
log_warn() { printf "  ${YELLOW}!${NC} %s\n" "$*"; }
log_err()  { printf "  ${RED}✗${NC} %s\n" "$*" >&2; }

azd_value() {
  # Fetch a single key from `azd env get-values --output json`
  azd env get-values --output json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('$1', ''))
except Exception:
    pass
"
}

api_call() {
  # $1=method $2=path $3=body(optional)
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS --max-time 60 -X "$method" "$SERVER_URL$path" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -sS --max-time 60 -X "$method" "$SERVER_URL$path" \
      -H "Authorization: Bearer $ADMIN_TOKEN"
  fi
}

json_field() {
  # $1=json $2=dot-path (e.g. "pipeline.id")
  python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    for p in sys.argv[2].split('.'):
        if isinstance(d, dict):
            d = d.get(p)
        else:
            d = None; break
    print(d if d is not None else '')
except Exception:
    pass
" "$1" "$2"
}

# ─── Banner ─────────────────────────────────────────────────────────────────
printf "\n${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}\n"
printf   "${GREEN}║  OmniVec E2E Demo — Azure Blob (txt) → Cosmos DB Vectors  ║${NC}\n"
printf   "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}\n"

# ─── Samples check ──────────────────────────────────────────────────────────
if [ ! -d "$SAMPLES_DIR" ]; then
  log_err "Samples directory not found: $SAMPLES_DIR"
  exit 1
fi

# ─── Select azd env ─────────────────────────────────────────────────────────
if [ -n "$ENV_NAME" ]; then
  azd env select "$ENV_NAME" >/dev/null
  log_ok "Using azd env: $ENV_NAME"
else
  CURRENT=$(azd env list --output json 2>/dev/null | python3 -c "
import json, sys
try:
    for e in json.load(sys.stdin):
        if e.get('IsDefault'):
            print(e.get('Name',''))
            break
except Exception:
    pass
")
  if [ -z "$CURRENT" ]; then
    log_err "No azd environment selected. Pass --env <name> or run azd env select."
    exit 1
  fi
  log_ok "Using azd env: $CURRENT"
fi

# ─── Resolve deployment details ─────────────────────────────────────────────
log_step 1 "Resolving deployment details from azd"
RESOURCE_GROUP=$(azd_value AZURE_RESOURCE_GROUP)
STORAGE_ACCT=$(azd_value AZURE_STORAGE_ACCOUNT_NAME)
BLOB_ENDPOINT=$(azd_value AZURE_STORAGE_BLOB_ENDPOINT)
IDENTITY_CID=$(azd_value AZURE_IDENTITY_CLIENT_ID)
[ -z "$IDENTITY_CID" ] && IDENTITY_CID=$(azd_value OMNIVEC_IDENTITY_CLIENT_ID)
[ -z "$ADMIN_TOKEN" ] && ADMIN_TOKEN=$(azd_value OMNIVEC_ADMIN_TOKEN)

for pair in "AZURE_RESOURCE_GROUP:$RESOURCE_GROUP" "AZURE_STORAGE_ACCOUNT_NAME:$STORAGE_ACCT" "OMNIVEC_ADMIN_TOKEN:$ADMIN_TOKEN"; do
  key="${pair%%:*}"; val="${pair#*:}"
  if [ -z "$val" ]; then
    log_err "Missing azd env value: $key. Run 'azd up' first or pass flags."
    exit 1
  fi
done

COSMOS_ENDPOINT=$(azd_value AZURE_COSMOS_ENDPOINT)
if [ -z "$COSMOS_ENDPOINT" ]; then
  COSMOS_ENDPOINT=$(az cosmosdb list --resource-group "$RESOURCE_GROUP" \
    --query "[?contains(name,'omnivec-cosmos')].documentEndpoint | [0]" -o tsv 2>/dev/null)
fi
if [ -z "$COSMOS_ENDPOINT" ]; then
  log_err "Could not locate OmniVec Cosmos account in RG $RESOURCE_GROUP"
  exit 1
fi

# AKS credentials
AKS_NAME=$(az aks list --resource-group "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null)
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" \
  --overwrite-existing --only-show-errors >/dev/null 2>&1 || true

EXT_IP=$(kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
if [ -z "$EXT_IP" ]; then
  EXT_IP=$(kubectl get svc omnivec-api -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
fi
if [ -z "$EXT_IP" ]; then
  log_err "No external IP found on omnivec-web or omnivec-api — is the cluster up?"
  exit 1
fi
SERVER_URL="http://$EXT_IP"
SEARCH_IP=$(kubectl get svc omnivec-search -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
SEARCH_TOKEN=$(azd_value OMNIVEC_SEARCH_TOKEN)

log_ok "RG              : $RESOURCE_GROUP"
log_ok "Storage account : $STORAGE_ACCT"
log_ok "Cosmos endpoint : $COSMOS_ENDPOINT"
log_ok "API             : $SERVER_URL"
if [ -n "$SEARCH_IP" ]; then log_ok "Search          : http://$SEARCH_IP"; else log_warn "omnivec-search external IP not yet available"; fi

# ─── Validate API + token ───────────────────────────────────────────────────
log_step 2 "Validating API + admin token"
if ! curl -sS --max-time 10 "$SERVER_URL/health" >/dev/null; then
  log_err "API /health unreachable at $SERVER_URL"
  exit 1
fi
if ! api_call GET "/api/auth/whoami" >/dev/null 2>&1; then
  if ! api_call GET "/api/sources" >/dev/null 2>&1; then
    log_err "Admin token rejected by API"
    exit 1
  fi
fi
log_ok "Admin token accepted"

# ─── AOAI creds ─────────────────────────────────────────────────────────────
if [ -z "$AOAI_ENDPOINT" ]; then
  printf "  Azure OpenAI endpoint (https://<res>.openai.azure.com): "
  read -r AOAI_ENDPOINT
fi
if [ -z "$AOAI_KEY" ]; then
  printf "  Azure OpenAI API key: "
  read -r -s AOAI_KEY
  echo
fi
if [ -z "$AOAI_ENDPOINT" ] || [ -z "$AOAI_KEY" ]; then
  log_err "AOAI endpoint + key required"
  exit 1
fi

# ─── Register embedding model (idempotent) ──────────────────────────────────
log_step 3 "Registering Azure OpenAI embedding model"
MODEL_NAME="e2e-blob-embed"
EXISTING_MODELS=$(api_call GET "/api/models" 2>/dev/null || echo '{"models":[]}')
MODEL_ID=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    for m in d.get('models', []):
        if m.get('name') == '$MODEL_NAME':
            print(m.get('id','')); break
except Exception:
    pass
" "$EXISTING_MODELS")

if [ -z "$MODEL_ID" ]; then
  MODEL_BODY=$(cat <<EOF
{"name":"$MODEL_NAME","type":"azure-openai","endpoint":"$AOAI_ENDPOINT","api_key":"$AOAI_KEY","model":"$AOAI_DEPLOYMENT","deployment":"$AOAI_DEPLOYMENT","dimensions":$AOAI_DIMS,"api_version":"2024-06-01"}
EOF
)
  RESP=$(api_call POST "/api/models" "$MODEL_BODY")
  MODEL_ID=$(json_field "$RESP" id)
  log_ok "Registered model: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)"
else
  log_ok "Re-using existing model: $MODEL_ID"
fi

# ─── Blob container + upload samples ────────────────────────────────────────
log_step 4 "Preparing blob container + uploading samples"
ALLOW_KEY=$(az storage account show --name "$STORAGE_ACCT" --resource-group "$RESOURCE_GROUP" \
  --query "allowSharedKeyAccess" -o tsv 2>/dev/null)
if [ "$ALLOW_KEY" = "true" ]; then
  STORAGE_KEY=$(az storage account keys list --account-name "$STORAGE_ACCT" \
    --resource-group "$RESOURCE_GROUP" --query "[0].value" -o tsv 2>/dev/null)
  AUTH_ARGS=(--account-key "$STORAGE_KEY")
  log "Using shared-key auth for local upload"
else
  AUTH_ARGS=(--auth-mode login)
  log "Shared keys disabled — using AAD (signed-in user) for local upload"
  ME=$(az ad signed-in-user show --query id -o tsv 2>/dev/null)
  SA_ID=$(az storage account show --name "$STORAGE_ACCT" --resource-group "$RESOURCE_GROUP" --query id -o tsv 2>/dev/null)
  if [ -n "$ME" ] && [ -n "$SA_ID" ]; then
    HAS_ROLE=$(az role assignment list --assignee "$ME" --scope "$SA_ID" \
      --role "Storage Blob Data Contributor" --query "[0].id" -o tsv 2>/dev/null)
    if [ -z "$HAS_ROLE" ]; then
      log "Granting 'Storage Blob Data Contributor' to signed-in user (one-time)"
      az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
        --role "Storage Blob Data Contributor" --scope "$SA_ID" --only-show-errors >/dev/null 2>&1 || true
      log "Waiting 45s for RBAC propagation..."
      sleep 45
    fi
  fi
fi

az storage container create --account-name "$STORAGE_ACCT" --name "$CONTAINER" \
  "${AUTH_ARGS[@]}" --only-show-errors >/dev/null
log_ok "Container ready: $CONTAINER"

SAMPLE_COUNT=0
for f in "$SAMPLES_DIR"/*.txt; do
  [ -e "$f" ] || continue
  az storage blob upload --account-name "$STORAGE_ACCT" --container-name "$CONTAINER" \
    --name "$(basename "$f")" --file "$f" "${AUTH_ARGS[@]}" --overwrite --only-show-errors >/dev/null
  log_ok "Uploaded $(basename "$f")"
  SAMPLE_COUNT=$((SAMPLE_COUNT + 1))
done
if [ "$SAMPLE_COUNT" -eq 0 ]; then
  log_err "No .txt samples in $SAMPLES_DIR"
  exit 1
fi

# ─── Cosmos database + vectors container ────────────────────────────────────
log_step 5 "Ensuring Cosmos database + vectors container"
COSMOS_ACCT=$(echo "$COSMOS_ENDPOINT" | sed -E 's|https://([^.]+)\..*|\1|')
DB_NAME="e2eblob"
VEC_CONTAINER="vectors"

az cosmosdb sql database create --account-name "$COSMOS_ACCT" --resource-group "$RESOURCE_GROUP" \
  --name "$DB_NAME" --only-show-errors >/dev/null 2>&1 || true

API_POD=$(kubectl get pods -n omnivec -l app=omnivec-api -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$API_POD" ]; then log_err "No omnivec-api pod running"; exit 1; fi

PY_SCRIPT=$(cat <<PYEOF
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$COSMOS_ENDPOINT", credential=cred)
db = client.get_database_client("$DB_NAME")
vp = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": $AOAI_DIMS}]}
ip = {"vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
try:
    db.create_container(id="$VEC_CONTAINER", partition_key={"paths": ["/id"], "kind": "Hash"}, vector_embedding_policy=vp, indexing_policy=ip)
    print("OK: vectors container created")
except Exception as e:
    if "Conflict" in str(e) or "already exists" in str(e).lower():
        print("OK: vectors container already exists")
    else:
        print(f"ERR: {e}")
        raise
PYEOF
)
ENCODED=$(echo -n "$PY_SCRIPT" | base64 -w 0)
OUT=$(kubectl exec -n omnivec "$API_POD" -- sh -c "echo $ENCODED | base64 -d | python3 -" 2>&1 || true)
if echo "$OUT" | grep -q "OK:"; then
  log_ok "$(echo "$OUT" | tr '\n' ' ')"
else
  log_err "Vectors container setup failed: $OUT"
  exit 1
fi

# ─── Source + destination + pipeline ────────────────────────────────────────
log_step 6 "Creating source, destination, and pipeline"
SOURCE_NAME="e2e-blob-source"
DEST_NAME="e2e-blob-dest"
PIPE_NAME="e2e-blob-pipeline"

# Clean up existing demo objects for idempotency
for kind in pipelines sources destinations; do
  LIST=$(api_call GET "/api/$kind" 2>/dev/null || echo '{}')
  IDS=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    for it in d.get('$kind', []):
        if it.get('name') in ('$SOURCE_NAME', '$DEST_NAME', '$PIPE_NAME'):
            print(it.get('id',''))
except Exception:
    pass
" "$LIST")
  for id in $IDS; do
    [ -n "$id" ] && api_call DELETE "/api/$kind/$id" >/dev/null 2>&1 || true
  done
done

SRC_BODY=$(cat <<EOF
{"name":"$SOURCE_NAME","type":"azure-blob","config":{"account_url":"$BLOB_ENDPOINT","container":"$CONTAINER","file_type":"txt","auth_type":"managed-identity"}}
EOF
)
SRC_RESP=$(api_call POST "/api/sources" "$SRC_BODY")
SOURCE_ID=$(json_field "$SRC_RESP" source.id)
log_ok "Source: $SOURCE_ID"

DST_BODY=$(cat <<EOF
{"name":"$DEST_NAME","type":"cosmosdb-vector","config":{"endpoint":"$COSMOS_ENDPOINT","database":"$DB_NAME","container":"$VEC_CONTAINER","auth_type":"managed-identity","client_id":"$IDENTITY_CID","vector_dimensions":$AOAI_DIMS,"vector_field":"embedding"}}
EOF
)
DST_RESP=$(api_call POST "/api/destinations" "$DST_BODY")
DEST_ID=$(json_field "$DST_RESP" destination.id)
log_ok "Destination: $DEST_ID"

# DocGrok pipelines registration (idempotent)
WORKER_URL="http://pipeline-worker-svc.omnivec.svc.cluster.local:8080"

register_docgrok() {
  local display="$1" model="$2"
  local body='{"name":"'"$display"'","worker_url":"'"$WORKER_URL"'","model_id":"'"$model"'","type":"embedding"}'
  local resp
  resp=$(echo "$body" | kubectl exec -i -n omnivec "$API_POD" -- \
    curl -sS -X POST "http://docgrok.omnivec.svc.cluster.local/admin/pipelines" \
    -H "content-type: application/json" --data-binary "@-" 2>&1 || true)
  local id
  id=$(json_field "$resp" id)
  if [ -n "$id" ]; then
    log_ok "DocGrok pipeline registered: $display -> id=$id (model=$model)"
    echo "$id"
  else
    log_warn "DocGrok pipeline $display registration failed: $resp"
    echo ""
  fi
}
DG_TEXT_ID=$(register_docgrok "DocGrok Text" "$MODEL_ID")
DG_PDF_ID=$(register_docgrok "DocGrok PDF" "$MODEL_ID")

if [ "$SKIP_QUEUE" = "true" ]; then
  PIP_MODE="inline"
  log "Pipeline mode: inline (--skip-queue)"
else
  PIP_MODE="queue"
fi

PIP_BODY=$(cat <<EOF
{"name":"$PIPE_NAME","sources":[{"source_id":"$SOURCE_ID","filters":{},"content_fields":["content"],"file_types":["txt"]}],"destination_id":"$DEST_ID","docgrok_pipeline":"$DG_TEXT_ID","vector_index_path":"embedding","process_existing":true,"processing_mode":"$PIP_MODE"}
EOF
)
PIP_RESP=$(api_call POST "/api/pipelines" "$PIP_BODY")
PIPE_ID=$(json_field "$PIP_RESP" pipeline.id)
log_ok "Pipeline: $PIPE_ID ($PIP_MODE mode)"

# ─── Activate pipeline and poll for vectors ─────────────────────────────────
log_step 7 "Activating pipeline and waiting for embeddings"
api_call POST "/api/sources/$SOURCE_ID/sync" "{}" >/dev/null
log_ok "Pipeline activated — controller will enumerate blobs"

EXPECTED=$SAMPLE_COUNT
DEADLINE=$(( $(date +%s) + 300 ))
LAST_COUNT=-1
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  COUNT_SCRIPT=$(cat <<PYEOF
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$COSMOS_ENDPOINT", credential=cred)
c = client.get_database_client("$DB_NAME").get_container_client("$VEC_CONTAINER")
q = list(c.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))
print(f"COUNT={q[0]}")
PYEOF
)
  ENCODED=$(echo -n "$COUNT_SCRIPT" | base64 -w 0)
  OUT=$(kubectl exec -n omnivec "$API_POD" -- sh -c "echo $ENCODED | base64 -d | python3 -" 2>&1 || true)
  if echo "$OUT" | grep -qE 'COUNT=[0-9]+'; then
    N=$(echo "$OUT" | grep -oE 'COUNT=[0-9]+' | head -1 | cut -d= -f2)
    if [ "$N" != "$LAST_COUNT" ]; then
      log "  vectors embedded: $N / $EXPECTED"
      LAST_COUNT=$N
    fi
    if [ "$N" -ge "$EXPECTED" ] 2>/dev/null; then
      log_ok "All $EXPECTED files embedded"
      break
    fi
  fi
  sleep 10
done
if [ "$LAST_COUNT" -lt "$EXPECTED" ] 2>/dev/null; then
  log_warn "Only $LAST_COUNT / $EXPECTED vectors after 5 minutes. Check: kubectl logs -n omnivec deploy/omnivec-controller"
fi

# ─── Query via omnivec-search ───────────────────────────────────────────────
if [ "$NO_SEARCH" != "true" ] && [ -n "$SEARCH_IP" ] && [ -n "$SEARCH_TOKEN" ]; then
  log_step 8 "Querying via omnivec-search"
  SEARCH_BODY=$(cat <<EOF
{"query":"how does kubernetes help run microservices","top_k":3,"indexes":[{"id":"e2e-blob","store":{"type":"cosmosdb","endpoint":"$COSMOS_ENDPOINT","database":"$DB_NAME","container":"$VEC_CONTAINER","auth":{"mode":"managed_identity"}},"vector":{"field":"embedding","dims":$AOAI_DIMS,"metric":"cosine"},"embedding":{"policy":"model","model_id":"$MODEL_ID"},"content_fields":["content"]}],"merge":{"strategy":"rrf"}}
EOF
)
  SRESP=$(curl -sS --max-time 30 -X POST "http://$SEARCH_IP/search" \
    -H "Authorization: Bearer $SEARCH_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$SEARCH_BODY" 2>&1 || true)
  if echo "$SRESP" | grep -q '"results"'; then
    COUNT=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(len(d.get('results',[])))
except Exception:
    print(0)
" "$SRESP")
    log_ok "Got $COUNT result(s):"
    python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    for r in d.get('results', [])[:3]:
        txt = (r.get('text') or '')[:80]
        print(f\"    [{r.get('rank')}] score={round(float(r.get('score') or 0),4)}  {txt}...\")
except Exception as e:
    print(f'  (could not parse results: {e})')
" "$SRESP"
  else
    log_warn "Search query failed: $SRESP"
  fi
elif [ "$NO_SEARCH" = "true" ]; then
  log_warn "Skipping search (--no-search passed)"
else
  log_warn "Skipping search (no IP or token)"
fi

# ─── Cleanup ────────────────────────────────────────────────────────────────
if [ "$CLEANUP" = "true" ]; then
  log_step 9 "Cleanup"
  for kind in pipelines sources destinations; do
    LIST=$(api_call GET "/api/$kind" 2>/dev/null || echo '{}')
    IDS=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    for it in d.get('$kind', []):
        if it.get('name') in ('$SOURCE_NAME', '$DEST_NAME', '$PIPE_NAME'):
            print(it.get('id',''))
except Exception:
    pass
" "$LIST")
    for id in $IDS; do
      [ -n "$id" ] && api_call DELETE "/api/$kind/$id" >/dev/null 2>&1 || true
    done
  done
  az storage container delete --account-name "$STORAGE_ACCT" --name "$CONTAINER" \
    --auth-mode login --only-show-errors >/dev/null 2>&1 || true
  log_ok "Demo objects deleted"
fi

printf "\n${GREEN}╔══════════════════════════╗${NC}\n"
printf   "${GREEN}║  E2E demo completed      ║${NC}\n"
printf   "${GREEN}╚══════════════════════════╝${NC}\n\n"
printf "  Source container : %s (%d files)\n" "$CONTAINER" "$SAMPLE_COUNT"
printf "  Destination      : %s/%s @ %s\n" "$DB_NAME" "$VEC_CONTAINER" "$COSMOS_ACCT"
printf "  Pipeline         : %s\n" "$PIPE_ID"
if [ -n "$SEARCH_IP" ]; then printf "  Search service   : http://%s\n" "$SEARCH_IP"; fi
