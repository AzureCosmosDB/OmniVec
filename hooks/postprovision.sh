#!/bin/sh
# OmniVec — postprovision hook
# Pushes images to ACR, configures AKS, creates K8s secrets, deploys via Helm

set -eu

# Ensure tools installed by preprovision are on PATH (kubectl, helm, kubelogin)
export PATH="$HOME/.azure-kubectl:$HOME/.local/bin:$PATH"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

printf "${GREEN}╔══════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║    OmniVec — Post-provision Setup        ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════╝${NC}\n"

# ── Load azd environment values (handles both `azd up` and `azd hooks run`) ──

get_azd_value() {
  key=$1
  # First check env var (set during azd up flow)
  val=$(eval echo "\${$key:-}")
  val=$(printf '%s' "$val" | tr -d '\r')
  if [ -n "$val" ]; then echo "$val"; return 0; fi
  # Fallback: read from azd env store (use && to suppress stdout errors)
  val=$(azd env get-value "$key" 2>/dev/null) && val=$(printf '%s' "$val" | tr -d '\r') || val=""
  if [ -n "$val" ]; then echo "$val"; return 0; fi
  echo ""
}

INSTANCE_ID=$(get_azd_value "AZURE_OMNIVEC_INSTANCE_ID")
AKS_CLUSTER=$(get_azd_value "AZURE_AKS_CLUSTER_NAME")
ACR_LOGIN_SERVER=$(get_azd_value "AZURE_ACR_LOGIN_SERVER")
ACR_NAME=$(get_azd_value "AZURE_ACR_NAME")
COSMOS_ENDPOINT=$(get_azd_value "AZURE_COSMOS_ENDPOINT")
IDENTITY_CLIENT_ID=$(get_azd_value "AZURE_IDENTITY_CLIENT_ID")
RESOURCE_GROUP=$(get_azd_value "AZURE_RESOURCE_GROUP")
BUILD_MODE=$(get_azd_value "OMNIVEC_BUILD_MODE")
if [ -z "$BUILD_MODE" ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    BUILD_MODE="docker"
  else
    BUILD_MODE="acr"
  fi
fi
ENABLE_BLOB_SOURCE=$(get_azd_value "AZURE_ENABLE_BLOB_SOURCE")
ENABLE_BLOB_SOURCE=${ENABLE_BLOB_SOURCE:-false}

# Blob source outputs (only set when enableBlobSource=true)
STORAGE_ACCOUNT=$(get_azd_value "AZURE_STORAGE_ACCOUNT_NAME")
STORAGE_BLOB_ENDPOINT=$(get_azd_value "AZURE_STORAGE_BLOB_ENDPOINT")
STORAGE_QUEUE_ENDPOINT=$(get_azd_value "AZURE_STORAGE_QUEUE_ENDPOINT")
SB_ENDPOINT=$(get_azd_value "AZURE_SERVICEBUS_ENDPOINT")
KEYVAULT_URI=$(get_azd_value "AZURE_KEYVAULT_URI")

# Validate required vars
for var in INSTANCE_ID AKS_CLUSTER ACR_LOGIN_SERVER ACR_NAME COSMOS_ENDPOINT IDENTITY_CLIENT_ID RESOURCE_GROUP; do
  val=$(eval echo "\$$var")
  if [ -z "$val" ]; then
    printf "${RED}Missing required output: $var. Run 'azd provision' first.${NC}\n"
    exit 1
  fi
done

if [ "$ENABLE_BLOB_SOURCE" = "true" ] && [ -z "$SB_ENDPOINT" ]; then
  printf "${RED}Missing Service Bus endpoint while blob source is enabled.${NC}\n"
  exit 1
fi

printf "\n${CYAN}Configuration:${NC}\n"
echo "  Instance ID:     $INSTANCE_ID"
echo "  AKS cluster:    $AKS_CLUSTER"
echo "  ACR:             $ACR_LOGIN_SERVER"
echo "  CosmosDB:        $COSMOS_ENDPOINT"
echo "  Blob source:     $ENABLE_BLOB_SOURCE"
if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
  echo "  Storage:         $STORAGE_ACCOUNT"
  echo "  Service Bus:     $SB_ENDPOINT"
fi
echo "  Identity:        $IDENTITY_CLIENT_ID"
echo "  Build mode:      $BUILD_MODE"

# -- Store config as RG tags (enables cross-machine config sync) --
printf "\n${CYAN}Saving config to resource group tags...${NC}\n"
SYS_VM=$(get_azd_value "OMNIVEC_SYSTEM_NODE_VM_SIZE")
SYS_CNT=$(get_azd_value "OMNIVEC_SYSTEM_NODE_COUNT")
GPU_VM=$(get_azd_value "OMNIVEC_GPU_NODE_VM_SIZE")
GPU_CNT=$(get_azd_value "OMNIVEC_GPU_NODE_COUNT")
META=$(get_azd_value "OMNIVEC_METADATA_STORE")
BLOB=$(get_azd_value "OMNIVEC_ENABLE_BLOB_SOURCE")
BUILD=$(get_azd_value "OMNIVEC_BUILD_MODE")
az group update --name "$RESOURCE_GROUP" --tags \
    "omnivec-sys-sku=$SYS_VM" \
    "omnivec-sys-count=$SYS_CNT" \
    "omnivec-gpu-sku=$GPU_VM" \
    "omnivec-gpu-count=$GPU_CNT" \
    "omnivec-metadata=$META" \
    "omnivec-blob=$BLOB" \
    "omnivec-build=$BUILD" \
    "omnivec-instance=$INSTANCE_ID" >/dev/null 2>&1 || true
printf "  ${GREEN}Config saved to RG tags.${NC}\n"

# =============================================================================
# PHASE 1: Import or Build images
# =============================================================================

# Shared registry with pre-built images (pull via token)
SHARED_REGISTRY="omnivecregistry.azurecr.io"
SHARED_REGISTRY_USER="omnivec-pull-token"
SHARED_REGISTRY_TOKEN="${OMNIVEC_SHARED_REGISTRY_TOKEN:-$(get_azd_value "OMNIVEC_SHARED_REGISTRY_TOKEN")}"

# Check if we should build or import
OMNIVEC_BUILD=$(get_azd_value "OMNIVEC_BUILD")
OMNIVEC_BUILD=${OMNIVEC_BUILD:-false}
FORCE_IMPORT=${OMNIVEC_FORCE_IMPORT:-false}

# Images to import/build
IMAGES="omnivec-api omnivec-web omnivec-changefeed omnivec-dotnet-worker docgrok-pipeline-worker docgrok-router"

image_exists() {
  name=$1
  tag=$2
  existing=$(az acr repository show-tags --name "$ACR_NAME" --repository "$name" --query "[?@ == '$tag']" -o tsv 2>/dev/null || true)
  [ -n "$existing" ]
}

# Compare image digest between shared registry and local ACR — returns 0 if identical
image_up_to_date() {
  name=$1
  tag=$2
  # Get digest from local ACR
  local_digest=$(az acr manifest show-metadata --registry "$ACR_NAME" --name "${name}:${tag}" --query "digest" -o tsv 2>/dev/null || true)
  if [ -z "$local_digest" ]; then return 1; fi
  # Get digest from shared registry
  shared_digest=$(az acr manifest show-metadata --registry "omnivecregistry" --name "${name}:${tag}" --query "digest" -o tsv 2>/dev/null || true)
  if [ -z "$shared_digest" ]; then return 1; fi
  [ "$local_digest" = "$shared_digest" ]
}

# ── Helper: build a single image via docker or ACR ──────────────────────
build_image() {
  name=$1
  dockerfile=$2
  context=$3
  tag=${4:-latest}

  if [ "$FORCE_IMPORT" != "true" ] && image_exists "$name" "$tag"; then
    printf "  ${GREEN}${name}:${tag} exists, skipping.${NC}\n"
    return 0
  fi

  printf "  ${CYAN}Building ${name}:${tag}...${NC}\n"
  if [ "$BUILD_MODE" = "docker" ]; then
    docker build -t "${ACR_LOGIN_SERVER}/${name}:${tag}" -f "$dockerfile" "$context"
    docker push "${ACR_LOGIN_SERVER}/${name}:${tag}"
  else
    az acr build --registry "$ACR_NAME" --image "${name}:${tag}" --file "$dockerfile" "$context" --no-logs 2>/dev/null || \
    az acr build --registry "$ACR_NAME" --image "${name}:${tag}" --file "$dockerfile" "$context"
  fi
  printf "  ${GREEN}${name}:${tag} pushed.${NC}\n"
}

build_all_images() {
  build_image "omnivec-api" "${ROOT_DIR}/api/Dockerfile" "$ROOT_DIR" "latest"
  build_image "omnivec-web" "${ROOT_DIR}/web/Dockerfile" "${ROOT_DIR}/web/" "latest"
  build_image "omnivec-changefeed" "${ROOT_DIR}/connectors/ingestion/dotnet/Dockerfile" "${ROOT_DIR}/connectors/ingestion/dotnet/" "latest"
  build_image "omnivec-dotnet-worker" "${ROOT_DIR}/connectors/worker/dotnet/Dockerfile" "${ROOT_DIR}/connectors/worker/dotnet/" "latest"
  if [ -f "${ROOT_DIR}/docgrok/pipeline-worker/Dockerfile" ]; then
    build_image "docgrok-pipeline-worker" "${ROOT_DIR}/docgrok/pipeline-worker/Dockerfile" "${ROOT_DIR}/docgrok/pipeline-worker/" "latest"
  fi
  if [ -f "${ROOT_DIR}/docgrok/router/Dockerfile" ]; then
    build_image "docgrok-router" "${ROOT_DIR}/docgrok/router/Dockerfile" "${ROOT_DIR}/docgrok/router/" "latest"
  fi
}

build_missing_images() {
  for image in "$@"; do
    case "$image" in
      omnivec-api)              build_image "$image" "${ROOT_DIR}/api/Dockerfile" "$ROOT_DIR" "latest" ;;
      omnivec-web)              build_image "$image" "${ROOT_DIR}/web/Dockerfile" "${ROOT_DIR}/web/" "latest" ;;
      omnivec-changefeed)       build_image "$image" "${ROOT_DIR}/connectors/ingestion/dotnet/Dockerfile" "${ROOT_DIR}/connectors/ingestion/dotnet/" "latest" ;;
      omnivec-dotnet-worker)    build_image "$image" "${ROOT_DIR}/connectors/worker/dotnet/Dockerfile" "${ROOT_DIR}/connectors/worker/dotnet/" "latest" ;;
      docgrok-pipeline-worker)
        if [ -f "${ROOT_DIR}/docgrok/pipeline-worker/Dockerfile" ]; then
          build_image "$image" "${ROOT_DIR}/docgrok/pipeline-worker/Dockerfile" "${ROOT_DIR}/docgrok/pipeline-worker/" "latest"
        else
          printf "  ${YELLOW}Skipping ${image}: source not present in repo.${NC}\n"
        fi
        ;;
      docgrok-router)
        if [ -f "${ROOT_DIR}/docgrok/router/Dockerfile" ]; then
          build_image "$image" "${ROOT_DIR}/docgrok/router/Dockerfile" "${ROOT_DIR}/docgrok/router/" "latest"
        else
          printf "  ${YELLOW}Skipping ${image}: source not present in repo.${NC}\n"
        fi
        ;;
    esac
  done
}

