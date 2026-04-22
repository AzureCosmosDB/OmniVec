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

# ── Source hardening libraries ──────────────────────────────────────────────
# shellcheck source=lib/heartbeat.sh
. "$SCRIPT_DIR/lib/heartbeat.sh" 2>/dev/null || true
# shellcheck source=lib/retry.sh
. "$SCRIPT_DIR/lib/retry.sh" 2>/dev/null || true

# Emit a slowest-step summary on any failure so the user sees where time went.
_postprov_exit() {
  _rc=$?
  [ "$_rc" -ne 0 ] && command -v hb_slowest_summary >/dev/null 2>&1 && hb_slowest_summary
  exit "$_rc"
}
trap '_postprov_exit' EXIT INT TERM

# Helper: read user input (handles non-TTY contexts)
read_input() {
  prompt="$1"
  _ri_val=""
  # Always prefer /dev/tty — azd hooks have stdin piped from azd, so stdin
  # may be consumed by child processes (az cli, etc.) causing hangs.
  if [ -e /dev/tty ]; then
    printf "%s" "$prompt" > /dev/tty
    read -r _ri_val < /dev/tty || true
  elif [ -t 0 ]; then
    printf "%s" "$prompt"
    read -r _ri_val || true
  else
    # No TTY at all — return empty (caller uses default)
    _ri_val=""
  fi
  echo "$_ri_val"
}

# -- Deployment lock: prevent concurrent postprovision runs --
_lock_dir="$HOME/.omnivec/locks"
mkdir -p "$_lock_dir"
_post_lock="$_lock_dir/${AZURE_ENV_NAME:-omnivec}.post.lock"
echo "$$" > "$_post_lock"
cleanup_post_lock() { rm -f "$_post_lock"; }
trap cleanup_post_lock EXIT

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
  val=$(azd env get-value "$key" < /dev/null 2>/dev/null) && val=$(printf '%s' "$val" | tr -d '\r') || val=""
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
APPINSIGHTS_CS=$(get_azd_value "AZURE_APPINSIGHTS_CONNECTION_STRING")
LOG_ANALYTICS_WS=$(get_azd_value "AZURE_LOG_ANALYTICS_WORKSPACE_ID")

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
_RG_ID=$(az group show --name "$RESOURCE_GROUP" --query "id" -o tsv < /dev/null 2>/dev/null)
az tag update --resource-id "$_RG_ID" --operation merge --tags \
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
IMAGES="omnivec-api omnivec-search omnivec-web omnivec-changefeed omnivec-dotnet-worker docgrok-pipeline-worker docgrok-router"

image_exists() {
  name=$1
  tag=$2
  existing=$(az acr repository show-tags --name "$ACR_NAME" --repository "$name" --query "[?@ == '$tag']" -o tsv </dev/null 2>/dev/null || true)
  [ -n "$existing" ]
}

# Compare image digest between shared registry and local ACR — returns 0 if identical
image_up_to_date() {
  name=$1
  tag=$2
  # Get digest from local ACR
  local_digest=$(az acr manifest show-metadata --registry "$ACR_NAME" --name "${name}:${tag}" --query "digest" -o tsv </dev/null 2>/dev/null || true)
  if [ -z "$local_digest" ]; then return 1; fi
  # Get digest from shared registry
  shared_digest=$(az acr manifest show-metadata --registry "omnivecregistry" --name "${name}:${tag}" --query "digest" -o tsv </dev/null 2>/dev/null || true)
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
    if ! docker build -t "${ACR_LOGIN_SERVER}/${name}:${tag}" -f "$dockerfile" "$context"; then
      printf "${RED}docker build failed for ${name}:${tag}${NC}\n"
      exit 1
    fi
    if ! docker push "${ACR_LOGIN_SERVER}/${name}:${tag}"; then
      printf "${RED}docker push failed for ${name}:${tag}${NC}\n"
      exit 1
    fi
  else
    if ! az acr build --registry "$ACR_NAME" --image "${name}:${tag}" --file "$dockerfile" "$context" --no-logs </dev/null 2>/dev/null; then
      if ! az acr build --registry "$ACR_NAME" --image "${name}:${tag}" --file "$dockerfile" "$context" </dev/null; then
        printf "${RED}az acr build failed for ${name}:${tag}${NC}\n"
        exit 1
      fi
    fi
  fi
  printf "  ${GREEN}${name}:${tag} pushed.${NC}\n"
}

