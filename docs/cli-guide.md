# OmniVec CLI Reference

The OmniVec CLI (`omnivec`) is a standalone binary for managing the OmniVec platform from the command line.

---

## Installation

| Platform | Binary |
|----------|--------|
| Linux (amd64) | `omnivec-linux-amd64` |
| macOS (Apple Silicon) | `omnivec-darwin-arm64` |
| macOS (Intel) | `omnivec-darwin-amd64` |
| Windows (amd64) | `omnivec-windows-amd64.exe` |

```bash
# Make executable (Linux/macOS)
chmod +x omnivec
sudo mv omnivec /usr/local/bin/

# Or build from source (requires Go 1.24+)
cd cli && go build -o ../bin/omnivec . && cd ..
```

---

## Configuration

```bash
# Set the server URL
omnivec config set server http://<omnivec-url>

# Set auth token
omnivec config set token <admin-token>

# View current config
omnivec config view
```

**Server URL resolution order:**
1. `--server` flag (highest priority)
2. `OMNIVEC_SERVER` environment variable
3. `~/.omnivec/config.yaml`
4. Default: `http://localhost:8080`

---

## Global Flags

| Flag | Short | Description | Default |
|------|-------|-------------|---------|
| `--server` | `-s` | OmniVec server URL | from config |
| `--output` | `-o` | Output format: `table`, `json`, `yaml` | `table` |
| `--help` | `-h` | Show help for any command | |

The CLI auto-detects ID prefixes: `omnivec source show abc123` is the same as `omnivec source show src-abc123`.

---

## Sources

A source is a connection to a data store. Sources store **connection info only** — content extraction config is on the pipeline.

```bash
# List all sources
omnivec source list

# Show source details
omnivec source show <id>

# Create a blob storage source
omnivec source create \
  --name "My Documents" \
  --type azure-blob \
  --config '{"account_url":"https://myaccount.blob.core.windows.net","container":"documents"}'

# Create a CosmosDB source
omnivec source create \
  --name "My CosmosDB" \
  --type cosmosdb \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"mydb","container":"docs"}'

# Create from a JSON file
omnivec source create --name "My Source" --type azure-blob -f source-config.json

# Test a source connection
omnivec source test <id>

# Trigger a source sync (re-scan and create jobs)
omnivec source sync <id>
omnivec source sync <id> --full   # reprocess all documents

# Update a source
omnivec source update <id> --name "Renamed Source"

# Delete a source
omnivec source delete <id> -y
```

**Config fields by source type:**

| Type | Required Fields | Optional Fields |
|------|----------------|----------------|
| `azure-blob` | `account_url`, `container` | `prefix` |
| `cosmosdb` | `endpoint`, `database`, `container` | `query` |
| `postgresql` | `host`, `database`, `table` | `port`, `user`, `password`, `ssl_mode` |
| `s3` | `bucket` | `prefix`, `region` |
| `http` | `url` | `method`, `headers`, `auth_type` |

> **Note:** Content extraction config (`content_fields`, `content_mode`, `file_types`) is configured per-pipeline on the pipeline source entry, not on the source itself.

---

## Destinations

A destination is where vector embeddings are stored.

```bash
# List destinations (aliases: destination, dst, destinations)
omnivec dest list

# Show destination details
omnivec dest show <id>

# Create a CosmosDB vector destination
omnivec dest create \
  --name "Production Vectors" \
  --type cosmosdb-vector \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"vectors","container":"embeddings"}'

# Test a destination connection (also returns vector indexes)
omnivec dest test <id>

# Update / Delete
omnivec dest update <id> --name "Renamed"
omnivec dest delete <id> -y
```

When you test a destination, it returns the container's **vector indexing policy** — the available embedding paths with dimensions, distance function, and index type. Use one of these paths as `--vector-index-path` when creating a pipeline.

---

## Pipelines

A pipeline connects a source to a destination through an embedding model.

```bash
# List pipelines (alias: pip)
omnivec pipeline list

# Show pipeline details
omnivec pipeline show <id>

# Create a pipeline
omnivec pipeline create \
  --name "My Pipeline" \
  --source <src-id> \
  --destination <dst-id> \
  --model text-azure \
  --content-fields content \
  --vector-index-path /embedding \
  --process-existing

# Update a pipeline
omnivec pipeline update <id> --name "Renamed" --description "Updated"

# Pause / Resume / Run / Reset / Delete
omnivec pipeline pause <id>
omnivec pipeline resume <id>
omnivec pipeline run <id>
omnivec pipeline reset <id> -y
omnivec pipeline delete <id> -y
```

**Pipeline create flags:**

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes | — | Pipeline name |
| `--source` | Yes | — | Source ID |
| `--destination` | Yes | — | Destination ID |
| `--model` | Yes | — | DocGrok pipeline name (e.g., `text-azure`, `pdf-vision`) |
| `--content-fields` | No | `content` | Comma-separated field names to embed |
| `--vector-index-path` | No | — | Vector index path from destination's vector policy (e.g., `/embedding`) |
| `--description` | No | — | Pipeline description |
| `--process-existing` | No | true | Process existing documents on creation |
| `--no-process-existing` | No | — | Only process new documents going forward |

---

## Jobs

Jobs are individual document processing units created automatically by the controller.

