#!/bin/sh
# scripts/doctor.sh — OmniVec environment diagnostic tool.
#
# Run BEFORE `azd up` to surface problems early, or AFTER a failure to
# diagnose what went wrong. Prints PASS/WARN/FAIL lines for each check.
#
# Exits 0 if no FAIL, 1 otherwise. WARN items do not fail the run.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

FAILS=0
WARNS=0
PASSES=0
pass() { PASSES=$((PASSES+1)); printf "  ${GREEN}✓${NC} %s\n" "$1"; }
warn() { WARNS=$((WARNS+1));   printf "  ${YELLOW}!${NC} %s\n" "$1"; [ -n "${2:-}" ] && printf "     %s\n" "$2"; }
fail() { FAILS=$((FAILS+1));   printf "  ${RED}✗${NC} %s\n" "$1"; [ -n "${2:-}" ] && printf "     %s\n" "$2"; }

printf "${CYAN}═══ OmniVec Doctor ═══${NC}\n"

# 1. Required tools ---------------------------------------------------------
printf "\n${CYAN}Tools:${NC}\n"
for _t in az azd kubectl helm git curl; do
    if command -v "$_t" >/dev/null 2>&1; then
        _ver=$("$_t" --version 2>/dev/null | head -1 | cut -c1-70)
        pass "$_t: ${_ver:-installed}"
    else
        case "$_t" in
            kubectl|helm) warn "$_t not on PATH (will be auto-installed by hooks)" ;;
            *)            fail "$_t missing"  "install from vendor docs and rerun" ;;
        esac
    fi
done

# 2. Azure login / subscription --------------------------------------------
printf "\n${CYAN}Azure:${NC}\n"
if _acct=$(az account show </dev/null 2>/dev/null); then
    _sub=$(printf '%s' "$_acct" | awk -F'"' '/"name"/ {print $4; exit}')
    _id=$(printf  '%s' "$_acct" | awk -F'"' '/"id"/   {print $4; exit}')
    pass "logged in: $_sub ($_id)"
else
    fail "not logged in" "run: az login"
fi

# 3. azd env ----------------------------------------------------------------
printf "\n${CYAN}azd environment:${NC}\n"
_envs=$(azd env list -o json </dev/null 2>/dev/null | tr -d '\r' || printf '')
if [ -z "$_envs" ] || [ "$_envs" = "[]" ]; then
    warn "no azd env found" "run: azd env new <name>"
else
    _cur=$(azd env list -o json </dev/null 2>/dev/null | awk -F'"' '/"IsDefault": true/{found=1} found && /"Name"/{print $4; exit}' || printf '')
    [ -n "$_cur" ] && pass "current env: $_cur" || warn "no default env selected"
fi

# 4. Preset env vars --------------------------------------------------------
printf "\n${CYAN}OmniVec config:${NC}\n"
_missing=""
for _k in AZURE_LOCATION AZURE_ENV_NAME; do
    _v=$(azd env get-value "$_k" </dev/null 2>/dev/null | tr -d '\r' || printf '')
    [ -z "$_v" ] && _missing="$_missing $_k"
done
[ -n "$_missing" ] && warn "not set:$_missing" "azd will prompt or use defaults"

# Non-interactive mode detection
_ni_active=""
for _v in OMNIVEC_NONINTERACTIVE AZD_NONINTERACTIVE CI GITHUB_ACTIONS; do
    eval "_val=\${$_v:-}"
    [ -n "${_val:-}" ] && _ni_active="$_ni_active $_v=${_val}"
done
if [ -n "$_ni_active" ]; then
    pass "non-interactive mode:${_ni_active}"
fi

# 5. TTY / stdin ------------------------------------------------------------
printf "\n${CYAN}Terminal:${NC}\n"
if [ -e /dev/tty ] && ( : >/dev/tty ) 2>/dev/null && ( : </dev/tty ) 2>/dev/null; then
    pass "/dev/tty is usable (interactive prompts will work)"
elif [ -t 0 ]; then
    pass "stdin is a TTY"
else
    if [ -n "$_ni_active" ]; then
        pass "no TTY but non-interactive mode is set"
    else
        warn "no TTY available" "set OMNIVEC_NONINTERACTIVE=1 or pre-set config via azd env set"
    fi
fi

# 6. Bicep CLI --------------------------------------------------------------
printf "\n${CYAN}Bicep:${NC}\n"
if command -v bicep >/dev/null 2>&1; then
    pass "bicep on PATH: $(bicep --version 2>/dev/null | head -1)"
elif [ -x "$HOME/.azure/bin/bicep" ] || [ -x "$HOME/.azure/bin/bicep.exe" ]; then
    pass "bicep installed via az (~/.azure/bin/bicep)"
else
    warn "bicep not installed" "run: az bicep install"
fi

# 7. RG status --------------------------------------------------------------
_env=$(azd env get-value AZURE_ENV_NAME </dev/null 2>/dev/null | tr -d '\r' || printf '')
if [ -n "$_env" ]; then
    _rg="rg-omnivec-$_env"
    printf "\n${CYAN}Resource group (%s):${NC}\n" "$_rg"
    _exists=$(az group exists --name "$_rg" </dev/null 2>/dev/null | tr -d '\r\n ')
    if [ "$_exists" = "true" ]; then
        pass "$_rg exists (will update in-place)"
        # Any failed deployments?
        _failed_count=$(az deployment group list --resource-group "$_rg" \
            --query "[?properties.provisioningState=='Failed'] | length(@)" -o tsv \
            </dev/null 2>/dev/null | tr -d '\r\n ')
        [ -n "$_failed_count" ] && [ "$_failed_count" -gt 0 ] && \
            warn "$_failed_count prior failed deployment(s) on this RG"
    else
        pass "$_rg does not exist yet (fresh deploy)"
    fi
fi

# 8. Disk / free space ------------------------------------------------------
printf "\n${CYAN}Host:${NC}\n"
if command -v df >/dev/null 2>&1; then
    _free=$(df -k . 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$_free" ] && [ "$_free" -lt 1048576 ]; then
        warn "less than 1 GiB free in $(pwd)"
    else
        pass "disk space OK"
    fi
fi

# Shell type
_sh=$(readlink -f /proc/$$/exe 2>/dev/null || echo "$SHELL")
pass "shell: $_sh"

# ── Summary ────────────────────────────────────────────────────────────────
printf "\n${CYAN}═══ Summary: %d passed, %d warnings, %d failures ═══${NC}\n" \
    "$PASSES" "$WARNS" "$FAILS"
[ "$FAILS" -eq 0 ]