build_all_images() {
  build_image "omnivec-api" "${ROOT_DIR}/api/Dockerfile" "$ROOT_DIR" "latest"
  build_image "omnivec-search" "${ROOT_DIR}/search/Dockerfile" "$ROOT_DIR" "latest"
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
      omnivec-search)           build_image "$image" "${ROOT_DIR}/search/Dockerfile" "$ROOT_DIR" "latest" ;;
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
ANON_OK=false
TOKEN_OK=false
AUTH_IMPORTED=false
IMAGES_CHANGED=false
FIRST_IMAGE=$(echo "$IMAGES" | awk '{print $1}')

# Honour OMNIVEC_SKIP_IMPORT — set this when you have locally built (or
# patched) images in ACR that you do NOT want overwritten by shared-registry
# copies on every azd up. Combined with image_up_to_date treating any local
# digest as authoritative (see below), this prevents regressions introduced
# by stale shared images clobbering a freshly built local image.
SKIP_IMPORT=${OMNIVEC_SKIP_IMPORT:-$(get_azd_value "OMNIVEC_SKIP_IMPORT")}

# Auth-test helper: skip the unconditional auth-test re-import when the
# first image already exists locally. Default policy is "prefer local" —
# re-import only if the user explicitly asked via OMNIVEC_FORCE_IMPORT.
_auth_test_can_skip() {
  if [ "$FORCE_IMPORT" = "true" ]; then return 1; fi
  if image_exists "$FIRST_IMAGE" "latest"; then return 0; fi
  return 1
}

if [ "$OMNIVEC_BUILD" != "true" ] && [ "$SKIP_IMPORT" != "true" ] && [ "$SKIP_IMPORT" != "1" ]; then
  printf "\n${YELLOW}Phase 1: Importing pre-built images from shared registry...${NC}\n"
  printf "  ${CYAN}Source: $SHARED_REGISTRY${NC}\n"

  # If the first image is already present and up-to-date locally, skip
  # the auth test entirely — importing unconditionally here is what used
  # to clobber locally patched images.
  if _auth_test_can_skip; then
    printf "  ${GREEN}${FIRST_IMAGE}:latest already present locally, skipping auth test.${NC}\n"
    ANON_OK=true
  else
    # Try anonymous pull first
    printf "  ${CYAN}Testing anonymous pull (this may take 30-60s)...${NC}"
    if timeout 90 az acr import --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:latest" --image "${FIRST_IMAGE}:latest" --force >/dev/null 2>&1; then
      printf " ${GREEN}✓ anonymous pull works${NC}\n"
      ANON_OK=true
      AUTH_IMPORTED=true
    else
      printf " ${YELLOW}✗ requires auth${NC}\n"
      # Try stored token
      if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
        printf "  ${CYAN}Trying stored token...${NC}"
        if az acr import --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:latest" --image "${FIRST_IMAGE}:latest" --username "$SHARED_REGISTRY_USER" --password "$SHARED_REGISTRY_TOKEN" --force >/dev/null 2>&1; then
          printf " ${GREEN}✓ token works${NC}\n"
          TOKEN_OK=true
          AUTH_IMPORTED=true
        else
          printf " ${RED}✗ token invalid/expired${NC}\n"
        fi
      fi
      # Prompt for token if nothing worked
      if [ "$TOKEN_OK" = "false" ]; then
        printf "  ${YELLOW}Registry token required for import.${NC}\n"
        _new_token=$(read_input "  Enter token for $SHARED_REGISTRY (or Enter to build from source): ")
        if [ -n "$_new_token" ]; then
          if az acr import --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:latest" --image "${FIRST_IMAGE}:latest" --username "$SHARED_REGISTRY_USER" --password "$_new_token" --force >/dev/null 2>&1; then
            SHARED_REGISTRY_TOKEN="$_new_token"
            azd env set OMNIVEC_SHARED_REGISTRY_TOKEN "$_new_token" </dev/null 2>/dev/null || true
            printf "  ${GREEN}Token valid — saved for future use.${NC}\n"
            TOKEN_OK=true
            AUTH_IMPORTED=true
          else
            printf "  ${RED}Token invalid. Will build from source.${NC}\n"
          fi
        fi
      fi
    fi
  fi
