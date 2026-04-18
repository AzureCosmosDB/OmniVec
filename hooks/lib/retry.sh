#!/bin/sh
# hooks/lib/retry.sh — transient-error retry helpers used by hooks.
#
# d1: Retry commands that fail on known-transient Azure/Kubernetes errors
# (429 Too Many Requests, 503 Service Unavailable, "ServiceBusy",
# "OperationNotAllowed", connection resets, intermittent TLS failures).
#
# POSIX, strict-set-eu safe.

set -u
[ "${OMNIVEC_RETRY_SH_LOADED:-}" = "1" ] && return 0
OMNIVEC_RETRY_SH_LOADED=1

: "${RED:=}"; : "${GREEN:=}"; : "${YELLOW:=}"; : "${CYAN:=}"; : "${NC:=}"

# Configurable:
#   OMNIVEC_RETRY_ATTEMPTS  (default 4)
#   OMNIVEC_RETRY_BASE_SEC  (default 5)  — exponential backoff base
: "${OMNIVEC_RETRY_ATTEMPTS:=4}"
: "${OMNIVEC_RETRY_BASE_SEC:=5}"

# Patterns that mark a failure as transient/retryable.
_RETRY_PATTERNS='
429
throttl
Too Many Requests
ServiceBusy
ServerBusy
RequestTimeout
OperationTimedOut
503
502
504
Service Unavailable
Temporary failure
Connection reset
TLS handshake
InternalServerError
i/o timeout
context deadline exceeded
'

# Is the given log blob a transient failure?
retry_is_transient() {
    _blob=$1
    _oldifs=$IFS
    IFS='
'
    for _pat in $_RETRY_PATTERNS; do
        [ -z "$_pat" ] && continue
        case "$_blob" in
            *"$_pat"*) IFS=$_oldifs; return 0 ;;
        esac
    done
    IFS=$_oldifs
    return 1
}

# retry_run LABEL -- cmd args...
# Runs cmd; on non-zero exit, classifies output; retries with exponential
# backoff if transient. Exit code of last attempt propagates.
retry_run() {
    _label=${1:-retry}; shift 2>/dev/null || true
    [ "${1:-}" = "--" ] && shift

    _attempt=1
    _rc=0
    while [ "$_attempt" -le "$OMNIVEC_RETRY_ATTEMPTS" ]; do
        _tmp=$(mktemp 2>/dev/null || mktemp -t retry)
        "$@" </dev/null >"$_tmp" 2>&1
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            cat "$_tmp"
            rm -f "$_tmp"
            return 0
        fi
        _out=$(cat "$_tmp")
        rm -f "$_tmp"
        if [ "$_attempt" -ge "$OMNIVEC_RETRY_ATTEMPTS" ]; then
            printf '%s\n' "$_out"
            printf "  ${RED}[%s] failed after %d attempts (rc=%d).${NC}\n" \
                "$_label" "$_attempt" "$_rc" >&2
            return "$_rc"
        fi
        if ! retry_is_transient "$_out"; then
            printf '%s\n' "$_out"
            printf "  ${RED}[%s] failed (rc=%d, non-transient).${NC}\n" "$_label" "$_rc" >&2
            return "$_rc"
        fi
        _sleep=$(( OMNIVEC_RETRY_BASE_SEC * _attempt ))
        printf "  ${YELLOW}[%s] transient failure (attempt %d/%d, rc=%d). Retrying in %ds...${NC}\n" \
            "$_label" "$_attempt" "$OMNIVEC_RETRY_ATTEMPTS" "$_rc" "$_sleep" >&2
        sleep "$_sleep"
        _attempt=$(( _attempt + 1 ))
    done
    return "$_rc"
}
