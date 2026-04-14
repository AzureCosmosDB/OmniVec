#!/bin/sh
# OmniVec Deployment Diagnostics (Linux/macOS)
# Comprehensive health check across infrastructure, pods, networking, auth,
# images, pipelines, models, and common failure modes.
#
# Usage:
#   ./scripts/diagnose.sh
#   ./scripts/diagnose.sh --env my-omnivec
#   ./scripts/diagnose.sh --server http://1.2.3.4 --token <token>

set +e  # Don't exit on errors — we handle them

# ── Parse arguments ──────────────────────────────────────────────────────────
ENV_NAME=""
SERVER_URL=""
ADMIN_TOKEN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --env)      ENV_NAME="$2"; shift 2 ;;
    --server)   SERVER_URL="$2"; shift 2 ;;
    --token)    ADMIN_TOKEN="$2"; shift 2 ;;
    *)          shift ;;
  esac
done

# ── Colors & helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS_COUNT=0; WARN_COUNT=0; FAIL_COUNT=0

pass()   { PASS_COUNT=$((PASS_COUNT + 1)); printf "  ${GREEN}✓ PASS${NC}  %s\n" "$1"; }
warn()   { WARN_COUNT=$((WARN_COUNT + 1)); printf "  ${YELLOW}⚠ WARN${NC}  %s\n" "$1"; [ -n "$2" ] && printf "          ${CYAN}Fix: %s${NC}\n" "$2"; }
fail()   { FAIL_COUNT=$((FAIL_COUNT + 1)); printf "  ${RED}✗ FAIL${NC}  %s\n" "$1"; [ -n "$2" ] && printf "          ${CYAN}Fix: %s${NC}\n" "$2"; }
header() { printf "\n${YELLOW}── %s ──${NC}\n" "$1"; }

api_get() {
  _path="$1"
  _headers=""
  [ -n "$ADMIN_TOKEN" ] && _headers="-H \"Authorization: Bearer $ADMIN_TOKEN\""
  eval curl -sf --max-time 15 $_headers "$SERVER_URL$_path" 2>/dev/null
}

# json field extraction without jq dependency
json_val() { echo "$1" | grep -o "\"$2\":[^,}]*" | head -1 | sed 's/.*://;s/"//g;s/ //g'; }
json_arr() { echo "$1" | grep -o "\"$2\":\[.*\]" | head -1; }

printf "\n${CYAN}╔══════════════════════════════════════════════════╗${NC}\n"
printf "${CYAN}║       OmniVec Deployment Diagnostics              ║${NC}\n"
printf "${CYAN}╚══════════════════════════════════════════════════╝${NC}\n"

# ── Resolve environment ──────────────────────────────────────────────────────
[ -z "$ENV_NAME" ] && ENV_NAME="$AZURE_ENV_NAME"
if [ -z "$ENV_NAME" ]; then
  _default=$(azd env list --output json 2>/dev/null | grep -o '"Name":"[^"]*","IsDefault":true' | head -1 | sed 's/.*"Name":"//;s/".*//')
  [ -n "$_default" ] && ENV_NAME="$_default"
fi

if [ -n "$ENV_NAME" ]; then
  printf "\n  Environment: ${CYAN}%s${NC}\n" "$ENV_NAME"
  azd env select "$ENV_NAME" 2>/dev/null
else
  printf "\n  ${YELLOW}No environment — using current kubectl context.${NC}\n"
fi

RG="rg-omnivec-$ENV_NAME"
KUBE_CONTEXT=""
AKS_NAME=""
ACR_NAME=""
COSMOS_NAME=""
IDENTITY_NAME=""

# ═════════════════════════════════════════════════════════════════════════════
# 1. INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

header "1. Infrastructure"