elif [ "$SKIP_IMPORT" = "true" ] || [ "$SKIP_IMPORT" = "1" ]; then
  printf "\n${YELLOW}Phase 1: Skipping image import (OMNIVEC_SKIP_IMPORT=true).${NC}\n"
  printf "  ${CYAN}Using images already present in $ACR_NAME.${NC}\n"
  # Treat as "imported" so we do not fall through to build-from-source.
  ANON_OK=true
fi

if [ "$OMNIVEC_BUILD" = "true" ] || { [ "$ANON_OK" = "false" ] && [ "$TOKEN_OK" = "false" ]; }; then
  # BUILD MODE: Build images from source
  printf "\n${YELLOW}Phase 1: Building images from source...${NC}\n"
  build_all_images
  IMAGES_CHANGED=true
  printf "${GREEN}All images built and pushed.${NC}\n"
else
  # IMPORT MODE: iterate every image. FIRST_IMAGE was handled by the auth
  # test above — count it as imported only if the auth test actually ran
  # an import (AUTH_IMPORTED=true); otherwise count it as a skip so
  # IMAGES_CHANGED stays false when nothing really changed.
  import_count=0
  skip_count=0
  IMPORT_TMP=$(mktemp -d)
  import_pids=""

  for image in $IMAGES; do
    # First image — already handled by auth test (imported or preserved)
    if [ "$image" = "$FIRST_IMAGE" ]; then
      if [ "$AUTH_IMPORTED" = "true" ]; then
        printf "  ${GREEN}${image}:latest already imported (auth test).${NC}\n"
        import_count=$((import_count + 1))
      else
        printf "  ${GREEN}${image}:latest already present locally, preserving (auth test).${NC}\n"
        skip_count=$((skip_count + 1))
      fi
      continue
    fi
    if [ "$FORCE_IMPORT" != "true" ] && image_exists "$image" "latest"; then
      # Default policy: local image wins. Re-import only when the user
      # explicitly sets OMNIVEC_FORCE_IMPORT=true. This prevents the
      # shared-registry :latest (which may lag behind hotfixes) from
      # clobbering locally built / patched images on every azd up.
      printf "  ${GREEN}${image}:latest already present locally, preserving (set OMNIVEC_FORCE_IMPORT=true to overwrite).${NC}\n"
      skip_count=$((skip_count + 1))
      continue
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
  if [ "$import_count" -gt 0 ]; then IMAGES_CHANGED=true; fi

  # If no images available from import, auto-fallback to build mode
  total_available=$((import_count + skip_count))
  if [ "$total_available" -eq 0 ]; then
    printf "\n${YELLOW}Import provided no usable images. Falling back to source build mode...${NC}\n"
    BUILD_MODE=${BUILD_MODE:-acr}
    build_all_images
    IMAGES_CHANGED=true
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

# Use a separate kubeconfig to avoid overwriting user's default context
OMNIVEC_KUBECONFIG="$HOME/.kube/omnivec-${AZURE_ENV_NAME:-omnivec}"
export KUBECONFIG="$OMNIVEC_KUBECONFIG"

if ! az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --file "$OMNIVEC_KUBECONFIG" \
  --overwrite-existing >/dev/null; then
  printf "${RED}Failed to fetch AKS credentials for cluster $AKS_CLUSTER${NC}\n"
  exit 1
fi

# Also materialize to the default kubeconfig path so kubectl finds the context
# even if $KUBECONFIG is reset by the outer runner (azd / heartbeat wrappers).
mkdir -p "$HOME/.kube"
if [ -L "$HOME/.kube/config" ]; then rm -f "$HOME/.kube/config"; fi
cp -f "$OMNIVEC_KUBECONFIG" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config" "$OMNIVEC_KUBECONFIG" 2>/dev/null || true

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
# Helper: always invoke kubectl against the freshly-fetched kubeconfig and context.
# Some azd/heartbeat wrappers reset $KUBECONFIG between hook phases, so we cannot
# rely solely on the env var.
kubectl_omnivec() {
  kubectl --kubeconfig "$OMNIVEC_KUBECONFIG" --context "$KUBE_CONTEXT" "$@"  # stdin-ok: callers supply </dev/null
}

