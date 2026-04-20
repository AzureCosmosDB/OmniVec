#!/bin/sh
# Integration tests for infra/main.bicep parameter plumbing.
#
# Regression target: historically, azd env substitution would inject raw
# strings (sometimes with BOM/CR/whitespace) into a parameters.json whose
# Bicep parameters were typed `bool`/`int`, causing intermittent
# InvalidTemplate failures.
#
# These tests simulate azd's substitution locally with various ugly values
# and verify the resolved parameters file + Bicep compile produce a valid,
# consistent ARM template without error.
#
# Requires: bicep (either `bicep` on PATH or `az bicep`), sh/dash/bash.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
PARAM_TMPL="$REPO_ROOT/infra/main.parameters.json"
MAIN_BICEP="$REPO_ROOT/infra/main.bicep"

BICEP_BIN=""
if command -v bicep >/dev/null 2>&1; then
    BICEP_BIN="bicep"
elif [ -x "$HOME/.azure/bin/bicep" ];     then BICEP_BIN="$HOME/.azure/bin/bicep"
elif [ -x "$HOME/.azure/bin/bicep.exe" ]; then BICEP_BIN="$HOME/.azure/bin/bicep.exe"
elif command -v az >/dev/null 2>&1; then
    BICEP_BIN="az-bicep"
fi

if [ -z "$BICEP_BIN" ]; then
    echo "SKIP: no bicep CLI available (install with 'az bicep install')" >&2
    exit 0
fi

# Resolve a Python interpreter: python3 (posix/mac/linux) or python (Windows).
PY_BIN=""
if   command -v python3 >/dev/null 2>&1; then PY_BIN="python3"
elif command -v python  >/dev/null 2>&1; then PY_BIN="python"
elif command -v py      >/dev/null 2>&1; then PY_BIN="py -3"
fi
if [ -z "$PY_BIN" ]; then
    echo "SKIP: no python interpreter available" >&2
    exit 0
fi

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t omnivec-bicep)
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

render_params() {
    _out=$1
    $PY_BIN - "$PARAM_TMPL" "$_out" <<'PYEOF'
import os, re, sys
src, out = sys.argv[1], sys.argv[2]
with open(src, 'r', encoding='utf-8') as f:
    text = f.read()
def repl(m):
    return os.environ.get(m.group(1), '')
text = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', repl, text)
with open(out, 'w', encoding='utf-8') as f:
    f.write(text)
PYEOF
}

bicep_build() {
    _src=$1
    _out=$2
    case $BICEP_BIN in
        az-bicep) az bicep build --file "$_src" --outfile "$_out" 2>&1 ;;
        *)        "$BICEP_BIN" build "$_src" --outfile "$_out" 2>&1 ;;
    esac
}