# ── If not explicitly set to build, try import ──────────────────────────
if [ "$OMNIVEC_BUILD" != "true" ]; then
  printf "\n${YELLOW}Phase 1: Importing pre-built images from shared registry...${NC}\n"
  printf "  ${CYAN}Source: $SHARED_REGISTRY${NC}\n"
  if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
    printf "  ${CYAN}Using provided registry token for import.${NC}\n"
  else
    printf "  ${CYAN}No token provided — assuming public registry (anonymous pull).${NC}\n"
  fi
fi

if [ "$OMNIVEC_BUILD" = "true" ]; then
  # BUILD MODE: Build images from source
  printf "\n${YELLOW}Phase 1: Building images from source...${NC}\n"
  build_all_images
  printf "${GREEN}All images built and pushed.${NC}\n"
else
  # IMPORT MODE: Token validated, proceed with parallel imports
  import_count=0
  skip_count=0
  IMPORT_TMP=$(mktemp -d)
  import_pids=""

  for image in $IMAGES; do
    if [ "$FORCE_IMPORT" != "true" ] && image_up_to_date "$image" "latest"; then
      printf "  ${GREEN}${image}:latest up to date (digest match), skipping.${NC}\n"
      skip_count=$((skip_count + 1))
      continue
    elif [ "$FORCE_IMPORT" != "true" ] && image_exists "$image" "latest"; then
      printf "  ${CYAN}${image}:latest exists but digest differs, re-importing...${NC}\n"
    fi

    printf "  ${CYAN}Importing ${image}:latest...${NC}\n"

    # Run import in background (parallel)
    (
      import_success=false
      for attempt in 1 2; do
        AUTH_ARGS=""
        if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
          AUTH_ARGS="--username $SHARED_REGISTRY_USER --password $SHARED_REGISTRY_TOKEN"
        fi
        import_error=$(az acr import \
            --name "$ACR_NAME" \
            --source "${SHARED_REGISTRY}/${image}:latest" \
            --image "${image}:latest" \
            $AUTH_ARGS \
            --force 2>&1) && import_success=true || import_success=false

        if [ "$import_success" = "true" ]; then break; fi
        if echo "$import_error" | grep -qi "unauthorized\|authentication\|401\|not found\|does not exist\|InvalidHostName\|could not be resolved"; then break; fi
        if [ "$attempt" -lt 2 ]; then sleep 2; fi
      done

      if [ "$import_success" = "true" ]; then
        echo "OK" > "$IMPORT_TMP/$image"
      else
        echo "$import_error" > "$IMPORT_TMP/$image"
      fi
    ) &
    import_pids="$import_pids $!"
  done

  # Wait for all imports
  for pid in $import_pids; do
    wait "$pid"
  done

  # Report results
  for image in $IMAGES; do
    result_file="$IMPORT_TMP/$image"
    if [ ! -f "$result_file" ]; then continue; fi
    result=$(cat "$result_file")
    if [ "$result" = "OK" ]; then
      printf "  ${GREEN}${image}:latest imported.${NC}\n"
      import_count=$((import_count + 1))
    else
      printf "  ${RED}${image}:latest import FAILED${NC}\n"
      printf "  ${RED}${result}${NC}\n"
    fi
  done
  rm -rf "$IMPORT_TMP"

  printf "${GREEN}Image import complete: $import_count imported, $skip_count skipped.${NC}\n"

  # If no images available from import, auto-fallback to build mode
  total_available=$((import_count + skip_count))
  if [ "$total_available" -eq 0 ]; then
    printf "\n${YELLOW}Import provided no usable images. Falling back to source build mode...${NC}\n"
    BUILD_MODE=${BUILD_MODE:-acr}
    build_all_images
  fi