# Sanity check before first kubectl call — surface config issues clearly.
if ! kubectl --kubeconfig "$OMNIVEC_KUBECONFIG" config get-contexts "$KUBE_CONTEXT" </dev/null >/dev/null 2>&1; then
  printf "${RED}Context '%s' not found in %s. Kubeconfig contents:${NC}\n" "$KUBE_CONTEXT" "$OMNIVEC_KUBECONFIG"
  kubectl --kubeconfig "$OMNIVEC_KUBECONFIG" config get-contexts </dev/null 2>&1 || true
  ls -la "$OMNIVEC_KUBECONFIG" "$HOME/.kube/config" 2>&1 || true
  exit 1
fi
kubectl_omnivec get nodes </dev/null >/dev/null
printf "${GREEN}Connected to AKS cluster: ${AKS_CLUSTER} (context: ${KUBE_CONTEXT})${NC}\n"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

printf "\n${YELLOW}Phase 3: Creating namespaces and secrets...${NC}\n"

# Create namespaces and label for Helm ownership
kubectl_omnivec create namespace omnivec </dev/null 2>/dev/null || true
kubectl_omnivec create namespace docgrok </dev/null 2>/dev/null || true
kubectl_omnivec label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite </dev/null
kubectl_omnivec annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite </dev/null

# Storage connection string secret (only when blob source is enabled)
if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
  kubectl_omnivec create secret generic omnivec-storage \
    --namespace omnivec \
    --from-literal=account-name="$STORAGE_ACCOUNT" \
    --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" \
    --dry-run=client -o yaml | kubectl_omnivec apply -f -
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
  if ! helm dependency build "$CHART_DIR" </dev/null; then
    printf "  ${RED}helm dependency build failed.${NC}\n"
    exit 1
  fi
  if [ -n "$CURRENT_HASH" ]; then
    echo "$CURRENT_HASH" > "$LOCK_HASH_FILE"
  fi
fi

# Image tag used for all images built in Phase 1
# Generate admin token if not already set
ADMIN_TOKEN=$(get_azd_value "OMNIVEC_ADMIN_TOKEN")
if [ -z "$ADMIN_TOKEN" ]; then
  ADMIN_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 44)
  azd env set OMNIVEC_ADMIN_TOKEN "$ADMIN_TOKEN" </dev/null
  printf "  ${GREEN}Generated new admin token.${NC}\n"
else
  printf "  ${GREEN}Using existing admin token.${NC}\n"
fi

# Generate search-service bootstrap + s2s tokens (distinct from admin token)
SEARCH_BOOTSTRAP_TOKEN=$(get_azd_value "OMNIVEC_SEARCH_TOKEN")
if [ -z "$SEARCH_BOOTSTRAP_TOKEN" ]; then
  SEARCH_BOOTSTRAP_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 44)
  azd env set OMNIVEC_SEARCH_TOKEN "$SEARCH_BOOTSTRAP_TOKEN" </dev/null
  printf "  ${GREEN}Generated new search bootstrap token.${NC}\n"
fi
SEARCH_INTERNAL_TOKEN=$(get_azd_value "SEARCH_INTERNAL_TOKEN")
if [ -z "$SEARCH_INTERNAL_TOKEN" ]; then
  SEARCH_INTERNAL_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 44)
  azd env set SEARCH_INTERNAL_TOKEN "$SEARCH_INTERNAL_TOKEN" </dev/null
  printf "  ${GREEN}Generated new search internal token.${NC}\n"
fi

IMAGE_TAG="latest"

# Write helm values to a temp file (avoids fragile eval + string concatenation)
HELM_VALUES_FILE=$(mktemp /tmp/omnivec-helm-values.XXXXXX.yaml)
cat > "$HELM_VALUES_FILE" <<EOF
global:
  imageRegistry: "${ACR_LOGIN_SERVER}"
azure:
  workloadIdentity:
    clientId: "${IDENTITY_CLIENT_ID}"
  cosmos:
    endpoint: "${COSMOS_ENDPOINT}"
api:
  image:
    tag: "${IMAGE_TAG}"
  adminToken: "${ADMIN_TOKEN}"
search:
  image:
    tag: "${IMAGE_TAG}"
  bootstrapToken: "${SEARCH_BOOTSTRAP_TOKEN}"
  internalToken: "${SEARCH_INTERNAL_TOKEN}"
controller:
  image:
    tag: "${IMAGE_TAG}"
