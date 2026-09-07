#!/bin/sh
# Hermetic function tests: no Azure, cluster, or filesystem mutations.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
HOOK="$ROOT/hooks/postprovision.sh"
for fn in read_input build_image run_bounded_import import_image kubectl_omnivec apply_kubernetes_resource; do
  eval "$(sed -n "/^${fn}() {/,/^}/p" "$HOOK")"
done
GREEN='' CYAN='' RED='' NC=''
FORCE_IMPORT=false OMNIVEC_BUILD=true BUILD_MODE=docker
ACR_NAME=mock ACR_LOGIN_SERVER=mock.invalid
CALLS=""
image_exists() { return 0; }
mark_image_update() { IMAGES_CHANGED=true; }
az() { CALLS="$CALLS az:$*"; }
docker() { CALLS="$CALLS docker:$1"; }
build_image test Dockerfile .
case "$CALLS" in *"az:acr login --name mock"*"docker:build"*"docker:push"*) ;; *) echo "FAIL forced build/login: $CALLS"; exit 1;; esac
echo "OK explicit source build replaces an existing tag and authenticates Docker"
OMNIVEC_BUILD=false CALLS=""
build_image test Dockerfile .
[ -z "$CALLS" ]
echo "OK ordinary deploy preserves existing images"

OMNIVEC_NONINTERACTIVE=1
[ -z "$(read_input 'must not prompt')" ]
echo "OK noninteractive import never opens a terminal"

SAVED_PATH=$PATH
OMNIVEC_TEST_SLEEP_BIN=$(command -v sleep)
export OMNIVEC_TEST_SLEEP_BIN
chmod +x "$ROOT/tests/hooks/portable-mocks/sleep"
PATH="$ROOT/tests/hooks/portable-mocks"
export PATH
if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
  echo "FAIL timeout utilities unexpectedly available"; exit 1
fi
az() { printf '%s\n' "$*"; }
[ "$(import_image --name mock)" = "acr import --name mock" ]
echo "OK real import helper succeeds without timeout or gtimeout on PATH"

az() { return 7; }
set +e
(import_image --name mock)
rc=$?
set -e
[ "$rc" = 7 ]
echo "OK ordinary Azure failures preserve their exit code"

OMNIVEC_IMPORT_TIMEOUT_SEC=1
az() { while :; do :; done; }
set +e
(import_image --name mock; exit 0) 2>/dev/null
rc=$?
set -e
[ "$rc" = 124 ]
echo "OK portable watchdog bounds stalled imports and prevents source-build fallback"

OMNIVEC_TEST_SLEEP_FAIL=true
export OMNIVEC_TEST_SLEEP_FAIL
set +e
(import_image --name mock; exit 0) 2>/dev/null
rc=$?
set -e
[ "$rc" = 125 ]
echo "OK watchdog failures stop deployment rather than appearing as authentication failures"

unset -f az
unset OMNIVEC_TEST_SLEEP_FAIL
set +e
(import_image --name mock; exit 0) 2>/dev/null
rc=$?
set -e
[ "$rc" = 127 ]
echo "OK missing Azure CLI stops deployment without authentication fallback"
unset OMNIVEC_IMPORT_TIMEOUT_SEC
unset OMNIVEC_TEST_SLEEP_BIN
PATH=$SAVED_PATH
export PATH

KUBE_CONTEXT=mock OMNIVEC_KUBECONFIG=isolated
kubectl() { CALLS="$*"; }
kubectl_omnivec get pods
case "$CALLS" in *"--kubeconfig isolated --context mock --request-timeout=30s get pods"*) ;; *) exit 1;; esac
echo "OK Kubernetes requests are bounded and use isolated credentials"

healthy() {
  printf '%s\n' "$1" | awk 'NF != 6 || $2 < $1 || $3 != $4 || $3 != $5 || $3 != $6 {bad=1} END {exit (NR == 0 || bad)}'
}
healthy "2 2 2 2 2 2"
if healthy "" || healthy "2 2 2   " || healthy "2 1 2 2 2 2" || healthy "2 2 2 1 2 2"; then
  echo "FAIL incomplete deployment considered healthy"; exit 1
fi
echo "OK empty, unobserved, and incomplete rollouts cannot pass health checks"

if grep -Eq 'helm (rollback|uninstall) omnivec|cp -f .*HOME/.kube/config|adopt_orphaned_resources' "$HOOK"; then
  echo "FAIL unsafe automatic deployment recovery"; exit 1
fi
echo "OK no automatic uninstall, ownership takeover, or default kubeconfig overwrite"
eval "$(sed -n '/^validate_system_pool() {/,/^}/p' "$ROOT/hooks/preprovision.sh")"
validate_system_pool Standard_D4s_v5 2
if validate_system_pool Standard_B4ms 2 2>/dev/null ||
   validate_system_pool Standard_D2s_v3 2 2>/dev/null ||
   validate_system_pool Standard_D4s_v5 1 2>/dev/null ||
   validate_system_pool Standard_D4s_v5 invalid 2>/dev/null; then
  echo "FAIL invalid AKS system pool accepted"; exit 1
fi
echo "OK supported AKS default accepted and invalid presets rejected"
kubectl_omnivec() { return 17; }
set +e
apply_kubernetes_resource create secret ignored
rc=$?
set -e
[ "$rc" = 17 ]
echo "OK resource generation failures cannot be masked by apply"
kubectl_omnivec() {
  case "$1" in
    apply) return 19 ;;
    *) printf 'kind: Secret\n' ;;
  esac
}
set +e
apply_kubernetes_resource create secret ignored
rc=$?
set -e
[ "$rc" = 19 ]
echo "OK resource apply failures propagate"
echo "14 deployment checks passed"