fi

# ── Final image check: verify all required images exist, build any missing ──
printf "\n${YELLOW}Verifying all required images exist in ACR...${NC}\n"
MISSING_IMAGES=""
for image in $IMAGES; do
  if ! image_exists "$image" "latest"; then
    printf "  ${RED}MISSING: ${image}:latest${NC}\n"
    MISSING_IMAGES="$MISSING_IMAGES $image"
  else
    printf "  ${GREEN}OK: ${image}:latest${NC}\n"
  fi
done

if [ -n "$MISSING_IMAGES" ]; then
  printf "\n${YELLOW}Building missing images from source...${NC}\n"
  # shellcheck disable=SC2086
  build_missing_images $MISSING_IMAGES

  STILL_MISSING=""
  for image in $MISSING_IMAGES; do
    if ! image_exists "$image" "latest"; then
      STILL_MISSING="$STILL_MISSING $image"
    fi
  done
  if [ -n "$STILL_MISSING" ]; then
    printf "\n${RED}ERROR: Required images are still missing after build attempt:${NC} $STILL_MISSING\n"
    printf "  Ensure docgrok submodule/source exists, then re-run: azd hooks run postprovision\n"
    exit 1
  fi
  printf "${GREEN}Missing images built and verified.${NC}\n"
else
  printf "${GREEN}All required images present in ACR.${NC}\n"