web:
  image:
    tag: "${IMAGE_TAG}"
  service:
    dnsLabel: "${INSTANCE_ID}"
changefeed:
  image:
    tag: "${IMAGE_TAG}"
dotnetWorker:
  enabled: true
docgrok:
  global:
    imageRegistry: "${ACR_LOGIN_SERVER}"
  azure:
    workloadIdentity:
      clientId: "${IDENTITY_CLIENT_ID}"
    cosmos:
      endpoint: "${COSMOS_ENDPOINT}"
      database: "omnivec"
      container: "metadata"
  docgrok:
    image:
      tag: "${IMAGE_TAG}"
EOF

if [ -n "$KEYVAULT_URI" ]; then
  cat >> "$HELM_VALUES_FILE" <<EOF
  keyVault:
    uri: "${KEYVAULT_URI}"
EOF
fi

# Add optional values via yq-style append (plain echo since YAML is simple)
if [ -n "$SB_ENDPOINT" ]; then
  # Append under azure: (already exists in the file, so use separate --set for these)
  :
fi

# Build helm command as a proper argument list using a function
run_helm_deploy() {
  set -- helm upgrade --install omnivec "${ROOT_DIR}/helm/omnivec" \
    --kube-context "$KUBE_CONTEXT" --kubeconfig "$OMNIVEC_KUBECONFIG" \
    --namespace omnivec \
    --values "$HELM_VALUES_FILE"

  if [ -n "$KEYVAULT_URI" ]; then
    set -- "$@" --set "azure.keyVault.uri=${KEYVAULT_URI}"
  fi

  if [ -n "$APPINSIGHTS_CS" ]; then
    set -- "$@" --set "azure.appInsights.connectionString=${APPINSIGHTS_CS}"
  fi
  if [ -n "$LOG_ANALYTICS_WS" ]; then
    set -- "$@" --set "azure.appInsights.workspaceId=${LOG_ANALYTICS_WS}"
  fi

  if [ -n "$SB_ENDPOINT" ]; then
    set -- "$@" --set "azure.serviceBus.namespace=${SB_ENDPOINT}"
  fi

  if [ "$ENABLE_BLOB_SOURCE" = "true" ]; then
    set -- "$@" --set "azure.storage.accountName=${STORAGE_ACCOUNT}" \
                --set "azure.storage.blobEndpoint=${STORAGE_BLOB_ENDPOINT}" \
                --set "blobIngestor.enabled=true"
  else
    set -- "$@" --set "blobIngestor.enabled=false"
  fi

  # Image tag channel: stable (default, for testers) or dev (for active work).
  # Users select via: azd env set OMNIVEC_IMAGE_TAG dev
  IMG_TAG=$(get_azd_value "OMNIVEC_IMAGE_TAG")
  IMG_TAG=${IMG_TAG:-stable}
  set -- "$@" \
    --set "web.image.tag=${IMG_TAG}" \
    --set "api.image.tag=${IMG_TAG}" \
    --set "search.image.tag=${IMG_TAG}" \
    --set "controller.image.tag=${IMG_TAG}" \
    --set "changefeed.image.tag=${IMG_TAG}" \
    --set "blobEnumerator.image.tag=${IMG_TAG}" \
    --set "sourceWorker.image.tag=${IMG_TAG}" \
    --set "blobWatcher.image.tag=${IMG_TAG}" \
    --set "docgrok.docgrok.image.tag=${IMG_TAG}" \
    --set "docgrok.pipelineWorker.image.tag=${IMG_TAG}"

  # Intentionally NO --atomic: on failure, --atomic runs `helm uninstall`, which
  # strips the release metadata but can leave Deployments/Services behind (they
  # have finalizers or take time to delete). Next run sees "release not found"
  # + orphaned resources → fresh install conflicts on AlreadyExists → --atomic
  # times out → uninstall again → infinite loop. Without --atomic, a failed
  # upgrade just leaves a release in `status=failed` that the next upgrade can
  # retry cleanly.
  set -- "$@" --wait --timeout 10m

  "$@"
}

