#!/bin/sh
# OmniVec - heartbeat helpers (strict POSIX sh)
#
# Designed for: dash, ash/busybox, bash in posix mode, zsh-sh, macOS /bin/sh.
# NO bashisms. NO 'local'. NO '[[ ]]'. NO arrays. NO process substitution.
#
# Source this file once; safe to re-source (idempotent).
#
# Public API
#   hb_init                          initialize timing (idempotent)
#   hb_now                           print "[mm:ss]" elapsed since init
#   hb_log LEVEL MSG...              timestamped log line
#                                    LEVEL in: info|ok|warn|err|tick
#   wait_with_heartbeat LABEL CMD... run CMD in background, emit heartbeat
#                                    every OMNIVEC_HEARTBEAT_INTERVAL seconds
#                                    until CMD exits. Returns CMD's exit code.
#   hb_step_start NAME               record start of a named step
#   hb_step_end   NAME [status]      record end; append to timings JSONL
#   hb_slowest_summary               print slowest 5 steps (on failure paths)
#
# Env knobs
#   OMNIVEC_HEARTBEAT_INTERVAL       seconds between ticks (default 15)
#   OMNIVEC_HEARTBEAT_QUIET=1        suppress tick lines (tests/CI quiet mode)
#   OMNIVEC_RUN_START                unix ts of run start (auto-set)
#   OMNIVEC_TIMINGS_FILE             JSONL path for step timings (auto-set)

# -- Colors: only if stdout is a TTY ---------------------------------------
if [ -t 1 ]; then
    _HB_YEL=$(printf '\033[1;33m')
    _HB_GRN=$(printf '\033[0;32m')
    _HB_CYN=$(printf '\033[0;36m')
    _HB_RED=$(printf '\033[0;31m')
    _HB_DIM=$(printf '\033[2m')
    _HB_NC=$(printf '\033[0m')
else
    _HB_YEL=''; _HB_GRN=''; _HB_CYN=''; _HB_RED=''; _HB_DIM=''; _HB_NC=''
fi

# -- init ------------------------------------------------------------------
hb_init() {
    if [ -z "${OMNIVEC_RUN_START:-}" ]; then
        OMNIVEC_RUN_START=$(date +%s)
        export OMNIVEC_RUN_START
    fi
    if [ -z "${OMNIVEC_TIMINGS_FILE:-}" ]; then
        _hb_runs_dir="${HOME:-/tmp}/.omnivec/runs"
        mkdir -p "$_hb_runs_dir" 2>/dev/null || true
        # Use a fixed name per shell PID; portable mktemp signatures vary.
        OMNIVEC_TIMINGS_FILE="$_hb_runs_dir/timings-$(date +%Y%m%d-%H%M%S)-$$.jsonl"
        export OMNIVEC_TIMINGS_FILE
    fi
    if [ -z "${OMNIVEC_HEARTBEAT_INTERVAL:-}" ]; then
        OMNIVEC_HEARTBEAT_INTERVAL=15
    fi
    export OMNIVEC_HEARTBEAT_INTERVAL
}

# -- elapsed timestamp -----------------------------------------------------
hb_now() {
    hb_init
    _hb_e=$(( $(date +%s) - OMNIVEC_RUN_START ))
    [ "$_hb_e" -lt 0 ] && _hb_e=0
    printf '[%02d:%02d]' $(( _hb_e / 60 )) $(( _hb_e % 60 ))
}

# -- structured log line ---------------------------------------------------
hb_log() {
    _hb_lvl=$1
    shift
    _hb_color=$_HB_NC
    case $_hb_lvl in
        info)  _hb_color=$_HB_CYN ;;
        ok)    _hb_color=$_HB_GRN ;;
        warn)  _hb_color=$_HB_YEL ;;
        err)   _hb_color=$_HB_RED ;;
        tick)  _hb_color=$_HB_DIM ;;
    esac
    printf '%s%s %s%s\n' "$_hb_color" "$(hb_now)" "$*" "$_HB_NC"
}