```bash
# List jobs (with filters)
omnivec job list
omnivec job list --pipeline <id>
omnivec job list --status failed
omnivec job list --pipeline <id> --status completed --limit 20

# Show job details
omnivec job show <id>

# Job statistics
omnivec job stats

# Cancel a pending job / Retry a failed job
omnivec job cancel <id>
omnivec job retry <id>
```

**Job statuses:** `pending`, `processing`, `completed`, `failed`, `cancelled`

---

## Deployments

Manage OmniVec Kubernetes deployments.

```bash
# List deployments (aliases: deploy, deployments)
omnivec deployment list

# Scale a deployment
omnivec deployment scale omnivec-worker --replicas 5

# Pause (scale to 0) / Resume (scale to 1)
omnivec deployment pause omnivec-worker
omnivec deployment resume omnivec-worker

# Rolling restart
omnivec deployment restart omnivec-worker -y
```

**Deployment names:** `omnivec-api`, `omnivec-controller`, `omnivec-worker`

---

## Models

Manage embedding models in DocGrok.

```bash
# List models
omnivec model list

# Add an Azure OpenAI model
omnivec model add \
  --provider my-azure-openai \
  --type azure-openai \
  --endpoint https://myresource.openai.azure.com \
  --api-key sk-... \
  --api-version 2024-06-01 \
  --model text-embedding-3-large \
  --dimensions 3072

# List external providers
omnivec model providers

# Start / Stop / Restart / Scale a model
omnivec model start bge
omnivec model stop bge
omnivec model restart bge
omnivec model scale bge --replicas 2

# View model logs
omnivec model logs bge
omnivec model logs bge --lines 500

# Delete an external provider
omnivec model delete my-azure-openai -y
```

| Flag | Required | Description |
|------|----------|-------------|
| `--provider` | Yes | Unique name for this provider |
| `--type` | Yes | `azure-openai`, `openai`, `cohere`, `custom` |
| `--endpoint` | Yes | Endpoint URL |
| `--model` | Yes | Model or deployment name |
| `--api-key` | No | API key |
| `--api-version` | No | API version (required for Azure OpenAI) |
| `--dimensions` | No | Embedding dimensions |

---

## Transform Pipelines

Multi-step processing chains in DocGrok (PDF extraction, text chunking, embedding).

```bash
# List transform pipelines (alias: tp)
omnivec transform list

# Show details
omnivec transform show text-azure

# Create from JSON file / inline JSON
omnivec transform create -f pipeline.json
omnivec transform create --config '{"name":"my-pipeline","description":"Custom chain","steps":[...]}'

# Update / Delete
omnivec transform update text-azure -f updated.json
omnivec transform delete my-pipeline -y
```

---

## Vector Search

```bash
# Search a vector index
omnivec search "how does authentication work" --index <dst-id> --top-k 5

# JSON output for scripting
omnivec search "kubernetes scaling" --index <dst-id> -o json
```

| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--index` | `-i` | Yes | Destination ID (vector store to search) |
| `--top-k` | `-k` | No | Number of results (default: 5) |

---

## System Status

```bash
omnivec status
```

Returns platform health, resource counts (sources, destinations, pipelines), and job statistics.

---

## Settings

```bash
# View system settings
omnivec settings list

# Update a setting
omnivec settings set <key> <value>
```

---

## Output Formats

Every command supports `-o table` (default), `-o json`, and `-o yaml`.

```bash
# JSON for scripting with jq
omnivec source list -o json | jq '.[].name'

# YAML
omnivec pipeline list -o yaml
```

---

## Walkthrough: End-to-End Example

```bash
# 1. Check the system
omnivec status

# 2. Add an external embedding model
omnivec model add --provider azure-openai-prod --type azure-openai \
  --endpoint https://myresource.openai.azure.com --api-key YOUR_KEY \
  --api-version 2024-06-01 --model text-embedding-3-large --dimensions 3072

# 3. Create a source
omnivec source create --name "Product Docs" --type azure-blob \
  --config '{"account_url":"https://mystore.blob.core.windows.net","container":"docs"}'
omnivec source test src-abc12345

# 4. Create a destination
omnivec dest create --name "Vectors" --type cosmosdb-vector \
  --config '{"endpoint":"https://myvectors.documents.azure.com","database":"vectors","container":"embeddings"}'
omnivec dest test dst-def67890

# 5. Create a pipeline (with content-fields and vector-index-path)
omnivec pipeline create --name "Product Pipeline" --source src-abc12345 \
  --destination dst-def67890 --model text-azure \
  --content-fields content --vector-index-path /embedding --process-existing

# 6. Monitor progress
omnivec job list --pipeline pip-ghi11111
omnivec job stats

# 7. Scale workers if needed
omnivec deployment scale omnivec-worker --replicas 5

# 8. Search
omnivec search "product features" --index dst-def67890 --top-k 5
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `connection refused` | Server unreachable | Check `omnivec config view`, verify the URL is correct |
| `401 Unauthorized` | Invalid or missing token | Run `omnivec config set token <token>` |
| `source not found` | Wrong ID | Use `omnivec source list` to get the correct ID |
| No jobs created | Pipeline paused | Run `omnivec pipeline resume <id>` |
| Jobs stuck in pending | Workers at 0 replicas | `omnivec deployment scale omnivec-worker --replicas 1` |