# Adopt orphaned resources when no helm release exists but the workload does.
# Caused by a previous --atomic timeout that wiped the release secret while
# the Deployments/Services persisted. We relabel them with Helm ownership so
# the next `helm install` takes them over instead of erroring on AlreadyExists.
adopt_orphaned_resources() {
  _existing=$(kubectl_omnivec get deploy -n omnivec -o name </dev/null 2>/dev/null | head -1)
  if [ -z "$_existing" ]; then
    return 0
  fi
  printf "${YELLOW}No Helm release found but resources exist in omnivec ns — adopting them for Helm ownership...${NC}\n"
  _adopt_kinds="deploy svc sa cm secret hpa ingress serviceaccount"
  # NOTE: exclude the auto-generated storage secret (omnivec-storage) — it is
  # created by postprovision itself and would conflict with helm-managed ones.
  for _kind in $_adopt_kinds; do
    kubectl_omnivec get "$_kind" -n omnivec -o name </dev/null 2>/dev/null | while read -r _res; do
      [ -z "$_res" ] && continue
      case "$_res" in
        secret/omnivec-storage|secret/sh.helm.*|secret/default-token-*) continue ;;
      esac
      kubectl_omnivec annotate "$_res" -n omnivec --overwrite \
        meta.helm.sh/release-name=omnivec \
        meta.helm.sh/release-namespace=omnivec </dev/null >/dev/null 2>&1 || true
      kubectl_omnivec label "$_res" -n omnivec --overwrite \
        app.kubernetes.io/managed-by=Helm </dev/null >/dev/null 2>&1 || true
    done
  done
  printf "${GREEN}Adoption annotations applied — helm install will take ownership.${NC}\n"
}

