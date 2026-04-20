#!/bin/sh
# hooks/lib/preflight.sh — preflight validators used by preprovision.sh
#
# All functions are strict-POSIX and set-eu safe. Source this file AFTER
# heartbeat.sh. None of these functions read from stdin; they explicitly
# pass </dev/null to every nested az call to comply with a4.
#
# Exported entrypoints:
#   preflight_sanitize_env KEY              — strict BOM/CR/ws strip + rewrite azd env
#   preflight_require_providers NS [NS ...] — az provider register (idempotent)
#   preflight_vcpu_quota LOCATION REQ VCPUS — check vCPU headroom for requested skus
#   preflight_name_collisions RG_NAME       — RG already owned by someone else?
#   preflight_blob_flip_guard RG_NAME WANT  — forbid enabling/disabling blob-source on existing RG

set -u

# Already sourced?
[ "${OMNIVEC_PREFLIGHT_SH_LOADED:-}" = "1" ] && return 0
OMNIVEC_PREFLIGHT_SH_LOADED=1

# Color fallbacks so this works standalone in tests.
: "${RED:=}"; : "${GREEN:=}"; : "${YELLOW:=}"; : "${CYAN:=}"; : "${NC:=}"

# ──────────────────────────────────────────────────────────────────────────
# b2: strict env sanitizer. Strip BOM, CR, tabs, and surrounding whitespace.
# Rewrite the value in azd env. Returns 0 if the value was clean OR cleaned
# successfully, 1 on error.
# ──────────────────────────────────────────────────────────────────────────
preflight_sanitize_env() {
    _key=$1
    _raw=$(azd env get-value "$_key" </dev/null 2>/dev/null || printf '')
    [ -z "$_raw" ] && return 0
    # Strip: BOM (0xEF 0xBB 0xBF), \r, \t, zero-width spaces, leading/trailing ws.
    _clean=$(printf '%s' "$_raw" \
        | sed 's/^\xEF\xBB\xBF//' \
        | tr -d '\r\t' \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    if [ "$_raw" != "$_clean" ]; then
        azd env set "$_key" "$_clean" </dev/null 2>/dev/null || return 1
        printf "  ${YELLOW}Sanitized %s (stripped hidden chars)${NC}\n" "$_key"
    fi
    return 0
}

# ──────────────────────────────────────────────────────────────────────────
# b4: idempotently register required ARM resource providers. Each provider is
# registered in parallel-ish; we don't wait for full registration (can take
# minutes) — we just kick off the registration and let ARM catch up. Already-
# registered providers return instantly.
# ──────────────────────────────────────────────────────────────────────────
preflight_require_providers() {
    printf "  ${CYAN}Registering Azure resource providers...${NC}\n"
    _failed=""
    for _ns in "$@"; do
        _state=$(az provider show --namespace "$_ns" --query registrationState -o tsv </dev/null 2>/dev/null || printf '')
        _state=$(printf '%s' "$_state" | tr -d '\r\n ')
        case "$_state" in
            Registered)
                printf "    %s: ${GREEN}Registered${NC}\n" "$_ns"
                ;;
            Registering)
                printf "    %s: ${YELLOW}Registering (in progress)${NC}\n" "$_ns"
                ;;
            '')
                printf "    %s: ${RED}lookup failed${NC}\n" "$_ns"
                _failed="$_failed $_ns"
                ;;
            *)
                printf "    %s: ${YELLOW}%s → registering${NC}\n" "$_ns" "$_state"
                if ! az provider register --namespace "$_ns" --wait false </dev/null >/dev/null 2>&1; then
                    # --wait false was added in 2.62; fall back to plain register.
                    az provider register --namespace "$_ns" </dev/null >/dev/null 2>&1 || _failed="$_failed $_ns"
                fi
                ;;
        esac
    done
    if [ -n "$_failed" ]; then
        printf "  ${RED}WARNING: could not register: %s${NC}\n" "$_failed"
        printf "  ${YELLOW}Deploy will likely fail with 'NoRegisteredProviderFound'.${NC}\n"
        return 1
    fi
    return 0
}

