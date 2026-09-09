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
  [ "$_rc" -ne 0 ] && command -v hb_slowest_summary >/dev/null 2>&1 && hb_slowest_summary || true
  exit "$_rc"
}
trap '_postprov_exit' EXIT INT TERM

# Helper: read user input (handles non-TTY contexts)
read_input() {
  prompt="$1"
  _ri_val=""
  if [ -n "${OMNIVEC_NONINTERACTIVE:-}${AZD_NONINTERACTIVE:-}${CI:-}${GITHUB_ACTIONS:-}${OMNIVEC_FORCE_NO_TTY:-}" ]; then
    echo ""
    return
  fi
  # Always prefer /dev/tty — azd hooks have stdin piped from azd, so stdin
  # may be consumed by child processes (az cli, etc.) causing hangs.
  if [ -e /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
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
_post_lock="$_lock_dir/${AZURE_ENV_NAME:-omnivec}.post.lock.d"
if ! mkdir "$_post_lock" 2>/dev/null; then
  printf "${RED}Another postprovision hook owns %s. If interrupted, confirm its owner has stopped before removing this directory.${NC}\n" "$_post_lock" >&2
  exit 1
fi
printf '%s\n' "$$" > "$_post_lock/pid"
cleanup_post_lock() {
  _rc=$?
  rm -f "$_post_lock/helm-values.yaml"
  if [ -d "$_post_lock/imports" ]; then
    rm -f "$_post_lock/imports/"*
    rmdir "$_post_lock/imports"
  fi
  rm -f "$_post_lock/pid"
  rmdir "$_post_lock"
  [ "$_rc" -ne 0 ] && command -v hb_slowest_summary >/dev/null 2>&1 && hb_slowest_summary || true
  exit "$_rc"
}
trap cleanup_post_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
IMAGE_UPDATE_MARKER="$_lock_dir/${AZURE_ENV_NAME:-omnivec}.images-pending"
mark_image_update() {
  : > "$IMAGE_UPDATE_MARKER"
  IMAGES_CHANGED=true
}

run_bounded_import() {
  (
    _limit=${OMNIVEC_IMPORT_TIMEOUT_SEC:-900}
    case "$_limit" in ''|*[!0-9]*) printf 'Invalid image import timeout.\n' >&2; exit 125;; esac
    if [ "$_limit" -lt 1 ] || [ "$_limit" -gt 3600 ]; then
      printf 'Image import timeout must be 1-3600 seconds.\n' >&2
      exit 125
    fi
    _import_pid=""
    _watchdog_pid=""
    _cancel_import() {
      if [ -n "$_import_pid" ]; then
        kill -KILL "$_import_pid" 2>/dev/null || true
        wait "$_import_pid" 2>/dev/null || true
      fi
      if [ -n "$_watchdog_pid" ]; then
        kill -TERM "$_watchdog_pid" 2>/dev/null || true
        wait "$_watchdog_pid" 2>/dev/null || true
      fi
    }
    trap '_cancel_import' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    az acr import "$@" </dev/null &
    _import_pid=$!
    (
      trap - EXIT
      _sleep_pid=""
      _expired=0
      _stop_timer() {
        if [ -n "$_sleep_pid" ]; then
          kill -TERM "$_sleep_pid" 2>/dev/null || true
          wait "$_sleep_pid" 2>/dev/null || true
        fi
        exit "$_expired"
      }
      trap '_stop_timer' INT TERM
      sleep "$_limit" >/dev/null 2>&1 &
      _sleep_pid=$!
      if ! wait "$_sleep_pid"; then
        kill -KILL "$_import_pid" 2>/dev/null || true
        exit 125
      fi
      _sleep_pid=""
      _expired=124
      kill -TERM "$_import_pid" 2>/dev/null || true
      sleep 10 >/dev/null 2>&1 &
      _sleep_pid=$!
      wait "$_sleep_pid" || true
      _sleep_pid=""
      kill -KILL "$_import_pid" 2>/dev/null || true
      exit 124
    ) &
    _watchdog_pid=$!

    _rc=0
    wait "$_import_pid" || _rc=$?
    _import_pid=""
    kill -TERM "$_watchdog_pid" 2>/dev/null || true
    _timer_rc=0
    wait "$_watchdog_pid" || _timer_rc=$?
    _watchdog_pid=""
    case "$_timer_rc" in
      124|125) exit "$_timer_rc" ;;
    esac
    exit "$_rc"
  )
}