# Detect stuck Helm release (pending-install / pending-upgrade from interrupted deploy)
set +e
_helm_status=$(helm status omnivec -n omnivec --kube-context "$KUBE_CONTEXT" --kubeconfig "$OMNIVEC_KUBECONFIG" -o json </dev/null 2>/dev/null)
_helm_phase=$(echo "$_helm_status" | grep -o '"status":"pending-[^"]*"' | head -1 | cut -d'"' -f4)
_helm_state=$(echo "$_helm_status" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
set -e
if [ -n "$_helm_phase" ]; then
  printf "${YELLOW}Detected stuck Helm release (status: ${_helm_phase}). Rolling back...${NC}\n"
  set +e
  helm rollback omnivec -n omnivec --kube-context "$KUBE_CONTEXT" --kubeconfig "$OMNIVEC_KUBECONFIG" </dev/null 2>/dev/null
  _rb_rc=$?
  set -e
  if [ "$_rb_rc" -ne 0 ]; then
    printf "${YELLOW}Rollback failed — uninstalling stuck release...${NC}\n"
    helm uninstall omnivec -n omnivec --kube-context "$KUBE_CONTEXT" --kubeconfig "$OMNIVEC_KUBECONFIG" </dev/null 2>/dev/null || true
    _helm_state=""
  fi
  printf "${GREEN}Stuck release cleared. Proceeding with fresh deploy.${NC}\n"
fi

# If no release exists but resources do (orphaned from a prior --atomic
# uninstall), adopt them so `helm install` can take ownership cleanly.
if [ -z "$_helm_state" ]; then
  adopt_orphaned_resources
fi

# ── Skip helm upgrade if nothing has changed ────────────────────────────────
# Rationale: helm upgrade --install --wait takes 1-2 minutes even
# when the computed manifest is identical to the live one. We avoid that cost
# when:
#   1. the release is currently 'deployed' (healthy, not pending/failed),
#   2. no images were imported / rebuilt in this run (IMAGES_CHANGED != true),
#   3. the helm values fingerprint matches the one we cached after the last
#      successful deploy,
#   4. all deployments in the omnivec namespace report at least one available
#      replica (so we don't skip past a broken cluster).
# Set OMNIVEC_FORCE_HELM=true to bypass this optimisation.
FINGERPRINT_FILE="${CHART_DIR}/.last-deploy-fingerprint"
# Fingerprint captures everything that determines the rendered manifest:
#   - the helm values we're about to pass in
#   - every file under the chart directory (templates, values.yaml, Chart.yaml,
#     Chart.lock, built subcharts) — so a template edit invalidates the cache
#     even if images/values didn't change.
# Any failure to compute the fingerprint → empty string → skip never triggers.
CURRENT_FP=""
if [ -f "$HELM_VALUES_FILE" ] && [ -d "$CHART_DIR" ]; then
  CURRENT_FP=$(
    {
      sha256sum "$HELM_VALUES_FILE" 2>/dev/null | cut -d' ' -f1
      find "$CHART_DIR" -type f ! -name '.last-deploy-fingerprint' 2>/dev/null \
        | LC_ALL=C sort \
        | xargs -r sha256sum 2>/dev/null \
        | awk '{print $1}'
    } | sha256sum 2>/dev/null | cut -d' ' -f1
  )
fi
CACHED_FP=""
[ -f "$FINGERPRINT_FILE" ] && CACHED_FP=$(cat "$FINGERPRINT_FILE" 2>/dev/null || true)

SKIP_HELM=false
if [ "${OMNIVEC_FORCE_HELM:-}" != "true" ] \
   && [ "$IMAGES_CHANGED" != "true" ] \
   && [ "$_helm_state" = "deployed" ] \
   && [ -n "$CURRENT_FP" ] \
   && [ "$CURRENT_FP" = "$CACHED_FP" ]; then
  set +e
  _unavail=$(kubectl_omnivec get deploy -n omnivec -o jsonpath='{range .items[?(@.status.availableReplicas==0)]}{.metadata.name}{"\n"}{end}' </dev/null 2>/dev/null)
  _deploy_rc=$?
  set -e
  if [ "$_deploy_rc" -eq 0 ] && [ -z "$_unavail" ]; then
    SKIP_HELM=true
  fi
fi

if [ "$SKIP_HELM" = "true" ]; then
  printf "  ${GREEN}No image/config changes detected and cluster is healthy — skipping helm upgrade.${NC}\n"
  printf "  ${CYAN}(Set OMNIVEC_FORCE_HELM=true to force a redeploy.)${NC}\n"
  rm -f "$HELM_VALUES_FILE"
  helm_rc=0
else
  # Execute (d1: retry on transient ARM / Helm errors)
  # While helm waits (can take 1-3 minutes with --wait), show a
  # focused heartbeat so the user sees WHAT helm is blocked on. We surface:
  #   - deployments with ready != desired (the primary `helm --wait` target)
  #   - services of type LoadBalancer still waiting for external IP
  #   - the 5 most recent warning/error events
  # If everything is green, a single line tells the user helm itself is just
  # finalising (common: 15-30s post-ready wait).
  OMNIVEC_RETRY_HEARTBEAT_SEC=${OMNIVEC_HELM_HEARTBEAT_SEC:-20}
  OMNIVEC_RETRY_HEARTBEAT_CMD='
KC="kubectl --context '"$KUBE_CONTEXT"' --kubeconfig '"$OMNIVEC_KUBECONFIG"' -n omnivec"
_not_ready=$($KC get deploy -o "jsonpath={range .items[?(@.status.readyReplicas<@.spec.replicas)]}{.metadata.name}{\" \"}{.status.readyReplicas}{\"/\"}{.spec.replicas}{\"\n\"}{end}" 2>/dev/null | grep -v "^$")
_not_ready_all=$($KC get deploy -o "jsonpath={range .items[?(!@.status.readyReplicas)]}{.metadata.name}{\" 0/\"}{.spec.replicas}{\"\n\"}{end}" 2>/dev/null | grep -v "^$")
_pending_lb=$($KC get svc -o "jsonpath={range .items[?(@.spec.type==\"LoadBalancer\")]}{.metadata.name}{\" \"}{.status.loadBalancer.ingress[0].ip}{\"\n\"}{end}" 2>/dev/null | awk "/ \$/ {print \$1}")
_events=$($KC get events --sort-by=.lastTimestamp -o "jsonpath={range .items[?(@.type==\"Warning\")]}{.reason}{\": \"}{.message}{\"\n\"}{end}" 2>/dev/null | tail -5)
{
  if [ -n "$_not_ready$_not_ready_all" ]; then
    printf "    deployments not ready:\n"
    printf "%s\n" "$_not_ready" "$_not_ready_all" | grep -v "^$" | awk "{printf \"      %s\n\", \$0}"
  fi
  if [ -n "$_pending_lb" ]; then
    printf "    services waiting for external IP:\n"
    printf "%s\n" "$_pending_lb" | awk "{printf \"      %s\n\", \$0}"
  fi
  if [ -n "$_events" ]; then
    printf "    recent warnings (last 5):\n"
    printf "%s\n" "$_events" | awk "{printf \"      %s\n\", substr(\$0,1,120)}"
  fi
  if [ -z "$_not_ready$_not_ready_all$_pending_lb$_events" ]; then
    printf "    all resources ready — helm is finalising (wait, typically 15-30s)\n"
  fi
}'
  export OMNIVEC_RETRY_HEARTBEAT_SEC OMNIVEC_RETRY_HEARTBEAT_CMD
  set +e
  if command -v retry_run >/dev/null 2>&1; then
    retry_run "helm-deploy" -- run_helm_deploy
    helm_rc=$?
  else
    run_helm_deploy
    helm_rc=$?
  fi
  set -e
  unset OMNIVEC_RETRY_HEARTBEAT_CMD OMNIVEC_RETRY_HEARTBEAT_SEC

  # Clean up temp values file
  rm -f "$HELM_VALUES_FILE"

  # Cache fingerprint only on success so a failed run doesn't poison future skips
  if [ "$helm_rc" -eq 0 ] && [ -n "$CURRENT_FP" ]; then
    echo "$CURRENT_FP" > "$FINGERPRINT_FILE"
  fi
fi

if [ "$helm_rc" -ne 0 ]; then
  printf "${RED}Helm deploy failed. Collecting pod diagnostics...${NC}\n"
  kubectl_omnivec get pods -n omnivec -o wide </dev/null || true
  kubectl_omnivec get pods -n omnivec --no-headers </dev/null 2>/dev/null | while read -r line; do
    pod=$(echo "$line" | awk '{print $1}')
    status=$(echo "$line" | awk '{print $3}')
    case "$status" in
      ImagePullBackOff|ErrImagePull|CrashLoopBackOff|Error|Pending)
        printf "\n${YELLOW}=== %s (%s) ===${NC}\n" "$pod" "$status"
        # CRITICAL: inner kubectl calls need </dev/null inside while-read loop,
        # otherwise they consume the outer pipe's stdin and break the loop.
        kubectl_omnivec describe pod "$pod" -n omnivec </dev/null | sed -n '/Events:/,$p' || true
        kubectl_omnivec logs "$pod" -n omnivec --tail=80 </dev/null || true
        ;;
    esac
  done
  exit "$helm_rc"