if [ -n "$ENV_NAME" ]; then
  _rg_exists=$(az group exists --name "$RG" 2>/dev/null | tr -d '\r\n ')
  if [ "$_rg_exists" = "true" ]; then
    pass "Resource group $RG exists"

    _resources=$(az resource list --resource-group "$RG" --query "[].{type:type,name:name}" -o tsv 2>/dev/null)

    AKS_NAME=$(echo "$_resources" | grep "containerService/managedClusters" | head -1 | awk '{print $2}')
    COSMOS_NAME=$(echo "$_resources" | grep "documentDB/databaseAccounts" | head -1 | awk '{print $2}')
    ACR_NAME=$(echo "$_resources" | grep "containerRegistry" | head -1 | awk '{print $2}')
    _KV_NAME=$(echo "$_resources" | grep "vaults" | head -1 | awk '{print $2}')
    _STOR_NAME=$(echo "$_resources" | grep "storageAccounts" | head -1 | awk '{print $2}')
    _SB_NAME=$(echo "$_resources" | grep "servicebus" | head -1 | awk '{print $2}')
    IDENTITY_NAME=$(echo "$_resources" | grep "userAssignedIdentities" | head -1 | awk '{print $2}')

    [ -n "$AKS_NAME" ]    && pass "AKS: $AKS_NAME"           || fail "No AKS cluster found" "azd up"
    [ -n "$COSMOS_NAME" ]  && pass "CosmosDB: $COSMOS_NAME"    || fail "No CosmosDB account found"
    [ -n "$ACR_NAME" ]     && pass "ACR: $ACR_NAME"            || fail "No Container Registry found"
    [ -n "$_KV_NAME" ]     && pass "Key Vault: $_KV_NAME"      || warn "No Key Vault found"
    [ -n "$_STOR_NAME" ]   && pass "Storage: $_STOR_NAME"      || warn "No Storage Account (blob source disabled?)"
    [ -n "$_SB_NAME" ]     && pass "Service Bus: $_SB_NAME"    || warn "No Service Bus (blob source disabled?)"
    [ -n "$IDENTITY_NAME" ] && pass "Managed Identity: $IDENTITY_NAME" || warn "No Managed Identity found"

    if [ -n "$AKS_NAME" ]; then
      az aks get-credentials --resource-group "$RG" --name "$AKS_NAME" --overwrite-existing 2>/dev/null
      KUBE_CONTEXT="$AKS_NAME"
    fi
  else
    fail "Resource group $RG does not exist" "azd up"
  fi
else
  warn "Skipping infrastructure checks (no environment name)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 2. POD HEALTH
# ═════════════════════════════════════════════════════════════════════════════

header "2. Pod Health"

if [ -n "$KUBE_CONTEXT" ]; then
  _pods=$(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec --no-headers 2>/dev/null | grep -v Terminating)
  if [ -n "$_pods" ]; then
    _total=$(echo "$_pods" | wc -l | tr -d ' ')
    _running=$(echo "$_pods" | grep -c "Running\|Completed" || true)

    if [ "$_running" -eq "$_total" ] && [ "$_total" -gt 0 ]; then
      pass "All $_total pods healthy"
    elif [ "$_total" -eq 0 ]; then
      fail "No pods found in omnivec namespace" "azd hooks run postprovision"
    else
      warn "$_running/$_total pods healthy"
    fi

    # Detect failures
    echo "$_pods" | while IFS= read -r line; do
      _pod=$(echo "$line" | awk '{print $1}')
      _status=$(echo "$line" | awk '{print $3}')
      _restarts=$(echo "$line" | awk '{print $4}')
      case "$_status" in
        ImagePullBackOff|ErrImagePull)
          fail "$_pod — $_status" "az acr repository list --name $ACR_NAME && azd hooks run postprovision" ;;
        CrashLoopBackOff)
          fail "$_pod — CrashLoopBackOff" "kubectl logs $_pod -n omnivec --tail=50 --previous" ;;
        Pending)
          fail "$_pod — Pending" "kubectl describe pod $_pod -n omnivec" ;;
        Error)
          fail "$_pod — Error" "kubectl logs $_pod -n omnivec --tail=50" ;;
      esac
      [ "$_restarts" -gt 5 ] 2>/dev/null && warn "$_pod — $_restarts restarts" "kubectl logs $_pod -n omnivec --tail=50 --previous"
    done

    # Expected deployments
    _deploys=$(kubectl --context "$KUBE_CONTEXT" get deployments -n omnivec --no-headers 2>/dev/null)
    for _d in omnivec-api omnivec-controller omnivec-web omnivec-cosmos-changefeed docgrok docgrok-controller; do
      _match=$(echo "$_deploys" | grep "^$_d ")
      if [ -n "$_match" ]; then
        _ready=$(echo "$_match" | awk '{print $2}' | cut -d/ -f1)
        _desired=$(echo "$_match" | awk '{print $2}' | cut -d/ -f2)
        if [ "$_desired" -eq 0 ] 2>/dev/null; then
          warn "$_d — scaled to 0" "kubectl scale deployment $_d -n omnivec --replicas=1"
        elif [ "$_ready" -lt "$_desired" ] 2>/dev/null; then
          warn "$_d — $_ready/$_desired ready"
        else
          pass "$_d — $_ready/$_desired ready"
        fi
      else
        fail "Deployment $_d not found" "azd hooks run postprovision"
      fi
    done
  else
    fail "Cannot list pods" "az aks get-credentials --resource-group $RG --name $AKS_NAME"
  fi
