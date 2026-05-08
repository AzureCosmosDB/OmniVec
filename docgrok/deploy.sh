#!/bin/bash
# DocGrok Deployment Script

set -e

KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
ACR_NAME="<internal-acr>.azurecr.io"
VERSION="${1:-v1}"

echo "=== DocGrok Deployment ==="
echo "ACR: $ACR_NAME"
echo "Version: $VERSION"

# Login to ACR
echo ""
echo "=== Logging into ACR ==="
az acr login --name <internal-acr>

# Build and push images
echo ""
echo "=== Building DocGrok Orchestrator ==="
docker build -t $ACR_NAME/docgrok:$VERSION -f Dockerfile .
docker push $ACR_NAME/docgrok:$VERSION

echo ""
echo "=== Building DSE-Qwen2 Embedding Service ==="
docker build -t $ACR_NAME/docgrok-dse-qwen2:$VERSION -f services/embedding/dse-qwen2/Dockerfile services/embedding/dse-qwen2/
docker push $ACR_NAME/docgrok-dse-qwen2:$VERSION

echo ""
echo "=== Building CLIP Embedding Service ==="
docker build -t $ACR_NAME/docgrok-clip:$VERSION -f services/embedding/clip/Dockerfile services/embedding/clip/
docker push $ACR_NAME/docgrok-clip:$VERSION

# Deploy to Kubernetes
echo ""
echo "=== Creating namespace ==="
kubectl --context "$KUBE_CONTEXT" apply -f k8s/namespace.yaml

echo ""
echo "=== Deploying backend services ==="
kubectl --context "$KUBE_CONTEXT" apply -f k8s/dse-qwen2.yaml
kubectl --context "$KUBE_CONTEXT" apply -f k8s/clip.yaml

echo ""
echo "=== Deploying DocGrok orchestrator ==="
kubectl --context "$KUBE_CONTEXT" apply -f k8s/docgrok.yaml

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Check status with:"
echo "  kubectl get pods -n docgrok"
echo "  kubectl get svc -n docgrok"
echo ""
echo "Get external IP:"
echo "  kubectl get svc docgrok -n docgrok -o jsonpath='{.status.loadBalancer.ingress[0].ip}'"
