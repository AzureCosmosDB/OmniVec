#!/bin/sh
# Shared helper - append "<tool> <args...>" to $OMNIVEC_MOCK_LOG.
# Sourced by every mock stub in this directory.
_mock_record() {
    _mr_tool="$1"; shift
    if [ -n "${OMNIVEC_MOCK_LOG:-}" ]; then
        printf '%s' "$_mr_tool" >> "$OMNIVEC_MOCK_LOG"
        for _mr_a in "$@"; do
            # Quote args containing whitespace so the log is parseable.
            case "$_mr_a" in
                *' '*|*'	'*) printf ' "%s"' "$_mr_a" >> "$OMNIVEC_MOCK_LOG" ;;
                *)               printf ' %s'   "$_mr_a" >> "$OMNIVEC_MOCK_LOG" ;;
            esac
        done
        printf '\n' >> "$OMNIVEC_MOCK_LOG"
    fi
}

_mock_env_store() {
    # Simple key=value store backed by $OMNIVEC_MOCK_AZD_ENV.
    if [ -z "${OMNIVEC_MOCK_AZD_ENV:-}" ]; then
        OMNIVEC_MOCK_AZD_ENV="${TMPDIR:-/tmp}/mock-azd-env.$$"
        export OMNIVEC_MOCK_AZD_ENV
    fi
    [ -f "$OMNIVEC_MOCK_AZD_ENV" ] || : > "$OMNIVEC_MOCK_AZD_ENV"
}

_mock_env_set() {
    _mock_env_store
    _k="$1"; _v="$2"
    # Remove any prior line for $_k, then append the new value.
    grep -v "^${_k}=" "$OMNIVEC_MOCK_AZD_ENV" > "${OMNIVEC_MOCK_AZD_ENV}.tmp" 2>/dev/null || true
    printf '%s=%s\n' "$_k" "$_v" >> "${OMNIVEC_MOCK_AZD_ENV}.tmp"
    mv "${OMNIVEC_MOCK_AZD_ENV}.tmp" "$OMNIVEC_MOCK_AZD_ENV"
}

_mock_env_get() {
    _mock_env_store
    _k="$1"
    # Preset values (read-only): OMNIVEC_* starting state from caller env.
    # Then look up the recorded store.
    grep "^${_k}=" "$OMNIVEC_MOCK_AZD_ENV" 2>/dev/null | tail -1 | sed "s/^${_k}=//"
}
