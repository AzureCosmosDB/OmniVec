# DocGrok Operations Guide

> DocGrok is deployed as a Helm subchart of OmniVec. The canonical chart lives
> at `helm/docgrok/` and is pulled in by `helm/omnivec/Chart.yaml`. The standard
> deployment path is `helm install/upgrade omnivec ./helm/omnivec` from the
> repository root — that brings up DocGrok along with the rest of the platform.
>
> Service endpoints are environment-specific; resolve them at runtime with
> `kubectl get svc -n docgrok` rather than hard-coded IPs.

## Test Request (replace `<DOCGROK_HOST>` with your service IP/DNS)
```bash
curl -X POST http://<DOCGROK_HOST>/embed \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "123",
    "blobUrl": "https://example.com/document.pdf",
    "expectedEtag": "123",
    "contentTypeHint": "application/pdf"
  }'
```

---

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

DocGrok images are built and pushed by the OmniVec CI pipeline
(`.github/workflows/build-images.yml`). To build locally against your own
registry, set `ACR=<your-registry>.azurecr.io` and run:

```bash
az acr login --name "${ACR%%.*}"

# From repository root
docker build -t $ACR/docgrok:$VERSION              -f docgrok/Dockerfile docgrok/
docker build -t $ACR/docgrok-dse-qwen2:$VERSION    -f docgrok/services/embedding/dse-qwen2/Dockerfile docgrok/services/embedding/dse-qwen2/
docker build -t $ACR/docgrok-clip:$VERSION         -f docgrok/services/embedding/clip/Dockerfile      docgrok/services/embedding/clip/

docker push $ACR/docgrok:$VERSION
docker push $ACR/docgrok-dse-qwen2:$VERSION
docker push $ACR/docgrok-clip:$VERSION

# Roll out via the OmniVec umbrella chart
helm upgrade omnivec ./helm/omnivec --set docgrok.image.tag=$VERSION
```

## API Endpoints

Resolve `<DOCGROK_HOST>` from `kubectl get svc -n docgrok` first, then:

```bash
# Health check
curl http://<DOCGROK_HOST>/health

# Stats
curl http://<DOCGROK_HOST>/stats

# Single embed request
curl -X POST http://<DOCGROK_HOST>/embed \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "test-123",
    "blobUrl": "https://example.com/document.pdf",
    "expectedEtag": "abc123"
  }'

# Batch embed (sync)
curl -X POST http://<DOCGROK_HOST>/embed/batch \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {"requestId": "1", "blobUrl": "https://example.com/doc1.pdf", "expectedEtag": ""},
      {"requestId": "2", "blobUrl": "https://example.com/doc2.pdf", "expectedEtag": ""}
    ]
  }'

# Batch embed (async)
curl -X POST http://<DOCGROK_HOST>/embed/batch/async \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [...]
  }'

# Check batch status
curl http://<DOCGROK_HOST>/embed/batch/{batch_id}/status
```

## Troubleshooting

```bash
# Pod not starting - check events
kubectl describe pod -n docgrok <pod-name>

# OOM issues - check resource usage
kubectl top pods -n docgrok

# Image pull errors - verify ACR login
az acr login --name "<your-acr-name>"

# Network issues - exec into pod
kubectl exec -it -n docgrok <pod-name> -- /bin/bash

# Check if backends reachable from docgrok
kubectl exec -it -n docgrok -l app=docgrok -- curl http://dse-qwen2-svc:8000/health
```

## Directory Structure

```
<repo-root>/
├── docgrok/
│   ├── api.py                          # Orchestrator
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── OPERATIONS.md                   # This file
│   └── services/embedding/
│       ├── dse-qwen2/                  # PDF embeddings
│       └── clip/                       # Image embeddings
└── helm/
    ├── omnivec/                        # Umbrella chart (entry point)
    └── docgrok/                        # Canonical DocGrok subchart
        ├── Chart.yaml
        ├── values.yaml                 # Model registry
        └── templates/
```
