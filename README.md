# OmniVec

**Any Document → Vector, From Anywhere**

OmniVec is a universal vector ingestion platform that processes documents from any source, in any format, and indexes them into Azure CosmosDB for vector search.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        OMNIVEC                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Sources          Processing           Destinations        │
│   ────────         ──────────           ────────────        │
│   Blob Storage ─┐                    ┌→ CosmosDB Vector     │
│   CosmosDB     ─┼─→  DocGrok    ────┤                       │
│   Postgres     ─┤    Pipelines      └→ pgvector             │
│   MS SQL       ─┘                                           │
│                                                             │
│   Formats: PDF, Images, Text, Office, Audio, Video, ...    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Multi-Source Ingestion**: Blob Storage, CosmosDB, Postgres, MS SQL
- **Live + Batch Processing**: Event Grid, Change Feed for real-time; scheduled scans for existing data
- **Any Format**: PDF, Images, Text, Office docs, and more
- **Flexible Pipelines**: Chain transformations via DocGrok
- **Vector Destinations**: CosmosDB Vector Search, pgvector
- **Web UI**: Visual pipeline builder and monitoring

## Components

- `api/` - Control plane API (FastAPI)
- `web/` - Web UI for pipeline management
- `connectors/ingestion/dotnet/` - .NET source ingestion connector (multi-source watcher)
- `connectors/worker/dotnet/` - .NET embedding worker
- `docgrok/` - Document intelligence engine (submodule)
- `cli/` - Go CLI for managing pipelines, sources, and jobs
- `infra/` - Azure Bicep infrastructure as code
- `helm/` - Kubernetes deployment charts

## Quick Start

### Option 1: Azure Developer CLI (Recommended)

```bash
# Login to Azure
azd auth login

# Deploy everything (infrastructure + application)
azd up
```

This provisions all Azure resources (AKS, CosmosDB, ACR, Storage) and deploys OmniVec via Helm in one command.

### Option 2: Manual Deployment

#### 1. Deploy Infrastructure

```bash
cd infra
az deployment sub create \
  --location eastus \
  --template-file main.bicep \
  --parameters main.parameters.json
```

#### 2. Deploy OmniVec

```bash
helm install omnivec ./helm/omnivec -n omnivec --create-namespace
```

#### 3. Access UI

```bash
kubectl port-forward svc/omnivec-web 8080:80 -n omnivec
open http://localhost:8080/ui
```

## License

MIT