else
  warn "Skipping pod checks (no AKS context)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 3. HELM RELEASE
# ═════════════════════════════════════════════════════════════════════════════

header "3. Helm Release"

if [ -n "$KUBE_CONTEXT" ]; then
  _helm_json=$(helm status omnivec -n omnivec --kube-context "$KUBE_CONTEXT" -o json 2>/dev/null)
  if [ -n "$_helm_json" ]; then
    _helm_status=$(json_val "$_helm_json" "status")
    _helm_rev=$(json_val "$_helm_json" "version")
    case "$_helm_status" in
      deployed) pass "Helm release — deployed (revision $_helm_rev)" ;;
      pending*) fail "Helm release stuck in '$_helm_status'" "helm rollback omnivec -n omnivec --kube-context $KUBE_CONTEXT" ;;
      failed)   fail "Helm release failed" "helm rollback omnivec -n omnivec && azd hooks run postprovision" ;;
      *)        warn "Helm release status: $_helm_status" ;;
    esac
  else
    fail "No Helm release 'omnivec' found" "azd hooks run postprovision"
  fi
else
  warn "Skipping Helm checks (no AKS context)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 4. NETWORKING & DNS
# ═════════════════════════════════════════════════════════════════════════════

header "4. Networking & DNS"

if [ -n "$KUBE_CONTEXT" ]; then
  _ext_ip=$(kubectl --context "$KUBE_CONTEXT" get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
  if [ -n "$_ext_ip" ]; then
    pass "External IP: $_ext_ip"
    [ -z "$SERVER_URL" ] && SERVER_URL="http://$_ext_ip"
  else
    fail "No external IP on omnivec-web" "Wait 2-3 min or check NSG rules"
  fi
fi

if [ -n "$SERVER_URL" ]; then
  _health=$(curl -sf --max-time 10 "$SERVER_URL/health" 2>/dev/null)
  if [ -n "$_health" ]; then
    _hstatus=$(json_val "$_health" "status")
    if [ "$_hstatus" = "healthy" ]; then
      pass "API /health — healthy"
    else
      warn "API /health returned: $_hstatus"
    fi
  else
    fail "API unreachable at $SERVER_URL/health" "Check omnivec-api pods"
  fi
else
  warn "No server URL — skipping API checks"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 5. AUTH & RBAC
# ═════════════════════════════════════════════════════════════════════════════

header "5. Auth & RBAC"

[ -z "$ADMIN_TOKEN" ] && ADMIN_TOKEN=$(azd env get-value OMNIVEC_ADMIN_TOKEN 2>/dev/null | tr -d '\r\n')

if [ -n "$SERVER_URL" ] && [ -n "$ADMIN_TOKEN" ]; then
  _auth_resp=$(curl -sf --max-time 10 -H "Authorization: Bearer $ADMIN_TOKEN" "$SERVER_URL/health" 2>/dev/null)
  if [ -n "$_auth_resp" ]; then
    pass "Admin token accepted"
  else
    fail "Admin token rejected or API unreachable" "azd env get-value OMNIVEC_ADMIN_TOKEN"
  fi
elif [ -z "$ADMIN_TOKEN" ]; then
  warn "No admin token" "azd env get-value OMNIVEC_ADMIN_TOKEN"
fi

# Workload Identity
if [ -n "$KUBE_CONTEXT" ]; then
  _wi=$(kubectl --context "$KUBE_CONTEXT" get pods -n kube-system --no-headers 2>/dev/null | grep "azure-wi-webhook\|workload-identity")
  if echo "$_wi" | grep -q "Running"; then
    pass "Workload Identity webhook — running"
  elif [ -n "$_wi" ]; then
    fail "Workload Identity webhook — NOT running" "kubectl describe pods -n kube-system -l app.kubernetes.io/name=azure-workload-identity-webhook"
  else
    warn "Workload Identity webhook not found" "Check AKS OIDC/WI addon"
  fi
fi

# CosmosDB RBAC
if [ -n "$IDENTITY_NAME" ] && [ -n "$COSMOS_NAME" ] && [ -n "$RG" ]; then
  _principal=$(az identity show --name "$IDENTITY_NAME" --resource-group "$RG" --query "principalId" -o tsv 2>/dev/null)
  _cosmos_id=$(az cosmosdb show --name "$COSMOS_NAME" --resource-group "$RG" --query "id" -o tsv 2>/dev/null)
  if [ -n "$_principal" ] && [ -n "$_cosmos_id" ]; then
    _arm_roles=$(az role assignment list --assignee "$_principal" --scope "$_cosmos_id" --query "[].roleDefinitionName" -o tsv 2>/dev/null)
    if echo "$_arm_roles" | grep -qi "reader"; then
      pass "CosmosDB ARM RBAC — Account Reader assigned"
    else
      fail "CosmosDB ARM RBAC — missing Account Reader" "az role assignment create --assignee $_principal --role 'Cosmos DB Account Reader Role' --scope $_cosmos_id"
    fi

    _sql_roles=$(az cosmosdb sql role assignment list --account-name "$COSMOS_NAME" --resource-group "$RG" --query "[?principalId=='$_principal'].roleDefinitionId" -o tsv 2>/dev/null)
    if [ -n "$_sql_roles" ]; then
      pass "CosmosDB SQL RBAC — Data role assigned"
    else
      fail "CosmosDB SQL RBAC — no data role" "Grant 'Cosmos DB Built-in Data Contributor'"
    fi
  fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# 6. CONTAINER IMAGES
# ═════════════════════════════════════════════════════════════════════════════

header "6. Container Images"

if [ -n "$ACR_NAME" ]; then
  _repos=$(az acr repository list --name "$ACR_NAME" -o tsv 2>/dev/null)
  for _img in omnivec-api omnivec-web omnivec-changefeed omnivec-dotnet-worker docgrok-router docgrok-pipeline-worker; do
    if echo "$_repos" | grep -qx "$_img"; then
      pass "$_img — present"
    else
      fail "$_img — MISSING" "azd hooks run postprovision"
    fi
  done
else
  warn "Skipping image checks (no ACR)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 7. NODE CAPACITY
# ═════════════════════════════════════════════════════════════════════════════

header "7. Node Capacity"

if [ -n "$KUBE_CONTEXT" ]; then
  _nodes=$(kubectl --context "$KUBE_CONTEXT" get nodes --no-headers 2>/dev/null)
  if [ -n "$_nodes" ]; then
    _total_n=$(echo "$_nodes" | wc -l | tr -d ' ')
    _ready_n=$(echo "$_nodes" | grep -c " Ready " || true)
    [ "$_ready_n" -eq "$_total_n" ] && pass "All $_total_n nodes Ready" || warn "$_ready_n/$_total_n nodes Ready"

    # Resource pressure
    echo "$_nodes" | awk '{print $1}' | while read -r _node; do
      _conds=$(kubectl --context "$KUBE_CONTEXT" get node "$_node" -o jsonpath='{range .status.conditions[*]}{.type}={.status}{" "}{end}' 2>/dev/null)
      echo "$_conds" | grep -q "MemoryPressure=True" && fail "Node $_node — MemoryPressure" "Scale up node pool or VM SKU"
      echo "$_conds" | grep -q "DiskPressure=True"   && fail "Node $_node — DiskPressure"
      echo "$_conds" | grep -q "PIDPressure=True"    && fail "Node $_node — PIDPressure"
    done

    _pending=$(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec --field-selector=status.phase=Pending --no-headers 2>/dev/null | grep -v "^$")
    if [ -n "$_pending" ]; then
      _pc=$(echo "$_pending" | wc -l | tr -d ' ')
      fail "$_pc pods Pending — insufficient capacity" "az aks nodepool scale --resource-group $RG --cluster-name $AKS_NAME --name system --node-count 3"
    else
      pass "No pods pending due to capacity"
    fi
  fi
else
  warn "Skipping node checks (no AKS context)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 8. MODELS
# ═════════════════════════════════════════════════════════════════════════════

header "8. Models"

if [ -n "$SERVER_URL" ] && [ -n "$ADMIN_TOKEN" ]; then
  _models_raw=$(api_get "/api/docgrok/models")
  if [ -n "$_models_raw" ]; then
    # Parse model entries (simple grep-based)
    _model_names=$(echo "$_models_raw" | grep -o '"name":"[^"]*"' | sed 's/"name":"//;s/"//')
    _model_statuses=$(echo "$_models_raw" | grep -o '"status":"[^"]*"' | sed 's/"status":"//;s/"//')
    _model_kinds=$(echo "$_models_raw" | grep -o '"kind":"[^"]*"' | sed 's/"kind":"//;s/"//')
    _model_endpoints=$(echo "$_models_raw" | grep -o '"endpoint":"[^"]*"' | sed 's/"endpoint":"//;s/"//')

    if [ -z "$_model_names" ]; then
      warn "No embedding models registered" "Register via UI or CLI: omnivec model add ..."
    else
      _i=1
      echo "$_model_names" | while read -r _mn; do
        _ms=$(echo "$_model_statuses" | sed -n "${_i}p")
        _mk=$(echo "$_model_kinds" | sed -n "${_i}p")
        _me=$(echo "$_model_endpoints" | sed -n "${_i}p")
        case "$_ms" in
          available|running|healthy) pass "Model '$_mn' ($_mk) — $_ms" ;;
          stopped)                   warn "Model '$_mn' ($_mk) — stopped" "omnivec model start $_mn" ;;
          *)                         warn "Model '$_mn' ($_mk) — $_ms" ;;
        esac

        # Test external endpoint
        if [ "$_mk" = "external" ] && [ -n "$_me" ]; then
          _http_code=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 5 "$_me" 2>/dev/null)
          case "$_http_code" in
            2*|3*|401|403) pass "  Endpoint reachable: $_me" ;;
            *)             warn "  Endpoint unreachable: $_me (HTTP $_http_code)" "Verify URL and network" ;;
          esac
        fi
        _i=$((_i + 1))
      done
    fi
  else
    warn "Could not fetch models from API"
  fi