fi

# =============================================================================
# PHASE 2: Get AKS credentials
# =============================================================================

printf "\n${YELLOW}Phase 2: Getting AKS credentials...${NC}\n"
KUBE_CONTEXT="${AKS_CLUSTER}"
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --context "$KUBE_CONTEXT" \
  --overwrite-existing >/dev/null

# WSL: az writes kubeconfig to Windows home — symlink to Linux home for helm/kubectl
if [ ! -f "$HOME/.kube/config" ] && [ -f "/mnt/c/Users/$(whoami)/.kube/config" ] 2>/dev/null; then
  mkdir -p "$HOME/.kube"
  ln -sf "/mnt/c/Users/$(whoami)/.kube/config" "$HOME/.kube/config"
elif [ ! -f "$HOME/.kube/config" ]; then
  # Try to find Windows kubeconfig via USERPROFILE
  WIN_HOME=$(cmd.exe /C "echo %USERPROFILE%" 2>/dev/null | tr -d '\r' | sed 's|\\|/|g; s|^\([A-Z]\):|/mnt/\L\1|') || true
  if [ -n "$WIN_HOME" ] && [ -f "$WIN_HOME/.kube/config" ]; then
    mkdir -p "$HOME/.kube"
    ln -sf "$WIN_HOME/.kube/config" "$HOME/.kube/config"
  fi
