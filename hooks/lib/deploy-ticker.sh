#!/bin/sh
# hooks/lib/deploy-ticker.sh — background ARM deployment progress reporter.
#
# Azd shows terse top-level progress during `azd provision`. This ticker
# augments it by polling the resource-group deployment's operations list and
# printing a one-line summary every 30s, so users never feel "blank".
#
# Usage:
#   deploy_ticker_start RG_NAME [INTERVAL_SEC]   -> writes PID to $DEPLOY_TICKER_PID_FILE
#   deploy_ticker_stop                            -> terminates ticker
#
# The ticker writes to stderr (azd swallows hook stdout in some contexts).
# It self-exits if polling fails 5 consecutive times (e.g., deployment done
# and removed from ARM cache).

set -u
[ "${OMNIVEC_DEPLOY_TICKER_SH_LOADED:-}" = "1" ] && return 0
OMNIVEC_DEPLOY_TICKER_SH_LOADED=1

: "${RED:=}"; : "${GREEN:=}"; : "${YELLOW:=}"; : "${CYAN:=}"; : "${NC:=}"
: "${DEPLOY_TICKER_PID_FILE:=${TMPDIR:-/tmp}/omnivec-deploy-ticker.pid}"
: "${DEPLOY_TICKER_LOG:=${TMPDIR:-/tmp}/omnivec-deploy-ticker.log}"

_ticker_fmt_elapsed() {
    _since=$1
    _now=$(date +%s 2>/dev/null || echo 0)
    _d=$(( _now - _since ))
    [ "$_d" -lt 0 ] && _d=0
    printf '%02d:%02d' $(( _d / 60 )) $(( _d % 60 ))
}

_ticker_loop() {
    _rg=$1
    _interval=$2
    _start=$(date +%s 2>/dev/null || echo 0)
    _fail_count=0
    while :; do
        sleep "$_interval"
        _deployments=$(az deployment group list --resource-group "$_rg" \
            --query "[?properties.provisioningState=='Running'].name" -o tsv </dev/null 2>/dev/null || printf '')
        _deployments=$(printf '%s' "$_deployments" | tr -d '\r')
        if [ -z "$_deployments" ]; then
            _fail_count=$(( _fail_count + 1 ))
            [ "$_fail_count" -ge 5 ] && break
            continue
        fi
        _fail_count=0
        for _d in $_deployments; do
            _summary=$(az deployment operation group list --resource-group "$_rg" --name "$_d" \
                --query "[].{s:properties.provisioningState, t:properties.targetResource.resourceType, n:properties.targetResource.resourceName}" \
                -o tsv </dev/null 2>/dev/null || printf '')
            _total=$(printf '%s\n' "$_summary" | grep -v '^$' | wc -l | tr -d ' ')
            _ok=$(printf    '%s\n' "$_summary" | awk '$1=="Succeeded"' | wc -l | tr -d ' ')
            _running=$(printf '%s\n' "$_summary" | awk '$1=="Running"' | wc -l | tr -d ' ')
            _failed=$(printf  '%s\n' "$_summary" | awk '$1=="Failed"'  | wc -l | tr -d ' ')
            _elapsed=$(_ticker_fmt_elapsed "$_start")
            printf "${CYAN}[%s] deploy %s: %d/%d ok, %d running, %d failed${NC}\n" \
                "$_elapsed" "$_d" "$_ok" "$_total" "$_running" "$_failed" >&2
            # Show the currently-running resource(s).
            printf '%s\n' "$_summary" | awk '$1=="Running"' | head -3 | while IFS=$(printf '\t') read -r _s _t _n; do
                [ -n "$_n" ] && printf "         ↳ %s (%s)\n" "$_n" "$_t" >&2
            done
        done
    done
}

deploy_ticker_start() {
    _rg=$1
    _interval=${2:-30}
    # Avoid double-start.
    if [ -f "$DEPLOY_TICKER_PID_FILE" ]; then
        _pid=$(cat "$DEPLOY_TICKER_PID_FILE" 2>/dev/null)
        if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$DEPLOY_TICKER_PID_FILE"
    fi
    ( _ticker_loop "$_rg" "$_interval" >"$DEPLOY_TICKER_LOG" 2>&1 ) &
    _pid=$!
    printf '%s\n' "$_pid" > "$DEPLOY_TICKER_PID_FILE"
}

deploy_ticker_stop() {
    [ -f "$DEPLOY_TICKER_PID_FILE" ] || return 0
    _pid=$(cat "$DEPLOY_TICKER_PID_FILE" 2>/dev/null)
    rm -f "$DEPLOY_TICKER_PID_FILE"
    [ -z "$_pid" ] && return 0
    kill "$_pid" 2>/dev/null || true
    # Give it a moment to flush.
    sleep 1 2>/dev/null
    kill -9 "$_pid" 2>/dev/null || true
}