import_image() {
  _import_rc=0
  run_bounded_import "$@" || _import_rc=$?
  case "$_import_rc" in
    124|125|126|127)
      printf 'Image import deadline or command execution failed (exit %s); stopping without authentication fallback or source builds.\n' "$_import_rc" >&2
      exit "$_import_rc"
      ;;
  esac
  return "$_import_rc"
}

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
  # Fallback: read from azd env store. azd returns exit 0 even for missing
  # keys and prints "ERROR: key not found..." to stdout, so we must filter.
  val=$(azd env get-value "$key" < /dev/null 2>/dev/null) || val=""
  val=$(printf '%s' "$val" | tr -d '\r')
  case "$val" in
    ERROR*|*"not found"*) val="" ;;
  esac
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
SHAREPOINT_ENABLED=$(get_azd_value "OMNIVEC_SHAREPOINT_ENABLED")
SHAREPOINT_ENABLED=${SHAREPOINT_ENABLED:-false}
case "$SHAREPOINT_ENABLED" in
  true|false) ;;
  *) printf 'OMNIVEC_SHAREPOINT_ENABLED must be true or false.\n' >&2; exit 1 ;;
esac

# Azure rejects PublicIP DNS labels containing reserved trademarks
# (windows, microsoft, azure, xbox, login, bing, apple) with
# DomainNameLabelReserved (400). When the INSTANCE_ID happens to contain
# one of these, fall back to an empty label so the Helm chart skips the
# service.beta.kubernetes.io/azure-dns-label-name annotation and the LB
# is reachable via its public IP instead of an azurewebsites FQDN.
_lc_id=$(printf '%s' "$INSTANCE_ID" | tr '[:upper:]' '[:lower:]')
WEB_DNS_LABEL="$INSTANCE_ID"
for _w in microsoft windows azure xbox login bing apple; do
  case "$_lc_id" in
    *"$_w"*)
      WEB_DNS_LABEL=""
      DNS_LABEL_RESERVED="$_w"
      break
      ;;
  esac