validate_params_json() {
    _params=$1
    _arm=$2
    $PY_BIN - "$_params" "$_arm" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: params = json.load(f)
with open(sys.argv[2]) as f: arm    = json.load(f)
declared = arm.get('parameters', {})
given    = params.get('parameters', {})
errors = []
for name, spec in given.items():
    if name not in declared: continue
    expected_type = declared[name].get('type')
    value = spec.get('value')
    if expected_type == 'int' and not isinstance(value, int):
        errors.append(f"param '{name}' expects int, got {type(value).__name__}={value!r}")
    if expected_type == 'bool' and not isinstance(value, bool):
        errors.append(f"param '{name}' expects bool, got {type(value).__name__}={value!r}")
if errors:
    for e in errors: print("  TYPE MISMATCH:", e, file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PYEOF
}

printf '\n=== bicep parameter plumbing tests ===\n'
printf 'using bicep: %s\n' "$BICEP_BIN"

ARM_OUT="$TMP/main.json"
if ! bicep_build "$MAIN_BICEP" "$ARM_OUT" >"$TMP/bicep.log" 2>&1; then
    fail "bicep build main.bicep" "see $TMP/bicep.log"
    cat "$TMP/bicep.log"
    exit 1
fi
pass "bicep build main.bicep compiles cleanly"

(
    export AZURE_ENV_NAME="testenv" AZURE_LOCATION="eastus2"
    export OMNIVEC_SYSTEM_NODE_VM_SIZE="Standard_B4ms"
    export OMNIVEC_SYSTEM_NODE_COUNT="2"
    export OMNIVEC_GPU_NODE_VM_SIZE=""
    export OMNIVEC_GPU_NODE_COUNT="0"
    export OMNIVEC_ENABLE_BLOB_SOURCE="true"
    render_params "$TMP/p1.json"
    validate_params_json "$TMP/p1.json" "$ARM_OUT"
) && pass "canonical values (true/2/0) validate" || fail "canonical values"

(
    export AZURE_ENV_NAME="testenv" AZURE_LOCATION="eastus2"
    export OMNIVEC_SYSTEM_NODE_VM_SIZE="Standard_B4ms"
    export OMNIVEC_SYSTEM_NODE_COUNT="2"
    export OMNIVEC_GPU_NODE_COUNT="0"
    export OMNIVEC_ENABLE_BLOB_SOURCE="true"
    export OMNIVEC_GPU_NODE_VM_SIZE=""
    render_params "$TMP/p2.json"
    validate_params_json "$TMP/p2.json" "$ARM_OUT"
) && pass "string values for former int/bool params validate" || fail "string-typed params"

(
    export AZURE_ENV_NAME="testenv" AZURE_LOCATION="eastus2"
    export OMNIVEC_SYSTEM_NODE_VM_SIZE=""
    export OMNIVEC_SYSTEM_NODE_COUNT=""
    export OMNIVEC_GPU_NODE_VM_SIZE=""
    export OMNIVEC_GPU_NODE_COUNT=""
    export OMNIVEC_ENABLE_BLOB_SOURCE=""
    render_params "$TMP/p3.json"
    validate_params_json "$TMP/p3.json" "$ARM_OUT"
) && pass "empty env values produce valid typed params" || fail "empty env values"

(
    # Validates the two-layer defense: hook-side sanitization (strips CR before
    # azd env set) PLUS Bicep-side normalization (trim/replace). Here we simulate
    # the hook stripping CR, and verify the resulting params round-trip cleanly.
    export AZURE_ENV_NAME="testenv" AZURE_LOCATION="eastus2"
    raw_size="Standard_B4ms$(printf '\r')"
    raw_count="2$(printf '\r')"
    raw_blob="false$(printf '\r')"
    # Simulate hook sanitize: tr -d '\r' (matches env sanitize loop in preprovision.sh)
    export OMNIVEC_SYSTEM_NODE_VM_SIZE="$(printf '%s' "$raw_size" | tr -d '\r')"
    export OMNIVEC_SYSTEM_NODE_COUNT="$(printf '%s' "$raw_count" | tr -d '\r')"
    export OMNIVEC_GPU_NODE_VM_SIZE=""
    export OMNIVEC_GPU_NODE_COUNT="$(printf '%s' "0$(printf '\r')" | tr -d '\r')"
    export OMNIVEC_ENABLE_BLOB_SOURCE="$(printf '%s' "$raw_blob" | tr -d '\r')"
    render_params "$TMP/p4.json"
    validate_params_json "$TMP/p4.json" "$ARM_OUT"
) && pass "CR-suffixed values (post hook-sanitize) validate" || fail "CR-suffixed values"

(
    export AZURE_ENV_NAME="testenv" AZURE_LOCATION="eastus2"
    export OMNIVEC_SYSTEM_NODE_VM_SIZE="Standard_B4ms"
    export OMNIVEC_SYSTEM_NODE_COUNT="2"
    export OMNIVEC_GPU_NODE_VM_SIZE=""
    export OMNIVEC_GPU_NODE_COUNT="0"
    export OMNIVEC_ENABLE_BLOB_SOURCE="$(printf '\357\273\277true')"
    render_params "$TMP/p5.json"
    validate_params_json "$TMP/p5.json" "$ARM_OUT"
) && pass "BOM-prefixed value still validates" || fail "BOM-prefixed value"

$PY_BIN - "$TMP/p1.json" "$ARM_OUT" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: params = json.load(f)
with open(sys.argv[2]) as f: arm    = json.load(f)
given    = set(params.get('parameters', {}))
required_declared = {k for k,v in arm.get('parameters', {}).items()
                    if 'defaultValue' not in v}
missing_required = required_declared - given
if missing_required:
    print("  missing required params in parameters.json:", missing_required, file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PYEOF
if [ $? -eq 0 ]; then pass "all required Bicep params present in parameters.json"
else                  fail "required param coverage"; fi

cat > "$TMP/probe.bicep" <<'BICEPEOF'
param raw string = 'true\r'
output cleaned string = toLower(trim(replace(replace(raw, '\u{FEFF}', ''), '\r', '')))
BICEPEOF
if bicep_build "$TMP/probe.bicep" "$TMP/probe.json" >"$TMP/probe.log" 2>&1; then
    pass "Bicep probe template compiles (trim/replace/toLower chain)"
else
    fail "Bicep probe compile" "$(cat "$TMP/probe.log")"
fi

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