fi

printf "${GREEN}Helm deployment complete.${NC}\n"

# Force pod restart if images were updated (tag is always 'latest', so Helm won't restart on its own)
if [ "$IMAGES_CHANGED" = "true" ]; then
  printf "\n${YELLOW}Images updated — restarting pods to pull new images...${NC}\n"
  kubectl_omnivec rollout restart deployment -n omnivec </dev/null 2>/dev/null || true
  kubectl_omnivec rollout status deployment/omnivec-api -n omnivec --timeout=5m </dev/null 2>/dev/null || true
  printf "${GREEN}Pods restarted with new images.${NC}\n"
fi

# =============================================================================
# PHASE 5: Verify and print info
# =============================================================================

printf "\n${YELLOW}Phase 5: Verifying deployment...${NC}\n"

printf "\n${CYAN}OmniVec pods:${NC}\n"
kubectl_omnivec get pods -n omnivec --no-headers </dev/null 2>/dev/null || true

printf "\n${CYAN}DocGrok pods:${NC}\n"
kubectl_omnivec get pods -n omnivec -l app=docgrok --no-headers </dev/null 2>/dev/null || true
kubectl_omnivec get pods -n omnivec -l app=docgrok-controller --no-headers </dev/null 2>/dev/null || true

# Wait for external IP
printf "\n${YELLOW}Waiting for external IP...${NC}\n"
EXTERNAL_IP=""
i=0
while [ $i -lt 30 ]; do
  EXTERNAL_IP=$(kubectl_omnivec get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' </dev/null 2>/dev/null || true)
  if [ -n "$EXTERNAL_IP" ]; then
    break
  fi
  sleep 5
  i=$((i + 1))
done

if ! kubectl_omnivec rollout status deployment/omnivec-api -n omnivec --timeout=5m >/dev/null; then
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

LOCATION="${AZURE_LOCATION:-}"
if [ -z "$LOCATION" ]; then LOCATION=$(get_azd_value "AZURE_LOCATION"); fi
if [ -z "$LOCATION" ]; then LOCATION="eastus2"; fi
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