done
unset _lc_id _w
if [ -z "$BUILD_MODE" ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    BUILD_MODE="docker"
  else
    BUILD_MODE="acr"
  fi
fi
# Blob source infrastructure (always provisioned).
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

if [ -z "$SB_ENDPOINT" ]; then
  printf "${RED}Missing Service Bus endpoint output. Run 'azd provision' first.${NC}\n"
  exit 1
fi

printf "\n${CYAN}Configuration:${NC}\n"
echo "  Instance ID:     $INSTANCE_ID"
echo "  AKS cluster:    $AKS_CLUSTER"
echo "  ACR:             $ACR_LOGIN_SERVER"
echo "  CosmosDB:        $COSMOS_ENDPOINT"
echo "  Storage:         $STORAGE_ACCOUNT"
echo "  Service Bus:     $SB_ENDPOINT"
echo "  Identity:        $IDENTITY_CLIENT_ID"
echo "  Build mode:      $BUILD_MODE"
if [ -n "${DNS_LABEL_RESERVED:-}" ]; then
  printf "  ${YELLOW}Note: instance id contains reserved word '${DNS_LABEL_RESERVED}'${NC}\n"
  printf "  ${YELLOW}      — Azure DNS label disabled; web LB will use IP only.${NC}\n"
fi

# -- Store config as RG tags (enables cross-machine config sync) --
printf "\n${CYAN}Saving config to resource group tags...${NC}\n"
SYS_VM=$(get_azd_value "OMNIVEC_SYSTEM_NODE_VM_SIZE")
SYS_CNT=$(get_azd_value "OMNIVEC_SYSTEM_NODE_COUNT")
GPU_VM=$(get_azd_value "OMNIVEC_GPU_NODE_VM_SIZE")
GPU_CNT=$(get_azd_value "OMNIVEC_GPU_NODE_COUNT")
META=$(get_azd_value "OMNIVEC_METADATA_STORE")
BUILD=$(get_azd_value "OMNIVEC_BUILD_MODE")
_RG_ID=$(az group show --name "$RESOURCE_GROUP" --query "id" -o tsv < /dev/null 2>/dev/null)
if az tag update --resource-id "$_RG_ID" --operation merge --tags \
    "omnivec-sys-sku=$SYS_VM" \
    "omnivec-sys-count=$SYS_CNT" \
    "omnivec-gpu-sku=$GPU_VM" \
    "omnivec-gpu-count=$GPU_CNT" \
    "omnivec-metadata=$META" \
    "omnivec-build=$BUILD" \
    "omnivec-instance=$INSTANCE_ID" </dev/null >/dev/null 2>&1; then
  printf "  ${GREEN}Config saved to RG tags.${NC}\n"
else
  printf "${YELLOW}Configuration was not saved to resource group tags; keep the local azd environment.${NC}\n" >&2
fi

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
IMAGES="omnivec-api omnivec-search omnivec-web omnivec-changefeed omnivec-dotnet-worker omnivec-onelake-iceberg-watcher omnivec-agent docgrok-pipeline-worker docgrok-router"

# Release channel tag (stable / dev / sha-xxxxxxx / vX.Y.Z / latest).
# Used for BOTH the acr import step AND the helm --set overrides so the
# imported tag matches what the pods try to pull.
# Resolution order:
#   1. Explicit OMNIVEC_IMAGE_TAG (env or azd) wins  — e.g. sha-xxxxxxx / vX.Y.Z
#   2. Auto-detect from current git branch:
#        dev  -> dev
#        main -> stable
#   3. Fallback -> stable
IMG_TAG=$(get_azd_value "OMNIVEC_IMAGE_TAG")
if [ -z "$IMG_TAG" ]; then
  _branch=$(git -C "$(dirname "$0")/.." rev-parse --abbrev-ref HEAD </dev/null 2>/dev/null || echo "")
  case "$_branch" in
    dev)  IMG_TAG=dev ;;
    main) IMG_TAG=stable ;;
    *)    IMG_TAG=stable ;;
  esac
  printf "${CYAN}Auto-detected branch '${_branch:-unknown}' -> image tag '${IMG_TAG}'${NC}\n" >&2
fi
# Validate IMG_TAG: must be a valid docker tag (alnum, dash, dot, underscore).
# Prevents garbage values (e.g. error messages) from being spliced into commands.
case "$IMG_TAG" in
  *[!A-Za-z0-9._-]*|'')
    printf "${RED}ERROR: OMNIVEC_IMAGE_TAG='%s' is not a valid image tag.${NC}\n" "$IMG_TAG" >&2
    printf "${RED}Fix: azd env set OMNIVEC_IMAGE_TAG stable (or 'dev')${NC}\n" >&2
    exit 1
    ;;
