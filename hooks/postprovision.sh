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
  if [ -n "$val" ]; then echo "$val"; return 0; fi
  # Fallback: read from azd env store
  val=$(azd env get-value "$key" 2>/dev/null || true)
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
BUILD_MODE=${BUILD_MODE:-acr}
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

if [ "$OMNIVEC_BUILD" = "true" ]; then
  # BUILD MODE: Build images from source
  printf "\n${YELLOW}Phase 1: Building images from source...${NC}\n"

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

  build_image "omnivec-api" "${ROOT_DIR}/api/Dockerfile" "$ROOT_DIR" "latest"
  build_image "omnivec-web" "${ROOT_DIR}/web/Dockerfile" "${ROOT_DIR}/web/" "latest"
  build_image "omnivec-changefeed" "${ROOT_DIR}/connectors/ingestion/dotnet/Dockerfile" "${ROOT_DIR}/connectors/ingestion/dotnet/" "latest"
  build_image "omnivec-dotnet-worker" "${ROOT_DIR}/connectors/worker/dotnet/Dockerfile" "${ROOT_DIR}/connectors/worker/dotnet/" "latest"

  if [ -d "${ROOT_DIR}/docgrok/pipeline-worker" ]; then
    build_image "docgrok-pipeline-worker" "${ROOT_DIR}/docgrok/pipeline-worker/Dockerfile" "${ROOT_DIR}/docgrok/pipeline-worker/" "latest"
  fi
  if [ -d "${ROOT_DIR}/docgrok/router" ]; then
    build_image "docgrok-router" "${ROOT_DIR}/docgrok/router/Dockerfile" "${ROOT_DIR}/docgrok/router/" "latest"
  fi

  printf "${GREEN}All images built and pushed.${NC}\n"
else
  # IMPORT MODE: Import pre-built images from shared registry (fast!)
  printf "\n${YELLOW}Phase 1: Importing pre-built images from shared registry...${NC}\n"
  printf "  ${CYAN}Source: $SHARED_REGISTRY${NC}\n"
  printf "  ${CYAN}To build from source instead: azd env set OMNIVEC_BUILD true${NC}\n"

  import_count=0
  skip_count=0

  # Import images in parallel for speed (each az acr import takes 30-120s)
  IMPORT_TMP=$(mktemp -d)
  import_pids=""

  for image in $IMAGES; do
    if [ "$FORCE_IMPORT" != "true" ] && image_exists "$image" "latest"; then
      printf "  ${GREEN}${image}:latest exists, skipping.${NC}\n"
      skip_count=$((skip_count + 1))
      continue
    fi

    printf "  ${CYAN}Importing ${image}:latest...${NC}\n"

    # Run import in background
    (
      import_success=false
      for attempt in 1 2; do
        import_error=$(az acr import \
            --name "$ACR_NAME" \
            --source "${SHARED_REGISTRY}/${image}:latest" \
            --image "${image}:latest" \
            --username "$SHARED_REGISTRY_USER" \
            --password "$SHARED_REGISTRY_TOKEN" \
            --force 2>&1) && import_success=true || import_success=false

        if [ "$import_success" = "true" ]; then break; fi
        if echo "$import_error" | grep -qi "unauthorized\|authentication\|401\|not found\|does not exist"; then break; fi
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

  # Wait for all imports to finish
  for pid in $import_pids; do
    wait "$pid" 2>/dev/null || true
  done

  # Report results
  for image in $IMAGES; do
    result_file="$IMPORT_TMP/$image"
    if [ ! -f "$result_file" ]; then continue; fi  # was skipped
    result=$(cat "$result_file")
    if [ "$result" = "OK" ]; then
      printf "  ${GREEN}${image}:latest imported.${NC}\n"
      import_count=$((import_count + 1))
    else
      printf "  ${RED}${image}:latest import FAILED${NC}\n"
      printf "  ${RED}Error: ${result}${NC}\n"
      if echo "$result" | grep -qi "unauthorized\|authentication\|401"; then
        printf "  ${RED}Hint: Token may be expired. Contact repo maintainer to regenerate.${NC}\n"
      elif echo "$result" | grep -qi "not found\|does not exist"; then
        printf "  ${RED}Hint: Image not found. Run: azd env set OMNIVEC_BUILD true${NC}\n"
      fi
    fi
  done
  rm -rf "$IMPORT_TMP"

  printf "${GREEN}Image import complete: $import_count imported, $skip_count skipped.${NC}\n"
fi

# =============================================================================
# PHASE 2: Get AKS credentials
# =============================================================================

printf "\n${YELLOW}Phase 2: Getting AKS credentials...${NC}\n"
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --overwrite-existing

printf "${GREEN}Connected to AKS cluster: ${AKS_CLUSTER}${NC}\n"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

printf "\n${YELLOW}Phase 3: Creating namespaces and secrets...${NC}\n"

# Create namespaces and label for Helm ownership
kubectl create namespace omnivec --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace docgrok --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite

# Storage connection string secret (only when blob source is enabled)
if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
  kubectl create secret generic omnivec-storage \
    --namespace omnivec \
    --from-literal=account-name="$STORAGE_ACCOUNT" \
    --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" \
    --dry-run=client -o yaml | kubectl apply -f -
  printf "  ${GREEN}omnivec-storage secret created.${NC}\n"
fi

printf "${GREEN}Namespaces and secrets created.${NC}\n"

# =============================================================================
# PHASE 4: Deploy with Helm
# =============================================================================

printf "\n${YELLOW}Phase 4: Deploying OmniVec via Helm...${NC}\n"

# Resolve helm chart dependencies (docgrok subchart)
printf "  ${CYAN}Resolving helm dependencies...${NC}\n"
helm dependency build "${ROOT_DIR}/helm/omnivec"

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

HELM_CMD="$HELM_CMD --wait --timeout 10m"

# Execute
eval $HELM_CMD

printf "${GREEN}Helm deployment complete.${NC}\n"

# =============================================================================
# PHASE 5: Verify and print info
# =============================================================================

printf "\n${YELLOW}Phase 5: Verifying deployment...${NC}\n"

printf "\n${CYAN}OmniVec pods:${NC}\n"
kubectl get pods -n omnivec --no-headers 2>/dev/null || true

printf "\n${CYAN}DocGrok pods:${NC}\n"
kubectl get pods -n docgrok --no-headers 2>/dev/null || true

# Wait for external IP
printf "\n${YELLOW}Waiting for external IP...${NC}\n"
EXTERNAL_IP=""
i=0
while [ $i -lt 30 ]; do
  EXTERNAL_IP=$(kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -n "$EXTERNAL_IP" ]; then
    break
  fi
  sleep 5
  i=$((i + 1))
done

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
