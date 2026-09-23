#!/bin/sh
set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
EMU_BIN="$SCRIPT_DIR/../emu/bin"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export OMNIVEC_EMU_STATE="$TMP/state"
export PATH="$EMU_BIN:$PATH"
chmod +x "$EMU_BIN"/*
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  OK  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL %s -- %s\n' "$1" "$2"; }
output_is() {
    label=$1; expected=$2; shift 2
    actual=$("$@" 2>&1); rc=$?
    if [ "$rc" -eq 0 ] && [ "$actual" = "$expected" ]; then
        ok "$label"
    else
        bad "$label" "rc=$rc; expected [$expected], got [$actual]"
    fi
}
fails_with() {
    label=$1; pattern=$2; shift 2
    actual=$("$@" 2>&1); rc=$?
    if [ "$rc" -ne 0 ] && printf '%s\n' "$actual" | grep -Eq "$pattern"; then
        ok "$label"
    else
        bad "$label" "rc=$rc; got [$actual]"
    fi
}

fails_with "missing repository is not found" NAME_UNKNOWN \
    az acr repository show-tags --name registry --repository team/api -o tsv
az acr build --registry registry --image team/api:stable . >/dev/null
output_is "unfiltered tags are a JSON string array" '["stable"]' \
    az acr repository show-tags --name registry --repository team/api
output_is "requested missing tag is empty TSV" '' \
    az acr repository show-tags --name registry --repository team/api --query "[?@ == 'latest']" -o tsv
output_is "requested missing tag is empty JSON" '[]' \
    az acr repository show-tags --name registry --repository team/api --query "[?@ == 'latest']" -o json
az acr import --name registry --source shared/team/api:stable --image team/api:latest
output_is "imported tag is exact TSV" latest \
    az acr repository show-tags --name registry --repository team/api --query "[?@ == 'latest']" -o tsv
output_is "tag query accepts compact equality" stable \
    az acr repository show-tags --name registry --repository team/api --query "[?@=='stable']" -o tsv
az acr build --registry registry --image team_api:latest . >/dev/null
output_is "repository names remain distinct" '["team/api","team_api"]' \
    az acr repository list --name registry -o json
output_is "nested repository tags do not collide" '["latest","stable"]' \
    az acr repository show-tags --name registry --repository team/api -o json
fails_with "registry state is isolated" NAME_UNKNOWN \
    az acr repository show-tags --name other --repository team/api -o tsv
fails_with "unsupported tag query is explicit" 'unsupported show-tags query' \
    az acr repository show-tags --name registry --repository team/api --query 'length(@)' -o tsv
digest=$(az acr manifest show-metadata --registry registry --name team/api:latest --query digest -o tsv)
if printf '%s\n' "$digest" | grep -Eq '^sha256:[a-f0-9]{64}$'; then ok "manifest digest is available"; else bad "manifest digest is available" "$digest"; fi
output_is "transient image probe succeeds on retry" latest \
    env OMNIVEC_EMU_TRANSIENT_CMD='az acr repository show-tags:1' sh -c '
        az acr repository show-tags --name registry --repository team/api --query "[?@ == '\''latest'\'']" -o tsv >/dev/null 2>&1 && exit 1
        az acr repository show-tags --name registry --repository team/api --query "[?@ == '\''latest'\'']" -o tsv'

namespace=$(kubectl create namespace omnivec --dry-run=client -o yaml)
if [ ! -e "$OMNIVEC_EMU_STATE/k8s/ns/omnivec" ]; then ok "namespace dry-run does not mutate state"; else bad "namespace dry-run does not mutate state" "namespace exists"; fi
output_is "namespace manifest applies" 'Namespace/omnivec configured' \
    sh -c 'printf "%s\n" "$1" | kubectl apply -f -' sh "$namespace"
output_is "namespace manifest reapplies" 'Namespace/omnivec configured' \
    sh -c 'printf "%s\n" "$1" | kubectl apply -f -' sh "$namespace"
secret=$(kubectl create secret generic demo -n omnivec --from-literal=token=synthetic --dry-run=client -o yaml)
if [ ! -e "$OMNIVEC_EMU_STATE/k8s/ns/omnivec/secrets/demo" ]; then ok "secret dry-run does not mutate state"; else bad "secret dry-run does not mutate state" "secret exists"; fi
output_is "secret uses its actual name" 'Secret/demo configured' \
    sh -c 'printf "%s\n" "$1" | kubectl apply -f -' sh "$secret"
if grep -q 'token: "synthetic"' "$OMNIVEC_EMU_STATE/k8s/ns/omnivec/secrets/demo"; then ok "secret data survives apply"; else bad "secret data survives apply" "missing data"; fi
fails_with "health fails before deployment" 'not deployed' curl --fail http://20.42.42.42/health
helm upgrade --install omnivec chart -n omnivec --wait >/dev/null
output_is "deployment generation query has six numeric fields" '1 1 2 2 2 2' \
    kubectl get deployment omnivec-api -n omnivec -o 'jsonpath={range .items[*]}{.metadata.generation}{" "}{.status.observedGeneration}{" "}{.spec.replicas}{" "}{.status.updatedReplicas}{" "}{.status.availableReplicas}{" "}{.status.replicas}{"\n"}{end}'
output_is "ready deployments pass rollout" 'deployment "deployment" successfully rolled out' \
    kubectl rollout status deployment -n omnivec
output_is "ready web and API pass public health" '{"status":"healthy"}' \
    curl --fail --silent --show-error --max-time 10 http://20.42.42.42/health
fails_with "curl never forwards unknown hosts" 'unsupported URL' curl --fail http://example.invalid/health
deployment="$OMNIVEC_EMU_STATE/k8s/ns/omnivec/deployments/omnivec-api"
sed 's/^observedGeneration=.*/observedGeneration=0/' "$deployment" > "$TMP/deployment"
mv "$TMP/deployment" "$deployment"
fails_with "stale observed generation fails rollout" 'progress deadline' kubectl rollout status deployment -n omnivec
fails_with "stale API fails health" 'HTTP 503' curl --fail http://20.42.42.42/health
helm upgrade --install omnivec chart -n omnivec --wait >/dev/null
output_is "upgrade advances generations" '2 2 2 2 2 2' \
    kubectl get deployment omnivec-api -n omnivec -o 'jsonpath={range .items[*]}{.metadata.generation}{" "}{.status.observedGeneration}{" "}{.spec.replicas}{" "}{.status.updatedReplicas}{" "}{.status.availableReplicas}{" "}{.status.replicas}{"\n"}{end}'
sed 's/^availableReplicas=.*/availableReplicas=0/' "$deployment" > "$TMP/deployment"
mv "$TMP/deployment" "$deployment"
fails_with "unavailable replicas fail rollout" 'progress deadline' kubectl rollout status deployment -n omnivec
fails_with "missing deployment fails rollout" 'not found' kubectl rollout status deployment/missing -n omnivec

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