esac

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

  if [ "$OMNIVEC_BUILD" != "true" ] && [ "$FORCE_IMPORT" != "true" ] && image_exists "$name" "$tag"; then
    printf "  ${GREEN}${name}:${tag} exists, skipping.${NC}\n"
    return 0
  fi

  printf "  ${CYAN}Building ${name}:${tag}...${NC}\n"
  mark_image_update
  if [ "$BUILD_MODE" = "docker" ]; then
    if [ "${DOCKER_LOGGED_IN:-false}" != "true" ]; then
      az acr login --name "$ACR_NAME" </dev/null
      DOCKER_LOGGED_IN=true
    fi
    if ! docker build -t "${ACR_LOGIN_SERVER}/${name}:${tag}" -f "$dockerfile" "$context"; then
      printf "${RED}docker build failed for ${name}:${tag}${NC}\n"
      exit 1
    fi
    if ! docker push "${ACR_LOGIN_SERVER}/${name}:${tag}"; then
      printf "${RED}docker push failed for ${name}:${tag}${NC}\n"
      exit 1
    fi
  else
    if ! az acr build --registry "$ACR_NAME" --image "${name}:${tag}" --file "$dockerfile" "$context" --timeout 3600 </dev/null; then
      printf "${RED}az acr build failed for ${name}:${tag}. Inspect the ACR build logs before retrying.${NC}\n"
      exit 1
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
  build_image "omnivec-onelake-iceberg-watcher" "${ROOT_DIR}/connectors/ingestion/onelake_iceberg/Dockerfile" "${ROOT_DIR}/connectors/ingestion/onelake_iceberg/" "latest"
  build_image "omnivec-agent" "${ROOT_DIR}/agent/Dockerfile" "$ROOT_DIR" "latest"
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
      omnivec-onelake-iceberg-watcher) build_image "$image" "${ROOT_DIR}/connectors/ingestion/onelake_iceberg/Dockerfile" "${ROOT_DIR}/connectors/ingestion/onelake_iceberg/" "latest" ;;
      omnivec-agent)            build_image "$image" "${ROOT_DIR}/agent/Dockerfile" "$ROOT_DIR" "latest" ;;
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
[ -f "$IMAGE_UPDATE_MARKER" ] && IMAGES_CHANGED=true
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
    mark_image_update
    if import_image --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:${IMG_TAG}" --image "${FIRST_IMAGE}:latest" --force >/dev/null 2>&1; then
      printf " ${GREEN}✓ anonymous pull works${NC}\n"
      ANON_OK=true
      AUTH_IMPORTED=true
    else
      printf " ${YELLOW}✗ requires auth${NC}\n"
      # Try stored token
      if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
        printf "  ${CYAN}Trying stored token...${NC}"
        if import_image --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:${IMG_TAG}" --image "${FIRST_IMAGE}:latest" --username "$SHARED_REGISTRY_USER" --password "$SHARED_REGISTRY_TOKEN" --force >/dev/null 2>&1; then
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
        # Strip ALL whitespace (leading, trailing, and any embedded CR/LF/tabs/spaces from paste).
        # Valid ACR tokens are base64-ish and contain no whitespace.
        _new_token=$(printf '%s' "$_new_token" | tr -d '[:space:]')
        if [ -n "$_new_token" ]; then
          if import_image --name "$ACR_NAME" --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:${IMG_TAG}" --image "${FIRST_IMAGE}:latest" --username "$SHARED_REGISTRY_USER" --password "$_new_token" --force >/dev/null 2>&1; then
            SHARED_REGISTRY_TOKEN="$_new_token"
            azd env set OMNIVEC_SHARED_REGISTRY_TOKEN "$_new_token" </dev/null
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
  IMPORT_TMP="$_post_lock/imports"
  mkdir "$IMPORT_TMP"
  import_pids=""

  for image in $IMAGES; do
    if [ "$SKIP_IMPORT" = "true" ] || [ "$SKIP_IMPORT" = "1" ]; then
      if ! image_exists "$image" "latest"; then
        printf "${RED}OMNIVEC_SKIP_IMPORT is set but %s:latest is missing. Build/push it or disable skip-import.${NC}\n" "$image" >&2
        exit 1
      fi
      skip_count=$((skip_count + 1))
      continue
    fi
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

    printf "  ${CYAN}Importing ${image}:${IMG_TAG} as :latest...${NC}\n"
    mark_image_update

    # Run import in background (parallel)
    (
      import_success=false
      for attempt in 1 2; do
        AUTH_ARGS=""
        if [ -n "$SHARED_REGISTRY_TOKEN" ]; then
          AUTH_ARGS="--username $SHARED_REGISTRY_USER --password $SHARED_REGISTRY_TOKEN"
        fi
        import_error=$(import_image \
            --name "$ACR_NAME" \
            --source "${SHARED_REGISTRY}/${image}:${IMG_TAG}" \
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
  failed_jobs=0
  for pid in $import_pids; do
    if ! wait "$pid"; then failed_jobs=$((failed_jobs + 1)); fi
  done
  if [ "$failed_jobs" -gt 0 ]; then
    printf "${RED}An image import process failed; refusing to deploy an incomplete image set.${NC}\n" >&2
    exit 1
  fi

  # Report results
  failed_imports=0
  for image in $IMAGES; do
    result_file="$IMPORT_TMP/$image"
    if [ ! -f "$result_file" ]; then continue; fi
    result=$(cat "$result_file")
    if [ "$result" = "OK" ]; then
      printf "  ${GREEN}${image}:latest (from ${IMG_TAG}) imported.${NC}\n"
      import_count=$((import_count + 1))
    else
      printf "  ${RED}${image}:${IMG_TAG} import FAILED${NC}\n"
      printf "  ${RED}${result}${NC}\n"
      failed_imports=$((failed_imports + 1))
    fi
  done
  rm -rf "$IMPORT_TMP"
  if [ "$failed_imports" -gt 0 ]; then
    printf "${RED}Refusing a partial image update. Retry imports or set OMNIVEC_BUILD=true.${NC}\n" >&2
    exit 1
  fi

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
  IMAGES_CHANGED=true

  STILL_MISSING=""
  for image in $MISSING_IMAGES; do
    if ! image_exists "$image" "latest"; then
      STILL_MISSING="$STILL_MISSING $image"
    fi
  done
  if [ -n "$STILL_MISSING" ]; then
    printf "\n${RED}ERROR: Required images are still missing after build attempt:${NC} $STILL_MISSING\n"
    printf "  Ensure docgrok source exists in-repo, then re-run: azd hooks run postprovision\n"
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

# Commands use the isolated kubeconfig; never replace the user's default.
mkdir -p "$HOME/.kube"
chmod 600 "$OMNIVEC_KUBECONFIG" 2>/dev/null || true

export KUBE_CONTEXT
# Helper: always invoke kubectl against the freshly-fetched kubeconfig and context.
# Some azd/heartbeat wrappers reset $KUBECONFIG between hook phases, so we cannot
# rely solely on the env var.
kubectl_omnivec() {
  kubectl --kubeconfig "$OMNIVEC_KUBECONFIG" --context "$KUBE_CONTEXT" --request-timeout=30s "$@"  # stdin-ok: callers supply </dev/null
}

apply_kubernetes_resource() {
  _resource=$(kubectl_omnivec "$@" --dry-run=client -o yaml </dev/null) || return $?
  if [ -z "$_resource" ]; then
    printf 'kubectl produced an empty resource manifest.\n' >&2
    return 1
  fi
  printf '%s\n' "$_resource" | kubectl_omnivec apply -f -
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
apply_kubernetes_resource create namespace omnivec
apply_kubernetes_resource create namespace docgrok
kubectl_omnivec label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite </dev/null
kubectl_omnivec annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite </dev/null

# Storage connection string secret (always created — blob infra always provisioned).
apply_kubernetes_resource create secret generic omnivec-storage \
  --namespace omnivec \
  --from-literal=account-name="$STORAGE_ACCOUNT" \
  --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT"
printf "  ${GREEN}omnivec-storage secret created.${NC}\n"

# Agent internal token secret (used for agent <-> API service-to-service auth)
AGENT_INTERNAL_TOKEN=$(get_azd_value "OMNIVEC_AGENT_INTERNAL_TOKEN")
if [ -z "$AGENT_INTERNAL_TOKEN" ]; then
  AGENT_INTERNAL_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 44)
  azd env set OMNIVEC_AGENT_INTERNAL_TOKEN "$AGENT_INTERNAL_TOKEN" </dev/null
fi
apply_kubernetes_resource create secret generic omnivec-agent-internal \
  --namespace omnivec \
  --from-literal=token="$AGENT_INTERNAL_TOKEN"
printf "  ${GREEN}omnivec-agent-internal secret created.${NC}\n"

printf "${GREEN}Namespaces and secrets created.${NC}\n"

# =============================================================================
# PHASE 4: Deploy with Helm
# =============================================================================

printf "\n${YELLOW}Phase 4: Deploying OmniVec via Helm...${NC}\n"

# Chart.lock does not track edits to local subchart templates; repackage it.
CHART_DIR="${ROOT_DIR}/helm/omnivec"
helm dependency build "$CHART_DIR" --skip-refresh </dev/null

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
umask 077
HELM_VALUES_FILE="$_post_lock/helm-values.yaml"
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
    dnsLabel: "${WEB_DNS_LABEL}"
changefeed:
  image:
    tag: "${IMAGE_TAG}"
dotnetWorker:
  enabled: true
sharepointWatcher:
  enabled: ${SHAREPOINT_ENABLED}
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

  # Blob storage + ingestor are always provisioned (Option A: always-on infra).
  set -- "$@" --set "azure.storage.accountName=${STORAGE_ACCOUNT}" \
              --set "azure.storage.blobEndpoint=${STORAGE_BLOB_ENDPOINT}" \
              --set "blobIngestor.enabled=true"

  # Image tags are NOT overridden here — postprovision imports every image
  # into the env-specific ACR tagged :latest (regardless of source channel),
  # so the default values.yaml (image.tag: latest) resolves correctly for
  # every service, including any future one we forget to wire up.

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

# Detect stuck Helm release (pending-install / pending-upgrade from interrupted deploy)
set +e
_helm_status=$(helm status omnivec -n omnivec --kube-context "$KUBE_CONTEXT" --kubeconfig "$OMNIVEC_KUBECONFIG" -o json </dev/null 2>/dev/null)
_helm_phase=$(echo "$_helm_status" | grep -o '"status":"pending-[^"]*"' | head -1 | cut -d'"' -f4)
_helm_state=$(echo "$_helm_status" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
set -e
if [ -n "$_helm_phase" ]; then
  printf "${RED}Helm release is %s. Another deployment may still be active. Inspect helm status/history with kubeconfig '%s' and recover explicitly before retrying.${NC}\n" "$_helm_phase" "$OMNIVEC_KUBECONFIG" >&2
  exit 1
fi

# Leave ownership conflicts to Helm; never adopt unrelated namespace resources.

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
      printf '%s\n' "$KUBE_CONTEXT" "$OMNIVEC_KUBECONFIG" "$KEYVAULT_URI" "$APPINSIGHTS_CS" "$LOG_ANALYTICS_WS" "$SB_ENDPOINT" "$STORAGE_ACCOUNT" "$STORAGE_BLOB_ENDPOINT"
      find "$CHART_DIR" -type f ! -name '.last-deploy-fingerprint' 2>/dev/null \
        -exec sha256sum {} \; | LC_ALL=C sort
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
  _deployment_rows=$(kubectl_omnivec get deploy -n omnivec -o 'jsonpath={range .items[*]}{.metadata.generation}{" "}{.status.observedGeneration}{" "}{.spec.replicas}{" "}{.status.updatedReplicas}{" "}{.status.availableReplicas}{" "}{.status.replicas}{"\n"}{end}' </dev/null 2>/dev/null)
  _deploy_rc=$?
  set -e
  if [ "$_deploy_rc" -eq 0 ] && printf '%s\n' "$_deployment_rows" | awk 'NF != 6 || $2 < $1 || $3 != $4 || $3 != $5 || $3 != $6 {bad=1} END {exit (NR == 0 || bad)}'; then
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
  kubectl_omnivec rollout restart deployment -n omnivec </dev/null
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

if ! kubectl_omnivec rollout status deployment -n omnivec --timeout=5m --request-timeout=5m </dev/null; then
  printf "${RED}One or more deployments did not become ready.${NC}\n"
  exit 1
fi
if [ -n "$CURRENT_FP" ]; then
  echo "$CURRENT_FP" > "$FINGERPRINT_FILE"
fi
rm -f "$IMAGE_UPDATE_MARKER"

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

# Persist the OmniVec server URL into the azd env so subsequent CLI
# invocations / tooling can pick it up without re-deriving from the cluster.
OMNIVEC_API_URL="http://${FQDN}"
azd env set OMNIVEC_API_URL "$OMNIVEC_API_URL" </dev/null 2>/dev/null || true
azd env set OMNIVEC_UI_URL  "${OMNIVEC_API_URL}/ui" </dev/null 2>/dev/null || true
if [ -n "${EXTERNAL_IP}" ]; then
  azd env set OMNIVEC_API_IP "http://${EXTERNAL_IP}" </dev/null 2>/dev/null || true
fi

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
