#!/bin/bash
# Benchmark run: delete leases, reset, scale to 10, measure, scale down
set -e

KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
RUN=$1
echo "=========================================="
echo "  RUN $RUN"
echo "=========================================="

# Delete lease containers
echo "[$(date -u +%H:%M:%S)] Deleting lease containers..."
for lid in $(az cosmosdb sql container list --account-name omnivec-cosmos -g cdb-mvs-rg --database-name omnivec --query "[?starts_with(name, 'leases-')].name" -o tsv 2>/dev/null); do
    az cosmosdb sql container delete --account-name omnivec-cosmos -g cdb-mvs-rg --database-name omnivec --name "$lid" --yes 2>/dev/null
done

# Reset pipeline
echo "[$(date -u +%H:%M:%S)] Resetting pipeline..."
curl -s -X POST "http://20.242.139.166/api/pipelines/pip-27bbd3f9/reset" > /dev/null

# Scale up
echo "[$(date -u +%H:%M:%S)] Scaling to 10 pods..."
kubectl --context "$KUBE_CONTEXT" scale deployment/omnivec-changefeed -n omnivec --replicas=10 > /dev/null
kubectl --context "$KUBE_CONTEXT" rollout status deployment/omnivec-changefeed -n omnivec --timeout=120s > /dev/null 2>&1

# Wait for processing to start (first Inline embed)
echo "[$(date -u +%H:%M:%S)] Waiting for processing to start..."
for i in $(seq 1 60); do
    started=0
    for pod in $(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l app=omnivec-changefeed -o jsonpath='{.items[*].metadata.name}'); do
        if kubectl --context "$KUBE_CONTEXT" logs $pod -n omnivec --timestamps 2>&1 | grep -q "Inline embed"; then
            started=1
            break
        fi
    done
    if [ "$started" -eq 1 ]; then break; fi
    sleep 5
done
echo "[$(date -u +%H:%M:%S)] Processing started"

# Wait for completion — check every 15s, done when total stops growing
prev_total=0
stable_count=0
while true; do
    sleep 15
    total=0
    for pod in $(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l app=omnivec-changefeed -o jsonpath='{.items[*].metadata.name}'); do
        count=$(kubectl --context "$KUBE_CONTEXT" logs $pod -n omnivec --timestamps 2>&1 | grep "Inline complete" | grep -oP '\d+/\d+ docs' | awk -F'/' '{sum += $1} END {print sum+0}')
        total=$((total + count))
    done
    echo "[$(date -u +%H:%M:%S)] Progress: $total patched"

    if [ "$total" -gt 0 ] && [ "$total" -eq "$prev_total" ]; then
        stable_count=$((stable_count + 1))
        if [ "$stable_count" -ge 2 ]; then
            break
        fi
    else
        stable_count=0
    fi
    prev_total=$total
done

# Collect results
first_ts=""
last_ts=""
errors=0
for pod in $(kubectl --context "$KUBE_CONTEXT" get pods -n omnivec -l app=omnivec-changefeed -o jsonpath='{.items[*].metadata.name}'); do
    ft=$(kubectl --context "$KUBE_CONTEXT" logs $pod -n omnivec --timestamps 2>&1 | grep "Inline embed" | head -1 | awk '{print $1}')
    lt=$(kubectl --context "$KUBE_CONTEXT" logs $pod -n omnivec --timestamps 2>&1 | grep "Inline complete" | tail -1 | awk '{print $1}')
    e=$(kubectl --context "$KUBE_CONTEXT" logs $pod -n omnivec --timestamps 2>&1 | grep "Inline complete" | grep -v "0 failed" | wc -l)
    errors=$((errors + e))
    if [ -n "$ft" ]; then
        if [ -z "$first_ts" ] || [[ "$ft" < "$first_ts" ]]; then first_ts="$ft"; fi
    fi
    if [ -n "$lt" ]; then
        if [ -z "$last_ts" ] || [[ "$lt" > "$last_ts" ]]; then last_ts="$lt"; fi
    fi
done

# Calculate duration
start_sec=$(date -d "${first_ts%Z}" +%s 2>/dev/null || echo 0)
end_sec=$(date -d "${last_ts%Z}" +%s 2>/dev/null || echo 0)
duration=$((end_sec - start_sec))
if [ "$duration" -gt 0 ]; then
    rate=$((total / duration))
else
    rate=0
fi

echo ""
echo "RUN $RUN RESULT: $total patched in ${duration}s = ${rate} docs/sec (errors=$errors)"
echo "$RUN,$total,$duration,$rate,$errors" >> /home/cdbmvs/omnivec/scripts/bench_results.csv

# Scale down
kubectl --context "$KUBE_CONTEXT" scale deployment/omnivec-changefeed -n omnivec --replicas=0 > /dev/null
kubectl --context "$KUBE_CONTEXT" rollout status deployment/omnivec-changefeed -n omnivec --timeout=30s > /dev/null 2>&1
echo "[$(date -u +%H:%M:%S)] Scaled down"
echo ""