fi

export KUBE_CONTEXT
kubectl --context "$KUBE_CONTEXT" get nodes >/dev/null
printf "${GREEN}Connected to AKS cluster: ${AKS_CLUSTER} (context: ${KUBE_CONTEXT})${NC}\n"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

printf "\n${YELLOW}Phase 3: Creating namespaces and secrets...${NC}\n"

# Create namespaces and label for Helm ownership
kubectl --context "$KUBE_CONTEXT" create namespace omnivec 2>/dev/null || true
kubectl --context "$KUBE_CONTEXT" create namespace docgrok 2>/dev/null || true
kubectl --context "$KUBE_CONTEXT" label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite
kubectl --context "$KUBE_CONTEXT" annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite

# Storage connection string secret (only when blob source is enabled)
if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
  kubectl --context "$KUBE_CONTEXT" create secret generic omnivec-storage \
    --namespace omnivec \
    --from-literal=account-name="$STORAGE_ACCOUNT" \
    --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" \
    --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
  printf "  ${GREEN}omnivec-storage secret created.${NC}\n"
fi

printf "${GREEN}Namespaces and secrets created.${NC}\n"

# =============================================================================
# PHASE 4: Deploy with Helm
# =============================================================================

printf "\n${YELLOW}Phase 4: Deploying OmniVec via Helm...${NC}\n"

# Resolve helm chart dependencies (docgrok subchart) — skip if already up to date
CHART_DIR="${ROOT_DIR}/helm/omnivec"
LOCK_FILE="${CHART_DIR}/Chart.lock"
LOCK_HASH_FILE="${CHART_DIR}/charts/.lock-hash"
CURRENT_HASH=""
if [ -f "$LOCK_FILE" ]; then
  CURRENT_HASH=$(sha256sum "$LOCK_FILE" 2>/dev/null | cut -d' ' -f1)
fi
CACHED_HASH=""
if [ -f "$LOCK_HASH_FILE" ]; then
  CACHED_HASH=$(cat "$LOCK_HASH_FILE" 2>/dev/null)
fi
if [ -n "$CURRENT_HASH" ] && [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
  printf "  ${GREEN}Helm dependencies up to date, skipping.${NC}\n"
else
  printf "  ${CYAN}Resolving helm dependencies...${NC}\n"
  helm dependency build "$CHART_DIR" 2>/dev/null
  if [ -n "$CURRENT_HASH" ]; then
    echo "$CURRENT_HASH" > "$LOCK_HASH_FILE"
  fi
fi

# Image tag used for all images built in Phase 1
# Generate admin token if not already set
ADMIN_TOKEN=$(get_azd_value "OMNIVEC_ADMIN_TOKEN")
if [ -z "$ADMIN_TOKEN" ]; then
  ADMIN_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 44)
  azd env set OMNIVEC_ADMIN_TOKEN "$ADMIN_TOKEN"
  printf "  ${GREEN}Generated new admin token.${NC}\n"
else
  printf "  ${GREEN}Using existing admin token.${NC}\n"
fi

IMAGE_TAG="latest"

# Build helm command (POSIX-compatible — no bash arrays)
HELM_CMD="helm upgrade --install omnivec ${ROOT_DIR}/helm/omnivec \
  --kube-context ${KUBE_CONTEXT} \
  --namespace omnivec \
  --set global.imageRegistry=${ACR_LOGIN_SERVER} \
  --set azure.workloadIdentity.clientId=${IDENTITY_CLIENT_ID} \
  --set azure.cosmos.endpoint=${COSMOS_ENDPOINT} \
  --set api.image.tag=${IMAGE_TAG} \
  --set controller.image.tag=${IMAGE_TAG} \
  --set web.image.tag=${IMAGE_TAG} \
  --set changefeed.image.tag=${IMAGE_TAG} \
  --set docgrok.global.imageRegistry=${ACR_LOGIN_SERVER} \
  --set docgrok.azure.workloadIdentity.clientId=${IDENTITY_CLIENT_ID} \
  --set docgrok.azure.cosmos.endpoint=${COSMOS_ENDPOINT} \
  --set docgrok.azure.cosmos.database=omnivec \
  --set docgrok.azure.cosmos.container=metadata \
  --set docgrok.docgrok.image.tag=${IMAGE_TAG} \
  --set api.adminToken=${ADMIN_TOKEN} \
  --set dotnetWorker.enabled=true \
  --set web.service.dnsLabel=${INSTANCE_ID}"

