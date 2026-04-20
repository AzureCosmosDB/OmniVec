#!/bin/sh
# scripts/azd-up.sh — OmniVec-hardened `azd up` wrapper.
#
# Runs `azd up` (or equivalent) with:
#   1. Preflight checks (quota, providers, name-collision) before any deploy
#   2. Background ARM deployment ticker (f3) for live resource-level progress
#   3. On-failure diagnostic dump (d2)
#   4. Timestamped log capture alongside for post-mortem
#
# Usage:
#   scripts/azd-up.sh                      # interactive
#   OMNIVEC_NONINTERACTIVE=1 ./azd-up.sh   # apply Quick-start defaults
#   scripts/azd-up.sh --preview             # azd provision what-if (no deploy)
#   scripts/azd-up.sh --skip-preflight      # only for debugging

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# Defaults (overridable via env).
: "${AZURE_LOCATION:=}"
: "${AZURE_ENV_NAME:=}"
: "${OMNIVEC_LOG_DIR:=$REPO_ROOT/.azd-logs}"
SKIP_PREFLIGHT=0
PREVIEW=0

# Parse args.
for _arg in "$@"; do
    case "$_arg" in
        --preview)        PREVIEW=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    esac
done

# Load shared libs.
# shellcheck source=../hooks/lib/heartbeat.sh
. "$REPO_ROOT/hooks/lib/heartbeat.sh"
# shellcheck source=../hooks/lib/preflight.sh
. "$REPO_ROOT/hooks/lib/preflight.sh"
# shellcheck source=../hooks/lib/retry.sh
. "$REPO_ROOT/hooks/lib/retry.sh"
# shellcheck source=../hooks/lib/deploy-ticker.sh
. "$REPO_ROOT/hooks/lib/deploy-ticker.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

mkdir -p "$OMNIVEC_LOG_DIR"
LOG_FILE="$OMNIVEC_LOG_DIR/azd-up-$(date +%Y%m%d-%H%M%S).log"

# Resolve env name / location from azd.
if [ -z "$AZURE_ENV_NAME" ]; then
    AZURE_ENV_NAME=$(azd env get-values </dev/null 2>/dev/null \
        | awk -F= '$1=="AZURE_ENV_NAME"{gsub(/"/,"",$2); print $2}' | tr -d '\r')
fi
if [ -z "$AZURE_LOCATION" ]; then
    AZURE_LOCATION=$(azd env get-values </dev/null 2>/dev/null \
        | awk -F= '$1=="AZURE_LOCATION"{gsub(/"/,"",$2); print $2}' | tr -d '\r')
fi
[ -z "$AZURE_ENV_NAME" ] && { printf "${RED}AZURE_ENV_NAME not set. Run 'azd env new <name>' first.${NC}\n" >&2; exit 1; }
[ -z "$AZURE_LOCATION" ] && AZURE_LOCATION="centralus"

RG_NAME="rg-omnivec-${AZURE_ENV_NAME}"

hb_log "OmniVec azd up wrapper starting (env=$AZURE_ENV_NAME, location=$AZURE_LOCATION)"
hb_log "Log file: $LOG_FILE"

# ── Preflight ──────────────────────────────────────────────────────────────
if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
    hb_step_start preflight
    {
        preflight_require_providers \
            Microsoft.ContainerService Microsoft.ContainerRegistry \
            Microsoft.OperationalInsights Microsoft.Storage \
            Microsoft.DocumentDB Microsoft.KeyVault Microsoft.Network \
            Microsoft.ServiceBus Microsoft.EventGrid Microsoft.Insights \
            || printf "  ${YELLOW}Provider registration incomplete; proceeding.${NC}\n"
        preflight_name_collisions "$RG_NAME" || exit 1
    } 2>&1 | tee -a "$LOG_FILE"
    hb_step_end preflight
else
    hb_log "[skipped preflight]"
fi

# ── Preview (what-if) ──────────────────────────────────────────────────────
if [ "$PREVIEW" -eq 1 ]; then
    hb_log "running 'azd provision --preview' (what-if)..."
    azd provision --preview 2>&1 | tee -a "$LOG_FILE"
    exit $?
fi

# ── Start deploy ticker in background ──────────────────────────────────────
deploy_ticker_start "$RG_NAME" "${OMNIVEC_TICKER_INTERVAL:-30}"
trap 'deploy_ticker_stop; exit 1' INT TERM
# shellcheck disable=SC2064
trap "deploy_ticker_stop" EXIT

# ── Run azd up with timestamped output ─────────────────────────────────────
hb_step_start azd_up
_ts_prefix() { while IFS= read -r _line; do printf '[%s] %s\n' "$(date +%H:%M:%S)" "$_line"; done; }
_rc=0
if azd up 2>&1 | _ts_prefix | tee -a "$LOG_FILE"; then
    _rc=0
else
    _rc=$?
fi
hb_step_end azd_up

deploy_ticker_stop

# ── Show deploy-ticker log tail (captures moments user may have missed) ────
if [ -f "$DEPLOY_TICKER_LOG" ] && [ -s "$DEPLOY_TICKER_LOG" ]; then
    printf "\n${CYAN}────── Deploy ticker tail ──────${NC}\n"
    tail -n 20 "$DEPLOY_TICKER_LOG"
fi

# ── On failure, dump diagnostics (d2) ──────────────────────────────────────
if [ "$_rc" -ne 0 ]; then
    printf "\n${RED}════ azd up FAILED (rc=%d) ════${NC}\n" "$_rc" >&2
    printf "${YELLOW}Gathering diagnostics into %s ...${NC}\n" "$LOG_FILE" >&2
    {
        printf '\n## Failure diagnostics\n\n'
        printf '### RG last deployment\n'
        az deployment group list --resource-group "$RG_NAME" \
            --query "[].{name:name,state:properties.provisioningState,ts:properties.timestamp}" \
            -o table </dev/null 2>&1 | head -10 || true
        printf '\n### Last failed deployment operations\n'
        _latest=$(az deployment group list --resource-group "$RG_NAME" \
            --query "sort_by([?properties.provisioningState=='Failed'], &properties.timestamp)[-1].name" \
            -o tsv </dev/null 2>/dev/null | tr -d '\r')
        if [ -n "$_latest" ]; then
            az deployment operation group list --resource-group "$RG_NAME" --name "$_latest" \
                --query "[?properties.provisioningState=='Failed'].{type:properties.targetResource.resourceType,name:properties.targetResource.resourceName,msg:properties.statusMessage.error.message}" \
                -o json </dev/null 2>&1 | head -100 || true
        fi
        hb_slowest_summary 2>&1 || true
    } >> "$LOG_FILE" 2>&1
    printf "${YELLOW}See %s for full diagnostics.${NC}\n" "$LOG_FILE" >&2
fi

exit "$_rc"