else
  warn "Skipping model checks (no API access)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 9. PIPELINES
# ═════════════════════════════════════════════════════════════════════════════

header "9. Pipelines"

if [ -n "$SERVER_URL" ] && [ -n "$ADMIN_TOKEN" ]; then
  _pip_raw=$(api_get "/api/pipelines")
  if [ -n "$_pip_raw" ]; then
    _pip_ids=$(echo "$_pip_raw" | grep -o '"id":"pip-[^"]*"' | sed 's/"id":"//;s/"//')
    _pip_names=$(echo "$_pip_raw" | grep -o '"name":"[^"]*"' | sed 's/"name":"//;s/"//')
    _pip_statuses=$(echo "$_pip_raw" | grep -o '"status":"[^"]*"' | sed 's/"status":"//;s/"//')

    if [ -z "$_pip_ids" ]; then
      warn "No pipelines configured" "Create via UI or CLI: omnivec pipeline create ..."
    else
      _i=1
      echo "$_pip_ids" | while read -r _pid; do
        _pn=$(echo "$_pip_names" | sed -n "${_i}p")
        _ps=$(echo "$_pip_statuses" | sed -n "${_i}p")
        case "$_ps" in
          active) pass "Pipeline '$_pn' ($_pid) — active" ;;
          paused) warn "Pipeline '$_pn' ($_pid) — paused" "omnivec pipeline resume $_pid" ;;
          error)  fail "Pipeline '$_pn' ($_pid) — error" "omnivec pipeline show $_pid" ;;
          *)      warn "Pipeline '$_pn' ($_pid) — $_ps" ;;
        esac
        _i=$((_i + 1))
      done
    fi
  else
    warn "Could not fetch pipelines from API"
  fi
