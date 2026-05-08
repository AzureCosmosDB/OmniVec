# DocGrok Operations Guide

## Working Endpoint (Direct DSE-Qwen2 MS API)
- **URL**: http://20.241.169.200
- **Health**: http://20.241.169.200/health
- **Docs**: http://20.241.169.200/docs

## Test Request
```bash
curl -X POST http://20.241.169.200/embed \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "123",
    "blobUrl": "https://example.com/document.pdf",
    "expectedEtag": "123",
    "contentTypeHint": "application/pdf"
  }'
```

---

## DocGrok Orchestrator (TODO: Fix asyncio issue)
- **URL**: TBD (not deployed)
- **Health**: TBD

## Helm Commands

```bash
# Install
helm install docgrok ../helm/docgrok

# Upgrade after changes
helm upgrade docgrok ../helm/docgrok

# Uninstall
helm uninstall docgrok

# View release status
helm status docgrok

# View what will be deployed (dry-run)
helm template docgrok ../helm/docgrok
```

## Scaling

```bash
# Scale DocGrok orchestrator
kubectl scale deployment docgrok -n docgrok --replicas=3

# Scale DSE-Qwen2 (PDF embeddings)
kubectl scale deployment dse-qwen2 -n docgrok --replicas=2

# Scale CLIP (image embeddings)
kubectl scale deployment clip -n docgrok --replicas=2

# Scale to zero (disable)
kubectl scale deployment clip -n docgrok --replicas=0
```

## Enable/Disable Models

```bash
# Disable CLIP at deploy time
helm upgrade docgrok ../helm/docgrok --set models.embedding.clip.enabled=false

# Enable OCR model
helm upgrade docgrok ../helm/docgrok --set models.ocr.doctr.enabled=true

# Multiple changes
helm upgrade docgrok ../helm/docgrok \
  --set models.embedding.clip.replicaCount=1 \
  --set models.embedding.dse-qwen2.replicaCount=2
```

## Monitoring

```bash
# Check all pods
kubectl get pods -n docgrok

# Check services
kubectl get svc -n docgrok

# View pod logs
kubectl logs -n docgrok -l app=docgrok --tail=100
kubectl logs -n docgrok -l app=dse-qwen2 --tail=100
kubectl logs -n docgrok -l app=clip --tail=100

# Follow logs
kubectl logs -n docgrok -l app=docgrok -f

# Describe pod (for troubleshooting)
kubectl describe pod -n docgrok -l app=docgrok
```

## Restart Deployments

```bash
# Restart DocGrok
kubectl rollout restart deployment docgrok -n docgrok

# Restart all
kubectl rollout restart deployment -n docgrok
```

## Docker Build & Push

```bash
# Login to ACR
az acr login --name cdbmvsacr4cc259

# Build images
cd /home/cdbmvs/docgrok
sudo docker build -t cdbmvsacr4cc259.azurecr.io/docgrok:v1 -f Dockerfile .
sudo docker build -t cdbmvsacr4cc259.azurecr.io/docgrok-dse-qwen2:v1 -f services/embedding/dse-qwen2/Dockerfile services/embedding/dse-qwen2/
sudo docker build -t cdbmvsacr4cc259.azurecr.io/docgrok-clip:v1 -f services/embedding/clip/Dockerfile services/embedding/clip/

# Push images
sudo docker push cdbmvsacr4cc259.azurecr.io/docgrok:v1
sudo docker push cdbmvsacr4cc259.azurecr.io/docgrok-dse-qwen2:v1
sudo docker push cdbmvsacr4cc259.azurecr.io/docgrok-clip:v1

# Build and deploy new version
VERSION=v2
sudo docker build -t cdbmvsacr4cc259.azurecr.io/docgrok:$VERSION -f Dockerfile .
sudo docker push cdbmvsacr4cc259.azurecr.io/docgrok:$VERSION
helm upgrade docgrok ../helm/docgrok --set docgrok.image.tag=$VERSION
```

## API Endpoints

```bash
# Health check
curl http://52.191.234.73/health

# Stats
curl http://52.191.234.73/stats

# Single embed request
curl -X POST http://52.191.234.73/embed \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "test-123",
    "blobUrl": "https://example.com/document.pdf",
    "expectedEtag": "abc123"
  }'

# Batch embed (sync)
curl -X POST http://52.191.234.73/embed/batch \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {"requestId": "1", "blobUrl": "https://example.com/doc1.pdf", "expectedEtag": ""},
      {"requestId": "2", "blobUrl": "https://example.com/doc2.pdf", "expectedEtag": ""}
    ]
  }'

# Batch embed (async)
curl -X POST http://52.191.234.73/embed/batch/async \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [...]
  }'

# Check batch status
curl http://52.191.234.73/embed/batch/{batch_id}/status
```

## Troubleshooting

```bash
# Pod not starting - check events
kubectl describe pod -n docgrok <pod-name>

# OOM issues - check resource usage
kubectl top pods -n docgrok

# Image pull errors - verify ACR login
az acr login --name cdbmvsacr4cc259

# Network issues - exec into pod
kubectl exec -it -n docgrok <pod-name> -- /bin/bash

# Check if backends reachable from docgrok
kubectl exec -it -n docgrok -l app=docgrok -- curl http://dse-qwen2-svc:8000/health
```

## Directory Structure

```
/home/cdbmvs/omnivec/
├── docgrok/
│   ├── api.py                          # Orchestrator
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── OPERATIONS.md                   # This file
│   └── services/embedding/
│       ├── dse-qwen2/                  # PDF embeddings
│       └── clip/                       # Image embeddings
└── helm/docgrok/                       # Canonical Helm chart
    ├── Chart.yaml
    ├── values.yaml                     # Model registry
    └── templates/
```
