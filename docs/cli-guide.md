# OmniVec CLI Guide

The OmniVec CLI (`omnivec`) is a standalone command-line tool for managing the OmniVec platform. It is a single binary with no dependencies — download it and run.

---

## Table of Contents

1. [Installation](#1-installation)
2. [Configuration](#2-configuration)
3. [Global Flags](#3-global-flags)
4. [Key Concepts](#4-key-concepts)
5. [Sources](#5-sources)
6. [Destinations (Vector Stores)](#6-destinations-vector-stores)
7. [Pipelines](#7-pipelines)
8. [Jobs](#8-jobs)
9. [Deployments (Operations)](#9-deployments-operations)
10. [Models](#10-models)
11. [Transform Pipelines](#11-transform-pipelines)
12. [Vector Search](#12-vector-search)
13. [System Status](#13-system-status)
14. [Output Formats](#14-output-formats)
15. [Walkthrough: End-to-End Example](#15-walkthrough-end-to-end-example)
16. [Command Reference](#16-command-reference)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Installation

Download the binary for your platform:

| Platform | Binary |
|----------|--------|
| Linux (amd64) | `omnivec` |
| Windows (amd64) | `omnivec-windows-amd64.exe` |
| macOS (Apple Silicon) | `omnivec-darwin-arm64` |

Make it executable (Linux/macOS):

```bash
chmod +x omnivec
```

Optionally move it to your PATH:

```bash
sudo mv omnivec /usr/local/bin/
```

Verify installation:

```bash
omnivec --help
```

---

## 2. Configuration

### Set the server URL

Before using the CLI, point it at your OmniVec server:

```bash
omnivec config set server http://<external-ip>
```

This saves the URL to `~/.omnivec/config.yaml`.

### View current configuration

```bash
omnivec config view
```

Output:

```
Server:  http://<external-ip>
Source:  /home/user/.omnivec/config.yaml
Config:  /home/user/.omnivec/config.yaml
```

### Server URL resolution order

The CLI resolves the server URL in this order:

1. `--server` flag (highest priority)
2. `OMNIVEC_SERVER` environment variable
3. `~/.omnivec/config.yaml` file
4. Default: `http://localhost:8080`

Example using the flag:

```bash
omnivec --server http://10.0.0.5:8080 source list
```

Example using environment variable:

```bash
export OMNIVEC_SERVER=http://10.0.0.5:8080
omnivec source list
```

---

## 3. Global Flags

These flags work with every command:

| Flag | Short | Description | Default |
|------|-------|-------------|---------|
| `--server` | `-s` | OmniVec server URL | from config |
| `--output` | `-o` | Output format: `table`, `json`, `yaml` | `table` |
| `--help` | `-h` | Show help for any command | |

---

## 4. Key Concepts

| Concept | Description |
|---------|-------------|
| **Source** | Where raw documents live (Azure Blob Storage or CosmosDB) |
| **Destination** | Where vector embeddings are stored (CosmosDB with vector indexing) |
| **Pipeline** | Connects a source to a destination through an embedding model |
| **Job** | A single document being processed (tracks status and errors) |
| **Model** | An embedding engine — internal (hosted in cluster) or external (Azure OpenAI, etc.) |
| **Transform Pipeline** | A multi-step processing chain in DocGrok (e.g., PDF → text → embedding) |
| **Deployment** | A Kubernetes deployment (API, Controller, or Worker) |

ID prefixes: sources start with `src-`, destinations with `dst-`, pipelines with `pip-`, jobs with `job-`.

The CLI auto-detects prefixes — `omnivec source show abc123` is the same as `omnivec source show src-abc123`.

---

## 5. Sources

A source is a data store that OmniVec reads documents from.

### List all sources

```bash
omnivec source list
```

```
ID            NAME           TYPE        ENABLED  UPDATED
src-775207ff  Mysrc          cosmosdb    Yes      3d ago
src-f9c8e574  BlobData       azure-blob  Yes      2d ago
src-15ab4952  NewBlobSource  azure-blob  Yes      13h ago
```

### Show source details

```bash
omnivec source show src-15ab4952
```

```
name:        NewBlobSource
type:        azure-blob
enabled:     Yes
config:      {
    "account_url": "https://blobomnivectest.blob.core.windows.net",
    "container": "documents"
  }
triggers:    []
created_at:  2026-02-12T14:12:23.425177
updated_at:  2026-02-12T18:04:17.828406
```

### Create a source

**Azure Blob Storage:**

```bash
omnivec source create \
  --name "My Documents" \
  --type azure-blob \
  --config '{"account_url":"https://myaccount.blob.core.windows.net","container":"documents"}'
```

**CosmosDB:**

```bash
omnivec source create \
  --name "My CosmosDB" \
  --type cosmosdb \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"mydb","container":"docs"}'
```

**From a JSON file:**

```bash
omnivec source create --name "My Source" --type azure-blob -f source-config.json
```

Where `source-config.json` contains:

```json
{
  "account_url": "https://myaccount.blob.core.windows.net",
  "container": "documents",
  "prefix": "uploads/"
}
```

Output:

```
OK: Source created: src-2b3356ab
```

### Config fields by source type

**Azure Blob Storage (`azure-blob`):**

| Field | Required | Description |
|-------|----------|-------------|
| `account_url` | Yes | Storage account URL (e.g., `https://myaccount.blob.core.windows.net`) |
| `container` | Yes | Blob container name |
| `prefix` | No | Only process blobs matching this prefix |

**CosmosDB (`cosmosdb`):**

| Field | Required | Description |
|-------|----------|-------------|
| `endpoint` | Yes | CosmosDB account endpoint |
| `database` | Yes | Database name |
| `container` | Yes | Container name |

> **Note:** Content extraction config (`content_mode`, `content_fields`) is now configured per-pipeline on the pipeline source entry, not on the source itself.

### Update a source

```bash
omnivec source update src-2b3356ab --name "Renamed Source"
```

The CLI fetches the current state, applies your changes, and sends the update. You only need to specify the fields you want to change.

### Test a source connection

```bash
omnivec source test src-15ab4952
```

```
Testing connection to src-15ab4952...
OK: Connection successful
{
  "container": "documents",
  "last_modified": "2026-02-12 05:17:24+00:00",
  "sample_count": 10,
  "status": "connected"
}
```

### Trigger a source sync

Force the controller to re-scan the source and create jobs for unprocessed documents:

```bash
omnivec source sync src-15ab4952
```

Full re-sync (reprocess all documents):

```bash
omnivec source sync src-15ab4952 --full
```

### Delete a source

```bash
omnivec source delete src-2b3356ab
```

```
Delete source src-2b3356ab? [y/N]: y
OK: Source src-2b3356ab deleted
```

Skip the confirmation prompt:

```bash
omnivec source delete src-2b3356ab -y
```

---

## 6. Destinations (Vector Stores)

A destination is where processed vector embeddings are stored.

### List destinations

```bash
omnivec destination list
```

Aliases: `dest`, `dst`, `destinations`

```
ID            NAME        TYPE             ENABLED  UPDATED
dst-1f872959  MyDest      cosmosdb-vector  Yes      3d ago
dst-a7281267  MyNewDest   cosmosdb-vector  Yes      1d ago
dst-fc8e5bb4  NewVector2  cosmosdb-vector  Yes      17h ago
```

### Show destination details

```bash
omnivec dest show dst-1f872959
```

### Create a destination

```bash
omnivec dest create \
  --name "Production Vectors" \
  --type cosmosdb-vector \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"vectors","container":"embeddings"}'
```

**Config fields for CosmosDB Vector (`cosmosdb-vector`):**

| Field | Required | Description |
|-------|----------|-------------|
| `endpoint` | Yes | CosmosDB account endpoint |
| `database` | Yes | Database name |
| `container` | Yes | Container name (must have vector indexing enabled) |

### Update a destination

```bash
omnivec dest update dst-1f872959 --name "Renamed Destination"
```

### Test a destination connection

```bash
omnivec dest test dst-1f872959
```

```
Testing connection to dst-1f872959...
OK: Connection successful
{
  "container": "vectors",
  "database": "omnivec",
  "has_vector_policy": true,
  "status": "connected",
  "vector_indexes": [
    {
      "dataType": "float32",
      "dimensions": 3072,
      "distanceFunction": "cosine",
      "indexType": "quantizedFlat",
      "path": "/embedding"
    }
  ]
}
```

### Delete a destination

```bash
omnivec dest delete dst-abc123 -y
```

---

## 7. Pipelines

A pipeline connects a source to a destination through an embedding model. When active, it automatically processes new documents.

### List pipelines

```bash
omnivec pipeline list
```

Alias: `pip`

```
ID            NAME              STATUS  DESTINATION   PROCESSED  UPDATED
pip-b13acd93  Pipeline          active  dst-1f872959  0          3d ago
pip-0febe9de  BlobPipeline      active  dst-1f872959  113        2d ago
pip-93f251cb  NewBlobPipeline   active  dst-fc8e5bb4  310        14h ago
pip-276b1e48  PreFillPipeline   active  dst-1f872959  100        8h ago
```

### Show pipeline details

```bash
omnivec pipeline show pip-93f251cb
```

### Create a pipeline

```bash
omnivec pipeline create \
  --name "My Pipeline" \
  --source src-15ab4952 \
  --destination dst-1f872959 \
  --model text-azure \
  --content-fields content \
  --process-existing
```

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Pipeline name |
| `--source` | Yes | Source ID |
| `--destination` | Yes | Destination ID |
| `--model` | Yes | DocGrok transform pipeline name (e.g., `text-azure`, `pdf-vision`) |
| `--description` | No | Pipeline description |
| `--content-fields` | No | Comma-separated field names to embed (default: `content`) |
| `--process-existing` | No | Process existing documents on creation (default: true) |
| `--no-process-existing` | No | Only process new documents going forward |

### Update a pipeline

```bash
omnivec pipeline update pip-abc123 --name "Renamed Pipeline" --description "Updated description"
```

### Pause a pipeline

Stop processing new documents:

```bash
omnivec pipeline pause pip-93f251cb
```

```
OK: Pipeline pip-93f251cb paused
```

### Resume a pipeline

Restart processing:

```bash
omnivec pipeline resume pip-93f251cb
```

```
OK: Pipeline pip-93f251cb resumed
```

### Run a pipeline

Activate and trigger processing:

```bash
omnivec pipeline run pip-93f251cb
```

### Delete a pipeline

```bash
omnivec pipeline delete pip-abc123 -y
```

---

## 8. Jobs

A job represents one document being processed. Jobs are created automatically by the controller when a pipeline detects new documents.

### List jobs

```bash
omnivec job list
```

```
ID                PIPELINE      SOURCE REF  STATUS     ERROR  CREATED
job-749c282e-7f7  pip-276b1e48  pre-99.txt  completed  -      8h ago
job-a01d5d3a-602  pip-276b1e48  pre-98.txt  completed  -      8h ago
job-d8bc6dba-36c  pip-276b1e48  pre-97.txt  completed  -      8h ago
```

### Filter jobs

By pipeline:

```bash
omnivec job list --pipeline pip-276b1e48
```

By status:

```bash
omnivec job list --status failed
```

Limit results:

```bash
omnivec job list --limit 10
```

Combine filters:

```bash
omnivec job list --pipeline pip-93f251cb --status completed --limit 20
```

### Show job details

```bash
omnivec job show job-749c282e-7f7
```

### Job statuses

| Status | Meaning |
|--------|---------|
| `pending` | Created, waiting for a worker to pick it up |
| `processing` | Worker is actively processing the document |
| `completed` | Document successfully embedded and stored |
| `failed` | Processing failed (check error field) |
| `cancelled` | Manually cancelled |

### Cancel a pending job

```bash
omnivec job cancel job-abc123
```

Only works for jobs in `pending` status.

### Retry a failed job

```bash
omnivec job retry job-abc123
```

Resets the job to `pending` so a worker picks it up again. Only works for jobs in `failed` status.

### View job statistics

```bash
omnivec job stats
```

```
Total:      786
Pending:    0
Processing: 0
Completed:  786
Failed:     0
```

> **Note:** The `job stats` command requires API version v51 or later. On earlier versions, use `omnivec status` instead.

---

## 9. Deployments (Operations)

Manage the OmniVec Kubernetes deployments: API, Controller, and Worker.

### List deployments

```bash
omnivec deployment list
```

Aliases: `deploy`, `deployments`

```
NAME                READY  STATUS   IMAGE                                       PODS
omnivec-api         1/1    Running  <internal-acr>.azurecr.io/omnivec-api:v50  1
omnivec-controller  1/1    Running  <internal-acr>.azurecr.io/omnivec-api:v49  1
omnivec-worker      7/7    Running  <internal-acr>.azurecr.io/omnivec-api:v49  7
```

### Scale a deployment

Scale the worker to 3 replicas to speed up processing:

```bash
omnivec deployment scale omnivec-worker --replicas 3
```

Scale back to 1 when done:

```bash
omnivec deployment scale omnivec-worker --replicas 1
```

### Pause a deployment (scale to 0)

```bash
omnivec deployment pause omnivec-worker
```

### Resume a deployment (scale to 1)

```bash
omnivec deployment resume omnivec-worker
```

### Restart a deployment (rolling restart)

```bash
omnivec deployment restart omnivec-worker
```

Prompts for confirmation. Skip with `-y`:

```bash
omnivec deployment restart omnivec-worker -y
```

### Deployment names

| Name | Role |
|------|------|
| `omnivec-api` | API server and web UI |
| `omnivec-controller` | Monitors sources, creates jobs |
| `omnivec-worker` | Processes jobs (embeds documents, writes vectors) |

---

## 10. Models

Manage embedding models in the DocGrok engine.

### List models

```bash
omnivec model list
```

```
NAME       SOURCE  TYPE  STATUS
bge        -       -     running
clip       -       -     running
dse-qwen2  -       -     running
```

### Add an external model

Add an Azure OpenAI embedding model:

```bash
omnivec model add \
  --provider my-azure-openai \
  --type azure-openai \
  --endpoint https://myresource.openai.azure.com \
  --api-key sk-... \
  --api-version 2024-06-01 \
  --model text-embedding-3-large \
  --dimensions 3072
```

| Flag | Required | Description |
|------|----------|-------------|
| `--provider` | Yes | Unique name for this provider |
| `--type` | Yes | Provider type: `azure-openai`, `openai`, `cohere`, `custom` |
| `--endpoint` | Yes | Endpoint URL |
| `--model` | Yes | Model or deployment name |
| `--api-key` | No | API key (recommended for external providers) |
| `--api-version` | No | API version (required for Azure OpenAI) |
| `--dimensions` | No | Embedding dimensions (e.g., 3072) |

### List external providers

```bash
omnivec model providers
```

### Start a model

```bash
omnivec model start bge
```

### Stop a model

```bash
omnivec model stop bge
```

### Restart a model

```bash
omnivec model restart bge
```

### Scale a model

```bash
omnivec model scale bge --replicas 2
```

### View model logs

```bash
omnivec model logs bge
```

Show more lines:

```bash
omnivec model logs bge --lines 500
```

### Delete an external provider

```bash
omnivec model delete my-azure-openai -y
```

---

## 11. Transform Pipelines

Transform pipelines define multi-step processing chains in the DocGrok engine (e.g., PDF extraction, text chunking, embedding).

### List transform pipelines

```bash
omnivec transform list
```

Alias: `tp`

```
NAME                                      DESCRIPTION                                         STEPS
pdf-ocr-text                              PDF → OCR → Text Embedding                          3
pdf-vision                                PDF → Screenshot → Vision Embedding                 2
text-azure                                Text → Azure OpenAI Embedding (3072 dims)           1
azure-openai-text-embedding-3-large-auto  Auto-created single-model pipeline using azure-...  1
```

### Show transform pipeline details

```bash
omnivec transform show text-azure
```

### Create a transform pipeline

```bash
omnivec transform create -f pipeline.json
```

Or with inline JSON:

```bash
omnivec transform create --config '{"name":"my-pipeline","description":"Custom chain","steps":[...]}'
```

### Update a transform pipeline

```bash
omnivec transform update text-azure -f updated-pipeline.json
```

### Delete a transform pipeline

```bash
omnivec transform delete my-pipeline -y
```

---

## 12. Vector Search

Search your vector indexes from the command line.

```bash
omnivec search "how does authentication work" --index dst-1f872959 --top-k 5
```

| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--index` | `-i` | Yes | Destination ID (the vector store to search) |
| `--top-k` | `-k` | No | Number of results (default: 5) |

Output:

```
Query: how does authentication work
Timing: embedding 670ms, search 431ms, total 1101ms

#1  95.2%  doc42.txt
    Source: BlobData
    Authentication in microservices typically uses JWT tokens...

#2  87.3%  doc18.txt
    Source: BlobData
    OAuth 2.0 provides delegated authorization...

#3  82.1%  pre-55.txt
    Source: PreFillBlob
    Zero-trust security models require continuous verification...
```

Get results as JSON for scripting:

```bash
omnivec search "kubernetes scaling" --index dst-1f872959 -o json
```

---

## 13. System Status

Get a quick health overview of the platform:

```bash
omnivec status
```

```
OmniVec Platform Status
Service:             healthy
Version:             1.0.0
DocGrok:             healthy

Resources
Sources:             6
Destinations:        3
Pipelines:           6

Processing
Events Processed:    786
Events Failed:       27

Jobs
Total:               786
Pending:             0
Processing:          0
Completed:           786
Failed:              0
```

---

## 14. Output Formats

Every command supports three output formats via the `-o` flag.

### Table (default)

Human-readable, column-aligned, with colored status indicators:

```bash
omnivec pipeline list
```

### JSON

Machine-readable, full detail:

```bash
omnivec pipeline list -o json
```

Useful for scripting with `jq`:

```bash
omnivec source list -o json | jq '.[].name'
```

### YAML

```bash
omnivec pipeline list -o yaml
```

### Detail view

When showing a single resource, table mode displays key-value pairs instead of a table:

```bash
omnivec source show src-15ab4952
```

```
name:        NewBlobSource
type:        azure-blob
enabled:     Yes
config:      {
    "account_url": "https://blobomnivectest.blob.core.windows.net",
    "container": "documents"
  }
```

---

## 15. Walkthrough: End-to-End Example

This walkthrough creates a complete pipeline from scratch — source to vector search.

### Step 1: Check the system

```bash
omnivec status
```

Verify the service is healthy and DocGrok is running.

### Step 2: Add an external embedding model (if needed)

```bash
omnivec model add \
  --provider azure-openai-prod \
  --type azure-openai \
  --endpoint https://myresource.openai.azure.com \
  --api-key YOUR_API_KEY \
  --api-version 2024-06-01 \
  --model text-embedding-3-large \
  --dimensions 3072
```

Verify it's listed:

```bash
omnivec model list
```

### Step 3: Create a source

```bash
omnivec source create \
  --name "Product Docs" \
  --type azure-blob \
  --config '{"account_url":"https://mystore.blob.core.windows.net","container":"docs"}'
```

```
OK: Source created: src-abc12345
```

Test the connection:

```bash
omnivec source test src-abc12345
```

### Step 4: Create a destination

```bash
omnivec dest create \
  --name "Product Vectors" \
  --type cosmosdb-vector \
  --config '{"endpoint":"https://myvectors.documents.azure.com","database":"vectors","container":"embeddings"}'
```

```
OK: Destination created: dst-def67890
```

Test the connection:

```bash
omnivec dest test dst-def67890
```

### Step 5: Create a pipeline

```bash
omnivec pipeline create \
  --name "Product Docs Pipeline" \
  --source src-abc12345 \
  --destination dst-def67890 \
  --model text-azure \
  --process-existing
```

```
OK: Pipeline created: pip-ghi11111 (status: active)
```

### Step 6: Monitor progress

Watch jobs get created and processed:

```bash
omnivec job list --pipeline pip-ghi11111
```

Check overall stats:

```bash
omnivec status
```

If processing is slow, scale up the worker:

```bash
omnivec deployment scale omnivec-worker --replicas 3
```

### Step 7: Search

Once jobs are completed, search your vectors:

```bash
omnivec search "how to configure the product" --index dst-def67890
```

### Step 8: Clean up (when done testing)

Scale workers back down:

```bash
omnivec deployment scale omnivec-worker --replicas 1
```

Pause the pipeline:

```bash
omnivec pipeline pause pip-ghi11111
```

Or delete everything:

```bash
omnivec pipeline delete pip-ghi11111 -y
omnivec dest delete dst-def67890 -y
omnivec source delete src-abc12345 -y
```

---

## 16. Command Reference

### Source commands

```
omnivec source list                                 List all sources
omnivec source show <id>                            Show source details
omnivec source create --name --type --config/--file Create a source
omnivec source update <id> [--name] [--type] [--config/--file]  Update a source
omnivec source delete <id> [-y]                     Delete a source
omnivec source test <id>                            Test source connection
omnivec source sync <id> [--full]                   Trigger source sync
```

### Destination commands

```
omnivec destination list                            List all destinations
omnivec destination show <id>                       Show destination details
omnivec destination create --name --type --config   Create a destination
omnivec destination update <id> [--name] [--config] Update a destination
omnivec destination delete <id> [-y]                Delete a destination
omnivec destination test <id>                       Test destination connection
```

### Pipeline commands

```
omnivec pipeline list                               List all pipelines
omnivec pipeline show <id>                          Show pipeline details
omnivec pipeline create --name --source --destination --model  Create a pipeline
omnivec pipeline update <id> [--name] [--description]          Update a pipeline
omnivec pipeline delete <id> [-y]                   Delete a pipeline
omnivec pipeline pause <id>                         Pause processing
omnivec pipeline resume <id>                        Resume processing
omnivec pipeline run <id>                           Activate/trigger pipeline
```

### Job commands

```
omnivec job list [--pipeline <id>] [--status <s>] [--limit <n>]  List jobs
omnivec job show <id>                               Show job details
omnivec job cancel <id>                             Cancel a pending job
omnivec job retry <id>                              Retry a failed job
omnivec job stats                                   Show job statistics
```

### Deployment commands

```
omnivec deployment list                             List deployments
omnivec deployment scale <name> --replicas <n>      Scale a deployment
omnivec deployment restart <name> [-y]              Rolling restart
omnivec deployment pause <name>                     Scale to 0
omnivec deployment resume <name>                    Scale to 1
```

### Model commands

```
omnivec model list                                  List all models
omnivec model add --provider --type --endpoint --model [--api-key] [--dimensions]  Add external model
omnivec model delete <provider> [-y]                Delete external provider
omnivec model start <name>                          Start/enable a model
omnivec model stop <name>                           Stop/disable a model
omnivec model restart <name>                        Restart a model
omnivec model scale <name> --replicas <n>           Scale a model
omnivec model logs <name> [--lines <n>]             View model logs
omnivec model providers                             List external providers
```

### Transform pipeline commands

```
omnivec transform list                              List transform pipelines
omnivec transform show <name>                       Show pipeline details
omnivec transform create --config/--file            Create a transform pipeline
omnivec transform update <name> --config/--file     Update a transform pipeline
omnivec transform delete <name> [-y]                Delete a transform pipeline
```

### Other commands

```
omnivec search <query> --index <dst-id> [--top-k <n>]  Vector similarity search
omnivec status                                      System health and statistics
omnivec config set <key> <value>                    Set configuration value
omnivec config view                                 Show current configuration
```

---

## 17. Troubleshooting

| Problem | Solution |
|---------|----------|
| `cannot connect to server` | Check the server URL: `omnivec config view`. Verify the server is running: `curl http://your-server/health` |
| `[404] Source 'xxx' not found` | Check the ID is correct: `omnivec source list`. IDs have prefixes (`src-`, `dst-`, `pip-`, `job-`). |
| `[400] Source is used by pipeline(s)` | Delete the pipeline first before deleting the source. |
| `[422] Field required` | The API requires certain fields. Check the `--help` for the command. |
| Connection test hangs | The test has a 10-second timeout. If it times out, check the endpoint URL (no extra spaces), network access, and Azure RBAC permissions. |
| Empty search results | Verify the pipeline has processed documents: `omnivec job list --pipeline <id>`. Check you're searching the correct destination. |
| `[503] Service unavailable` | DocGrok or the Kubernetes API may be down. Check `omnivec status` and `omnivec deployment list`. |
| Colors not showing | Pipe through `less -R` or set `TERM=xterm-256color`. Colors are disabled when output is piped. |

### Aliases

For quicker typing, the CLI supports aliases:

| Full name | Aliases |
|-----------|---------|
| `source` | `sources`, `src` |
| `destination` | `destinations`, `dest`, `dst` |
| `pipeline` | `pipelines`, `pip` |
| `job` | `jobs` |
| `deployment` | `deployments`, `deploy` |
| `model` | `models` |
| `transform` | `transforms`, `tp` |