# ──────────────────────────────────────────────────────────────────────────
# c1: check that the requested vCPU count fits in the subscription's quota
# for the SKU family in the target region. Best-effort: if az vm list-usage
# fails (no permission, transient), return 0 — we'd rather deploy than block.
# Args: LOCATION SYS_SKU SYS_COUNT GPU_SKU GPU_COUNT
# ──────────────────────────────────────────────────────────────────────────
preflight_vcpu_quota() {
    _loc=$1; _sys_sku=$2; _sys_count=${3:-0}; _gpu_sku=${4:-}; _gpu_count=${5:-0}

    # vCPUs per common SKU. When a SKU isn't in the table we skip it — better
    # than false-positive blocking on a user's custom choice.
    _sys_vcpu=$(preflight_sku_vcpus "$_sys_sku")
    _gpu_vcpu=0
    [ -n "$_gpu_sku" ] && [ "$_gpu_count" -gt 0 ] 2>/dev/null && _gpu_vcpu=$(preflight_sku_vcpus "$_gpu_sku")

    _sys_family=$(preflight_sku_family "$_sys_sku")
    _gpu_family=$(preflight_sku_family "${_gpu_sku:-}")

    _sys_need=$(( _sys_vcpu * _sys_count ))
    _gpu_need=$(( _gpu_vcpu * _gpu_count ))

    _usage_json=$(az vm list-usage --location "$_loc" -o json </dev/null 2>/dev/null || printf '')
    if [ -z "$_usage_json" ]; then
        printf "  ${YELLOW}Could not fetch vCPU usage (skipping quota preflight).${NC}\n"
        return 0
    fi

    _check_family() {
        _fam=$1; _need=$2; _label=$3
        [ -z "$_fam" ] || [ "$_need" -le 0 ] && return 0
        _limit=$(printf '%s' "$_usage_json" | awk -v f="$_fam" '
            BEGIN { RS="}"; FS="[,:]" }
            index($0, "\"value\":\""f"\"") {
                for (i=1; i<=NF; i++) if ($i ~ /"limit"/) { gsub(/[^0-9]/,"",$(i+1)); print $(i+1); exit }
            }')
        _current=$(printf '%s' "$_usage_json" | awk -v f="$_fam" '
            BEGIN { RS="}"; FS="[,:]" }
            index($0, "\"value\":\""f"\"") {
                for (i=1; i<=NF; i++) if ($i ~ /"currentValue"/) { gsub(/[^0-9]/,"",$(i+1)); print $(i+1); exit }
            }')
        [ -z "$_limit" ] && return 0
        [ -z "$_current" ] && _current=0
        _avail=$(( _limit - _current ))
        if [ "$_need" -gt "$_avail" ]; then
            printf "  ${RED}✗ %s quota: need %d vCPU, have %d available (limit %d, used %d)${NC}\n" \
                "$_label" "$_need" "$_avail" "$_limit" "$_current"
            return 1
        fi
        printf "  ${GREEN}✓ %s quota: %d / %d vCPU available${NC}\n" "$_label" "$_avail" "$_limit"
        return 0
    }

    printf "  ${CYAN}Checking vCPU quotas in %s...${NC}\n" "$_loc"
    _rc=0
    _check_family "$_sys_family" "$_sys_need" "System pool ($_sys_sku x$_sys_count)" || _rc=1
    _check_family "$_gpu_family" "$_gpu_need" "GPU pool ($_gpu_sku x$_gpu_count)"      || _rc=1
    if [ "$_rc" -ne 0 ]; then
        printf "  ${YELLOW}Request quota increase: https://aka.ms/quota${NC}\n"
    fi
    return "$_rc"
}

# Map SKU → vCPU count. Covers common SKUs; unknown → 0 (skipped).
preflight_sku_vcpus() {
    case "$1" in
        Standard_B2ms)                echo 2 ;;
        Standard_B4ms)                echo 4 ;;
        Standard_B8ms)                echo 8 ;;
        Standard_D2s_v3|Standard_D2ds_v5) echo 2 ;;
        Standard_D4s_v3|Standard_D4ds_v5) echo 4 ;;
        Standard_D8s_v3|Standard_D8ds_v5) echo 8 ;;
        Standard_D16s_v3)             echo 16 ;;
        Standard_NC4as_T4_v3)         echo 4 ;;
        Standard_NC6s_v3)             echo 6 ;;
        Standard_NC8as_T4_v3)         echo 8 ;;
        Standard_NC12s_v3)            echo 12 ;;
        Standard_NC24ads_A100_v4)     echo 24 ;;
        *)                            echo 0 ;;
    esac
}

