#!/bin/sh
# tests/emu/run-azd-up.sh — drive the real hooks end-to-end against the emulator.
#
# Simulates the complete `azd up` lifecycle:
#   1. preprovision.sh   (collects inputs, sets azd env vars)
#   2. provision phase   (we seed the ARM resources Bicep would create)
#   3. postprovision.sh  (image import, AKS creds, helm deploy)
#
# Produces a state dir you can inspect afterwards:
#   $OMNIVEC_EMU_STATE/events.log                    (chronological calls)
#   $OMNIVEC_EMU_STATE/arm/...                       (Azure resources)
#   $OMNIVEC_EMU_STATE/k8s/ns/omnivec/...            (synthesised cluster state)
#   $OMNIVEC_EMU_STATE/helm/releases/omnivec         (helm release record)

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
EMU_BIN="$SCRIPT_DIR/bin"

# ── Emulator state (per-run unless caller exports OMNIVEC_EMU_STATE) ────────
: "${OMNIVEC_EMU_STATE:=$(mktemp -d -t omnivec-emu.XXXXXX)}"
export OMNIVEC_EMU_STATE

# ── Put emulated binaries at the front of PATH ─────────────────────────────
chmod +x "$EMU_BIN"/* 2>/dev/null || true
export PATH="$EMU_BIN:$PATH"

# ── Deterministic env for the hooks ─────────────────────────────────────────
export AZURE_ENV_NAME=emu-env
export AZURE_LOCATION=eastus2
export AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000
export HOME=$OMNIVEC_EMU_STATE/home
mkdir -p "$HOME"
export OMNIVEC_NONINTERACTIVE=1
export OMNIVEC_FORCE_NO_TTY=1

banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

banner "Emulator state: $OMNIVEC_EMU_STATE"
banner "PATH shims: $EMU_BIN"

# ── Phase 1: preprovision ───────────────────────────────────────────────────
banner "Phase 1: preprovision"
sh "$REPO_ROOT/hooks/preprovision.sh" </dev/null || {
    echo "preprovision.sh failed (rc=$?)" >&2
    exit 1
}

# ── Phase 2: simulated `azd provision` (Bicep would create these) ──────────
banner "Phase 2: provision (seeding ARM state)"

RG="${AZURE_ENV_NAME}-rg"
# Use azd-env values if set, else synthesise stable names the hooks expect.
ACR_NAME=$(azd env get-value AZURE_ACR_NAME 2>/dev/null)
[ -z "$ACR_NAME" ] && ACR_NAME="omnivecacremu1234"
AKS_NAME=$(azd env get-value AZURE_AKS_CLUSTER_NAME 2>/dev/null)
[ -z "$AKS_NAME" ] && AKS_NAME="omnivec-aks-emu1234"
INSTANCE_ID=$(azd env get-value AZURE_OMNIVEC_INSTANCE_ID 2>/dev/null)
[ -z "$INSTANCE_ID" ] && INSTANCE_ID="emu1234"

az group create --name "$RG" --location eastus2 >/dev/null

# Seed values into azd env that postprovision.sh reads
azd env set AZURE_RESOURCE_GROUP            "$RG"
azd env set AZURE_ACR_NAME                  "$ACR_NAME"
azd env set AZURE_ACR_LOGIN_SERVER          "${ACR_NAME}.azurecr.io"
azd env set AZURE_AKS_CLUSTER_NAME          "$AKS_NAME"
azd env set AZURE_OMNIVEC_INSTANCE_ID       "$INSTANCE_ID"
azd env set AZURE_COSMOS_ENDPOINT           "https://omnivec-cosmos-${INSTANCE_ID}.documents.azure.com:443/"
azd env set AZURE_IDENTITY_CLIENT_ID        "22222222-2222-2222-2222-222222222222"
azd env set AZURE_KEYVAULT_URI              "https://omnivec-kv-${INSTANCE_ID}.vault.azure.net/"
azd env set AZURE_STORAGE_ACCOUNT_NAME      "omnivecstg${INSTANCE_ID}"
azd env set AZURE_STORAGE_BLOB_ENDPOINT     "https://omnivecstg${INSTANCE_ID}.blob.core.windows.net/"
azd env set AZURE_STORAGE_QUEUE_ENDPOINT    "https://omnivecstg${INSTANCE_ID}.queue.core.windows.net/"
azd env set AZURE_SERVICEBUS_ENDPOINT       "omnivec-sb-${INSTANCE_ID}.servicebus.windows.net"
azd env set AZURE_APPINSIGHTS_CONNECTION_STRING "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://emu.applicationinsights.azure.com/"
azd env set AZURE_LOG_ANALYTICS_WORKSPACE_ID "00000000-0000-0000-0000-000000000000"
azd env set AZURE_ENABLE_BLOB_SOURCE        "true"
azd env set OMNIVEC_BUILD_MODE              "acr"

# ── Phase 3: postprovision ──────────────────────────────────────────────────
banner "Phase 3: postprovision"
sh "$REPO_ROOT/hooks/postprovision.sh" </dev/null
_rc=$?

banner "Done (rc=$_rc)"
echo "State dir: $OMNIVEC_EMU_STATE"
echo "Event log: $OMNIVEC_EMU_STATE/events.log"
exit $_rc
