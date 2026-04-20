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
_retry_print_excerpt() {
    # Print the last few non-blank lines of $1 to stderr, indented, in yellow.
    # Helps users understand WHY a transient retry happened without dumping
    # the entire log mid-flow.
    _src=$1
    _max=${2:-6}
    [ -s "$_src" ] || return 0
    printf "  ${YELLOW}---- last output ----${NC}\n" >&2
    grep -v '^[[:space:]]*$' "$_src" 2>/dev/null | tail -n "$_max" \
        | sed 's/^/    /' >&2
    printf "  ${YELLOW}---------------------${NC}\n" >&2
}

retry_run() {
    _label=${1:-retry}; shift 2>/dev/null || true
    [ "${1:-}" = "--" ] && shift

    # Persist last attempt's full log to a stable path so users can inspect it
    # after a successful retry (mktemp would be lost on success).
    _log_dir=${TMPDIR:-/tmp}
    _log="${_log_dir}/omnivec-retry-${_label}.log"

    _attempt=1
    _rc=0
    while [ "$_attempt" -le "$OMNIVEC_RETRY_ATTEMPTS" ]; do
        : > "$_log"

        # Optional heartbeat: while the command runs, periodically print an
        # "elapsed + pod status" line to stderr so the user doesn't stare at
        # a frozen terminal during long helm waits. Activated by setting
        #   OMNIVEC_RETRY_HEARTBEAT_CMD  — shell cmd that prints a one-shot status
        #   OMNIVEC_RETRY_HEARTBEAT_SEC  — interval (default 20)
        _hb_pid=""
        if [ -n "${OMNIVEC_RETRY_HEARTBEAT_CMD:-}" ]; then
            _hb_interval=${OMNIVEC_RETRY_HEARTBEAT_SEC:-20}
            _hb_label=$_label
            (
                _hb_start=$(date +%s 2>/dev/null || echo 0)
                # Wait one interval before the first heartbeat so fast commands
                # (< interval) never produce any heartbeat noise.
                sleep "$_hb_interval"
                while :; do
                    _hb_now=$(date +%s 2>/dev/null || echo 0)
                    _hb_el=$(( _hb_now - _hb_start ))
                    printf "  ${CYAN}[%s] still running (%ds elapsed)...${NC}\n" \
                        "$_hb_label" "$_hb_el" >&2
                    # Run the heartbeat command; ignore its stderr (cluster may
                    # be briefly unreachable during a rollout), pipe its stdout
                    # to the parent's stderr so the user sees status inline.
                    sh -c "$OMNIVEC_RETRY_HEARTBEAT_CMD" >&2 2>/dev/null || true
                    sleep "$_hb_interval"
                done
            ) &
            _hb_pid=$!
        fi

        "$@" </dev/null >"$_log" 2>&1
        _rc=$?

        if [ -n "$_hb_pid" ]; then
            kill "$_hb_pid" 2>/dev/null || true
            wait "$_hb_pid" 2>/dev/null || true
        fi

        if [ "$_rc" -eq 0 ]; then
            cat "$_log"
            return 0
        fi
        _out=$(cat "$_log")
        if [ "$_attempt" -ge "$OMNIVEC_RETRY_ATTEMPTS" ]; then
            printf '%s\n' "$_out"
            printf "  ${RED}[%s] failed after %d attempts (rc=%d). Full log: %s${NC}\n" \
                "$_label" "$_attempt" "$_rc" "$_log" >&2
            return "$_rc"
        fi
        if ! retry_is_transient "$_out"; then
            printf '%s\n' "$_out"
            printf "  ${RED}[%s] failed (rc=%d, non-transient). Full log: %s${NC}\n" "$_label" "$_rc" "$_log" >&2
            return "$_rc"
        fi
        _sleep=$(( OMNIVEC_RETRY_BASE_SEC * _attempt ))
        printf "  ${YELLOW}[%s] transient failure (attempt %d/%d, rc=%d). Retrying in %ds...${NC}\n" \
            "$_label" "$_attempt" "$OMNIVEC_RETRY_ATTEMPTS" "$_rc" "$_sleep" >&2
        _retry_print_excerpt "$_log" 6
        printf "  ${CYAN}(full log: %s)${NC}\n" "$_log" >&2
        sleep "$_sleep"
        _attempt=$(( _attempt + 1 ))
    done
    return "$_rc"
}
