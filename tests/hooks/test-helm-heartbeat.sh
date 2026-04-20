#!/bin/sh
# tests/hooks/test-helm-heartbeat.sh — validate the helm-deploy heartbeat
# surfaces useful information (not just "all pods Running" noise).

set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf "  OK  %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  FAIL %s -- %s\n" "$1" "$2"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin"
cat > "$TMP/bin/kubectl" <<'MOCK'
#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        --context|--kubeconfig|-n|--namespace|-o) shift 2 ;;
        --sort-by=*) shift ;;
        -*) shift ;;
        *) break ;;
    esac
done
verb=${1:-}; noun=${2:-}
case "$verb $noun" in
    "get deploy")
        case "${KUBECTL_MOCK_SCENARIO:-ready}" in
            ready) ;;
            pending) printf "omnivec-api 0/2\n" ;;
            half)    printf "omnivec-api 1/2\n" ;;
        esac
        ;;
    "get svc")
        case "${KUBECTL_MOCK_SCENARIO:-ready}" in
            ready)   printf "omnivec 20.1.2.3\n" ;;
            pending) printf "omnivec \n" ;;
            half)    printf "omnivec 20.1.2.3\n" ;;
        esac
        ;;
    "get events")
        case "${KUBECTL_MOCK_SCENARIO:-ready}" in
            pending) printf "FailedScheduling: 0/3 nodes available\n" ;;
        esac
        ;;
esac
MOCK
chmod +x "$TMP/bin/kubectl"
export PATH="$TMP/bin:$PATH"

run_heartbeat() {
    sh -c '
KC="kubectl -n omnivec"
_not_ready=$($KC get deploy -o "jsonpath={x}" 2>/dev/null | grep -v "^$")
_pending_lb=$($KC get svc -o "jsonpath={x}" 2>/dev/null | awk "/ \$/ {print \$1}")
_events=$($KC get events --sort-by=.lastTimestamp -o "jsonpath={x}" 2>/dev/null | tail -5)
{
  if [ -n "$_not_ready" ]; then
    printf "    deployments not ready:\n"
    printf "%s\n" "$_not_ready" | awk "{printf \"      %s\n\", \$0}"
  fi
  if [ -n "$_pending_lb" ]; then
    printf "    services waiting for external IP:\n"
    printf "%s\n" "$_pending_lb" | awk "{printf \"      %s\n\", \$0}"
  fi
  if [ -n "$_events" ]; then
    printf "    recent warnings (last 5):\n"
    printf "%s\n" "$_events" | awk "{printf \"      %s\n\", substr(\$0,1,120)}"
  fi
  if [ -z "$_not_ready$_pending_lb$_events" ]; then
    printf "    all resources ready -- helm is finalising (atomic wait, typically 15-30s)\n"
  fi
}'
}

# Scenario 1: ready
out=$(KUBECTL_MOCK_SCENARIO=ready run_heartbeat)
case "$out" in
    *"all resources ready"*) ok "ready scenario -> all resources ready" ;;
    *)                       bad "ready scenario -> all resources ready" "got: $out" ;;
esac

# Scenario 2: pending
out=$(KUBECTL_MOCK_SCENARIO=pending run_heartbeat)
case "$out" in
    *"deployments not ready"*"omnivec-api 0/2"*) ok "pending -> unready deployments listed" ;;
    *)                                            bad "pending -> unready deployments listed" "got: $out" ;;
esac
case "$out" in
    *"services waiting for external IP"*) ok "pending -> pending LB surfaced" ;;
    *)                                    bad "pending -> pending LB surfaced" "got: $out" ;;
esac
case "$out" in
    *"FailedScheduling"*) ok "pending -> warning events shown" ;;
    *)                    bad "pending -> warning events shown" "got: $out" ;;
esac

# Scenario 3: half-ready
out=$(KUBECTL_MOCK_SCENARIO=half run_heartbeat)
case "$out" in
    *"omnivec-api 1/2"*) ok "half -> partial ready count shown" ;;
    *)                   bad "half -> partial ready count shown" "got: $out" ;;
esac

# Scenario 4: postprovision.sh wires the new markers
if grep -q 'deployments not ready' "$REPO_ROOT/hooks/postprovision.sh" \
  && grep -q 'services waiting for external IP' "$REPO_ROOT/hooks/postprovision.sh" \
  && grep -q 'all resources ready' "$REPO_ROOT/hooks/postprovision.sh"; then
    ok "postprovision.sh wires new heartbeat markers"
else
    bad "postprovision.sh wires new heartbeat markers" "one marker missing"
fi

printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