else
  warn "Skipping pipeline checks (no API access)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 10. SERVICE BUS
# ═════════════════════════════════════════════════════════════════════════════

header "10. Service Bus"

if [ -n "$_SB_NAME" ] && [ -n "$RG" ]; then
  _queues=$(az servicebus queue list --namespace-name "$_SB_NAME" --resource-group "$RG" --query "[].{name:name,messageCount:messageCount}" -o tsv 2>/dev/null)
  if [ -n "$_queues" ]; then
    echo "$_queues" | while IFS=$'\t' read -r _qn _qc; do
      if [ "$_qc" -gt 1000 ] 2>/dev/null; then
        warn "Queue '$_qn' — $_qc messages backed up" "Scale workers: kubectl scale deployment omnivec-dotnet-worker -n omnivec --replicas=3"
      else
        pass "Queue '$_qn' — $_qc messages"
      fi
    done
  else
    warn "Could not query Service Bus queues"
  fi
else
  warn "Skipping Service Bus checks (not provisioned)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 11. RECENT ERRORS
# ═════════════════════════════════════════════════════════════════════════════

header "11. Recent Errors (last 200 log lines)"

if [ -n "$KUBE_CONTEXT" ]; then
  for _dep in omnivec-api omnivec-controller omnivec-cosmos-changefeed; do
    _pod=$(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l "app=$_dep" --no-headers 2>/dev/null | grep Running | head -1 | awk '{print $1}')
    if [ -n "$_pod" ]; then
      _errors=$(kubectl --context "$KUBE_CONTEXT" logs "$_pod" -n omnivec --tail=200 2>/dev/null | grep -i "ERROR\|Exception\|Traceback\|RBAC\|readMetadata\|Unauthorized\|forbidden\|connection refused" | tail -5)
      if [ -n "$_errors" ]; then
        warn "$_dep — recent errors:"
        echo "$_errors" | while read -r _eline; do
          printf "          %.140s\n" "$_eline"
        done
        echo "$_errors" | grep -qi "readMetadata" && printf "          ${CYAN}Fix: Grant 'Cosmos DB Account Reader Role' to managed identity${NC}\n"
        echo "$_errors" | grep -qi "Unauthorized" && printf "          ${CYAN}Fix: Check internal auth bypass for Host: omnivec-api${NC}\n"
      else
        pass "$_dep — no errors in recent logs"
      fi
    fi
  done
else
  warn "Skipping log checks (no AKS context)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

printf "\n${CYAN}══════════════════════════════════════════════════${NC}\n"
printf "${CYAN}  Summary: %d passed, %d warnings, %d failures${NC}\n" "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
  printf "${RED}  Issues found — review FAIL items above.${NC}\n"
elif [ "$WARN_COUNT" -gt 0 ]; then
  printf "${YELLOW}  Mostly healthy — review WARN items above.${NC}\n"
else
  printf "${GREEN}  All checks passed! Deployment is healthy.${NC}\n"
fi
echo ""