if [ -n "$KEYVAULT_URI" ]; then
  HELM_CMD="$HELM_CMD \
  --set azure.keyVault.uri=${KEYVAULT_URI}"
fi

if [ -n "$SB_ENDPOINT" ]; then
  HELM_CMD="$HELM_CMD \
  --set azure.serviceBus.namespace=${SB_ENDPOINT}"
fi

if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
  HELM_CMD="$HELM_CMD \
  --set azure.storage.accountName=${STORAGE_ACCOUNT} \
  --set azure.storage.blobEndpoint=${STORAGE_BLOB_ENDPOINT}"
fi

HELM_CMD="$HELM_CMD --wait --timeout 10m --atomic"

# Execute
set +e
eval $HELM_CMD
helm_rc=$?
set -e

if [ "$helm_rc" -ne 0 ]; then
  printf "${RED}Helm deploy failed. Collecting pod diagnostics...${NC}\n"
  kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -o wide || true
  kubectl --context "$KUBE_CONTEXT" get pods -n omnivec --no-headers 2>/dev/null | while read -r line; do
    pod=$(echo "$line" | awk '{print $1}')
    status=$(echo "$line" | awk '{print $3}')
    case "$status" in
      ImagePullBackOff|ErrImagePull|CrashLoopBackOff|Error|Pending)
        printf "\n${YELLOW}=== %s (%s) ===${NC}\n" "$pod" "$status"
        kubectl --context "$KUBE_CONTEXT" describe pod "$pod" -n omnivec | sed -n '/Events:/,$p' || true
        kubectl --context "$KUBE_CONTEXT" logs "$pod" -n omnivec --tail=80 || true
        ;;
    esac
  done
  exit "$helm_rc"
fi

printf "${GREEN}Helm deployment complete.${NC}\n"

# =============================================================================
# PHASE 5: Verify and print info
# =============================================================================

printf "\n${YELLOW}Phase 5: Verifying deployment...${NC}\n"

printf "\n${CYAN}OmniVec pods:${NC}\n"
kubectl --context "$KUBE_CONTEXT" get pods -n omnivec --no-headers 2>/dev/null || true

printf "\n${CYAN}DocGrok pods:${NC}\n"
kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l app=docgrok --no-headers 2>/dev/null || true
kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l app=docgrok-controller --no-headers 2>/dev/null || true

# Wait for external IP
printf "\n${YELLOW}Waiting for external IP...${NC}\n"
EXTERNAL_IP=""
i=0
while [ $i -lt 30 ]; do
  EXTERNAL_IP=$(kubectl --context "$KUBE_CONTEXT" get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -n "$EXTERNAL_IP" ]; then
    break
  fi
  sleep 5
  i=$((i + 1))
done

if ! kubectl --context "$KUBE_CONTEXT" rollout status deployment/omnivec-api -n omnivec --timeout=5m >/dev/null; then
  printf "${RED}API deployment did not become ready.${NC}\n"
  exit 1
fi

echo ""
printf "${GREEN}╔══════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║         Deployment Successful!           ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════╝${NC}\n"
echo ""
printf "  Instance ID:   ${CYAN}${INSTANCE_ID}${NC}\n"
printf "  Environment:   ${CYAN}${AZURE_ENV_NAME}${NC}\n"
printf "  AKS Cluster:   ${CYAN}${AKS_CLUSTER}${NC}\n"
printf "  ACR Registry:  ${CYAN}${ACR_LOGIN_SERVER}${NC}\n"
printf "  CosmosDB:      ${CYAN}${COSMOS_ENDPOINT}${NC}\n"

printf "  Admin Token:   ${CYAN}${ADMIN_TOKEN}${NC}\n"

LOCATION="${AZURE_LOCATION:-eastus2}"
FQDN="${INSTANCE_ID}.${LOCATION}.cloudapp.azure.com"

if [ -n "${EXTERNAL_IP}" ]; then
  echo ""
  printf "  OmniVec FQDN:  ${CYAN}http://${FQDN}/ui${NC}\n"
  printf "  OmniVec IP:    ${CYAN}http://${EXTERNAL_IP}/ui${NC}\n"
  printf "  Health Check:  ${CYAN}http://${FQDN}/health${NC}\n"
else
  echo ""
  printf "  ${YELLOW}External IP not yet assigned. Check with:${NC}\n"
  echo "  kubectl get svc omnivec-web -n omnivec"
fi
echo ""