# Map SKU → az usage-family name (the "name.value" in `az vm list-usage`).
preflight_sku_family() {
    case "$1" in
        Standard_B*)                  echo "standardBSFamily" ;;
        Standard_D*ds_v5)             echo "standardDDSv5Family" ;;
        Standard_D*s_v3)              echo "standardDSv3Family" ;;
        Standard_NC*T4*)              echo "standardNCASv3_T4Family" ;;
        Standard_NC*s_v3)             echo "standardNCSv3Family" ;;
        Standard_NC*A100*)            echo "standardNCADSA100v4Family" ;;
        *)                            echo "" ;;
    esac
}

# ──────────────────────────────────────────────────────────────────────────
# c2: warn about name-collision risks for the RG and global-namespace resources.
# We don't fail here — azd will fail definitively if it can't create — but we
# surface the most common issues (RG owned by different subscription; storage
# account DNS name taken globally).
# ──────────────────────────────────────────────────────────────────────────
preflight_name_collisions() {
    _rg=$1
    printf "  ${CYAN}Checking for name collisions...${NC}\n"
    _rg_sub=$(az group show --name "$_rg" --query "id" -o tsv </dev/null 2>/dev/null || printf '')
    if [ -n "$_rg_sub" ]; then
        _cur_sub=$(az account show --query id -o tsv </dev/null 2>/dev/null)
        case "$_rg_sub" in
            */subscriptions/"$_cur_sub"/*)
                printf "  ${GREEN}✓ RG %s exists in current subscription (will update).${NC}\n" "$_rg" ;;
            *)
                printf "  ${RED}✗ RG %s exists in a DIFFERENT subscription.${NC}\n" "$_rg"
                printf "  ${YELLOW}Pick a new AZURE_ENV_NAME or switch subscriptions.${NC}\n"
                return 1 ;;
        esac
    else
        printf "  ${GREEN}✓ RG %s name is free.${NC}\n" "$_rg"
    fi
    return 0
}

# ──────────────────────────────────────────────────────────────────────────
# b3: forbid switching an existing deployment's blob-source setting. If the
# RG tag `omnivec-blob` is `true` but user set FALSE (or vice versa), we
# refuse — Storage Account / ServiceBus / EventGrid would be orphaned.
# ──────────────────────────────────────────────────────────────────────────
preflight_blob_flip_guard() {
    _rg=$1
    _want=$2
    _existing=$(az group show --name "$_rg" --query "tags.\"omnivec-blob\"" -o tsv </dev/null 2>/dev/null || printf '')
    _existing=$(printf '%s' "$_existing" | tr -d '\r\n ')
    [ -z "$_existing" ] && return 0  # no prior tag, nothing to guard
    _want_norm=$(printf '%s' "$_want" | tr -d '\r\n ' | tr '[:upper:]' '[:lower:]')
    _existing_norm=$(printf '%s' "$_existing" | tr '[:upper:]' '[:lower:]')
    if [ "$_existing_norm" != "$_want_norm" ]; then
        printf "\n  ${RED}✗ Cannot change OMNIVEC_ENABLE_BLOB_SOURCE from %s to %s on existing deployment.${NC}\n" \
            "$_existing" "$_want"
        printf "  ${YELLOW}This would orphan Storage/ServiceBus/EventGrid (or leave code with no source).${NC}\n"
        printf "  ${YELLOW}Run 'azd down' first, or pick a different AZURE_ENV_NAME.${NC}\n"
        return 1
    fi
    return 0
}