# -- run a command with heartbeat -----------------------------------------
# wait_with_heartbeat LABEL CMD [ARG...]
# Preserves CMD's exit code via `wait`. Works in dash, ash, bash.
wait_with_heartbeat() {
    hb_init
    if [ $# -lt 2 ]; then
        hb_log err "wait_with_heartbeat: usage: LABEL CMD [ARG...]"
        return 2
    fi
    _hb_label=$1
    shift

    # Run the command in a subshell so $! is our own background PID.
    # `exec "$@"` avoids an extra shell layer, giving us clean signals.
    ( exec "$@" ) &
    _hb_pid=$!

    _hb_start=$(date +%s)
    _hb_interval=${OMNIVEC_HEARTBEAT_INTERVAL:-15}
    # Clamp to sane range (1..3600)
    case $_hb_interval in
        ''|*[!0-9]*) _hb_interval=15 ;;
    esac
    [ "$_hb_interval" -lt 1 ]    && _hb_interval=1
    [ "$_hb_interval" -gt 3600 ] && _hb_interval=3600

    # Outer loop: while child is still alive, sleep up to interval sec then tick.
    while kill -0 "$_hb_pid" 2>/dev/null; do
        _hb_slept=0
        # 1-second slices so we exit quickly when child finishes.
        while [ "$_hb_slept" -lt "$_hb_interval" ]; do
            sleep 1 2>/dev/null || true
            _hb_slept=$(( _hb_slept + 1 ))
            if ! kill -0 "$_hb_pid" 2>/dev/null; then
                break
            fi
        done
        if kill -0 "$_hb_pid" 2>/dev/null; then
            _hb_el=$(( $(date +%s) - _hb_start ))
            if [ "${OMNIVEC_HEARTBEAT_QUIET:-0}" != "1" ]; then
                hb_log tick "still ${_hb_label}... (${_hb_el}s)"
            fi
        fi
    done

    # Reap and propagate exit code.
    wait "$_hb_pid" 2>/dev/null
    _hb_rc=$?
    return $_hb_rc
}

# -- step timing -----------------------------------------------------------
# Slot = step name with non-alphanumerics replaced by '_'.
_hb_slot() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'
}

hb_step_start() {
    hb_init
    _hb_name=$1
    _hb_slot_name=$(_hb_slot "$_hb_name")
    _hb_ts=$(date +%s)
    # Use eval to set a dynamic variable name; safe because slot is sanitized.
    eval "_HB_STEP_START_${_hb_slot_name}=${_hb_ts}"
    hb_log info "> ${_hb_name}"
}

hb_step_end() {
    hb_init
    _hb_name=$1
    _hb_status=${2:-ok}
    _hb_slot_name=$(_hb_slot "$_hb_name")
    eval "_hb_start=\${_HB_STEP_START_${_hb_slot_name}:-0}"
    _hb_now=$(date +%s)
    if [ "$_hb_start" -gt 0 ]; then
        _hb_dur=$(( _hb_now - _hb_start ))
    else
        _hb_dur=0
    fi
    # JSON-escape name + status (minimal: backslash and quote)
    _hb_name_j=$(printf '%s' "$_hb_name" | sed 's/\\/\\\\/g; s/"/\\"/g')
    _hb_stat_j=$(printf '%s' "$_hb_status" | sed 's/\\/\\\\/g; s/"/\\"/g')
    if [ -n "${OMNIVEC_TIMINGS_FILE:-}" ]; then
        printf '{"name":"%s","status":"%s","start":%s,"end":%s,"duration":%s}\n' \
            "$_hb_name_j" "$_hb_stat_j" "$_hb_start" "$_hb_now" "$_hb_dur" \
            >> "$OMNIVEC_TIMINGS_FILE" 2>/dev/null || true
    fi
    case $_hb_status in
        ok)   hb_log ok   "OK ${_hb_name} (${_hb_dur}s)" ;;
        fail) hb_log err  "FAIL ${_hb_name} (${_hb_dur}s)" ;;
        skip) hb_log info "SKIP ${_hb_name}" ;;
        *)    hb_log info "${_hb_name}: ${_hb_status} (${_hb_dur}s)" ;;
    esac
}

# -- summary: slowest steps -----------------------------------------------
# Portable JSONL parsing using sed; no awk, no jq.
hb_slowest_summary() {
    [ -n "${OMNIVEC_TIMINGS_FILE:-}" ] || return 0
    [ -f "$OMNIVEC_TIMINGS_FILE" ]     || return 0
    hb_log info "Step timing summary (slowest first):"
    # Extract "<duration>\t<name>" per line, sort desc, take top 5.
    while IFS= read -r _hb_ln; do
        [ -n "$_hb_ln" ] || continue
        _hb_d=$(printf '%s' "$_hb_ln" | sed -n 's/.*"duration":\([0-9][0-9]*\).*/\1/p')
        _hb_n=$(printf '%s' "$_hb_ln" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')
        if [ -n "$_hb_d" ] && [ -n "$_hb_n" ]; then
            printf '%s\t%s\n' "$_hb_d" "$_hb_n"
        fi
    done < "$OMNIVEC_TIMINGS_FILE" \
        | sort -rn 2>/dev/null \
        | head -5 \
        | while IFS='	' read -r _hb_d _hb_n; do
              printf '    %4ss  %s\n' "$_hb_d" "$_hb_n"
          done
}

# -- auto-init on source ---------------------------------------------------
hb_init
