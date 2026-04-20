#!/bin/sh
# tests/emu/lib/_common.sh - shared helpers for the OmniVec service emulator.
#
# Loaded by each emulated binary (az, azd, kubectl, helm, docker). Provides:
#   - State directory plumbing ($OMNIVEC_EMU_STATE)
#   - Chronological event log ($OMNIVEC_EMU_STATE/events.log)
#   - Fault injection via env vars (see tests/emu/README.md)

set -u

: "${OMNIVEC_EMU_STATE:=${TMPDIR:-/tmp}/omnivec-emu}"
mkdir -p "$OMNIVEC_EMU_STATE"

emu_log_event() {
    _bin=$1; shift
    _argv=$*
    printf '%s\t%s\t%s\n' "$(date +%s 2>/dev/null || echo 0)" "$_bin" "$_argv" \
        >> "$OMNIVEC_EMU_STATE/events.log" 2>/dev/null || true
}

emu_dir() {
    _p="$OMNIVEC_EMU_STATE/$1"
    mkdir -p "$_p"
    printf '%s' "$_p"
}

emu_set() {
    # emu_set <subdir> <key> <value>
    _d=$(emu_dir "$1")
    printf '%s' "$3" > "$_d/$2"
}

emu_get() {
    _f="$OMNIVEC_EMU_STATE/$1/$2"
    [ -f "$_f" ] && cat "$_f"
}

emu_has() {
    [ -f "$OMNIVEC_EMU_STATE/$1/$2" ]
}

emu_list() {
    _d="$OMNIVEC_EMU_STATE/$1"
    [ -d "$_d" ] || return 0
    ls "$_d" 2>/dev/null
}

emu_del() {
    rm -f "$OMNIVEC_EMU_STATE/$1/$2"
}

# Safe filename: translate problematic chars
emu_safe() {
    printf '%s' "$1" | tr '/:@ ' '____'
}

# ── Fault injection ─────────────────────────────────────────────────────────
emu_match() {
    printf '%s' "$2" | grep -Eq -- "$1" 2>/dev/null
}

emu_counter_inc() {
    _safe=$(printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_')
    _d=$(emu_dir "fault")
    _f="$_d/$_safe.count"
    _n=0
    [ -f "$_f" ] && _n=$(cat "$_f" 2>/dev/null || echo 0)
    _n=$((_n + 1))
    echo "$_n" > "$_f"
    echo "$_n"
}

emu_apply_faults() {
    _bin=$1; shift
    _cmdline="$_bin $*"

    # Global mode
    case "${OMNIVEC_EMU_MODE:-success}" in
        slow)
            sleep "${OMNIVEC_EMU_SLOW_SECS:-2}" 2>/dev/null || true
            ;;
        fail)
            case "$_cmdline" in
                *"helm upgrade"*|*"helm install"*)
                    echo "Error: Internal error occurred: fake failure injected by OMNIVEC_EMU_MODE=fail" >&2
                    exit 1 ;;
            esac ;;
        transient)
            case "$_cmdline" in
                *"helm upgrade"*|*"helm install"*)
                    _n=$(emu_counter_inc "global-transient")
                    if [ "$_n" -le "${OMNIVEC_EMU_TRANSIENT_N:-2}" ]; then
                        echo "Error: 503 Service Unavailable (transient, attempt $_n)" >&2
                        exit 1
                    fi ;;
            esac ;;
    esac

    if [ -n "${OMNIVEC_EMU_DELAY_CMD:-}" ]; then
        _rx=${OMNIVEC_EMU_DELAY_CMD%:*}
        _secs=${OMNIVEC_EMU_DELAY_CMD##*:}
        if emu_match "$_rx" "$_cmdline"; then
            sleep "$_secs" 2>/dev/null || true
        fi
    fi

    if [ -n "${OMNIVEC_EMU_TRANSIENT_CMD:-}" ]; then
        _rx=${OMNIVEC_EMU_TRANSIENT_CMD%:*}
        _max=${OMNIVEC_EMU_TRANSIENT_CMD##*:}
        if emu_match "$_rx" "$_cmdline"; then
            _n=$(emu_counter_inc "trans_${_rx}")
            if [ "$_n" -le "$_max" ]; then
                echo "Error: 503 Service Unavailable (injected transient $_n/$_max)" >&2
                exit 1
            fi
        fi
    fi

    if [ -n "${OMNIVEC_EMU_FAIL_CMD:-}" ]; then
        if emu_match "$OMNIVEC_EMU_FAIL_CMD" "$_cmdline"; then
            echo "Error: injected non-transient failure (matched: $OMNIVEC_EMU_FAIL_CMD)" >&2
            exit 1
        fi
    fi
}

emu_enter() {
    _bin=$1; shift
    emu_log_event "$_bin" "$@"
    emu_apply_faults "$_bin" "$@"
}
