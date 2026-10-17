# OmniVec CLI Reference

[README](../README.md) | [User guide](user-guide.md) | [Search API](search-api.md)

The OmniVec CLI (`omnivec`) is a standalone binary for managing the OmniVec platform from the command line.

Commands below assume `omnivec` is on `PATH` and you have configured the server
and authentication. Replace angle-bracket placeholders with real values.
Multi-line examples marked `bash` use Bash continuations; in PowerShell, use
backticks or put the command on one line. Prefer JSON files for connector
configuration to avoid shell-quoting differences.

Use `omnivec <command> --help` for the flags available in your installed version.
Match the CLI to your server version when using recently added connector features.

---

## Installation

### Install a published release

Review the [Bash installer](../scripts/install.sh) or
[PowerShell installer](../scripts/install.ps1) before executing it. These download
the latest release by default; `OMNIVEC_VERSION` can pin a published tag.

**macOS & Linux** (auto-detects OS + arch, strips macOS quarantine, installs to `~/.local/bin`):
```bash
curl -fsSL https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.sh | bash
```

**macOS via Homebrew:**
```bash
brew install --formula https://raw.githubusercontent.com/AzureCosmosDB/OmniVec/main/scripts/omnivec.rb
```

**Windows** (installs to `%USERPROFILE%\.omnivec\bin` and updates user PATH):
```powershell
irm https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.ps1 | iex
```

### Manual download

Grab the binary for your platform from the [latest release](https://github.com/AzureCosmosDB/OmniVec/releases/latest):

| Platform | Binary |
|----------|--------|
| Linux (amd64) | `omnivec-vX.Y.Z-linux-amd64` |
| Linux (arm64) | `omnivec-vX.Y.Z-linux-arm64` |
| macOS (Apple Silicon) | `omnivec-vX.Y.Z-darwin-arm64` |
| macOS (Intel) | `omnivec-vX.Y.Z-darwin-amd64` |
| Windows (amd64) | `omnivec-vX.Y.Z-windows-amd64.exe` |
| Windows (arm64) | `omnivec-vX.Y.Z-windows-arm64.exe` |

Download only the asset for your OS/architecture, rename it to `omnivec`
(`omnivec.exe` on Windows), and add its directory to `PATH`. On Linux/macOS,
make that specific file executable with `chmod +x omnivec`.

> **macOS Gatekeeper note:** if you see *"cannot be opened because the developer cannot be verified"*, strip the quarantine flag before running:
> ```bash
> xattr -dr com.apple.quarantine /usr/local/bin/omnivec
> ```
> The quick-install script and Homebrew formula handle this automatically.

### Build from source

Requires Go 1.25 or later, as declared in [go.mod](../cli/go.mod).
From the repository root:

```powershell
go -C cli build -o ..\bin\omnivec.exe .
.\bin\omnivec.exe --help
```

On Linux/macOS:

```bash
go -C cli build -o ../bin/omnivec .
./bin/omnivec --help
```

---

## Configuration

```bash
# Set the server URL
omnivec config set server https://<omnivec-host>

# Optionally validate and save a token already supplied in the environment
omnivec auth login --token "$OMNIVEC_TOKEN"

# View current config
omnivec config view
```

**Server URL resolution order:**
1. `--server` flag (highest priority)
2. `OMNIVEC_SERVER` environment variable
3. `~/.omnivec/config.yaml`
4. Default: `http://localhost:8080`

Token precedence is `--token`, then `OMNIVEC_TOKEN`, then the saved configuration.
Use a control-plane credential, not a standalone search token. Saved credentials
are local to the CLI; protect the config file and avoid putting literal secrets
in shell history or source control. An HTTP localhost tunnel is useful for local
administration; use HTTPS across untrusted networks.

Supply `OMNIVEC_TOKEN` through your shell's secure credential workflow before
running the example. In PowerShell, the login command is
`omnivec auth login --token $env:OMNIVEC_TOKEN`. Login requires an explicit token;
it does not prompt. Other commands can use `OMNIVEC_TOKEN` without saving it.

---

## Global Flags

| Flag | Short | Description | Default |
|------|-------|-------------|---------|
| `--server` | `-s` | OmniVec server URL | from config |
| `--token` | | Authentication token; prefer the environment for routine commands | from environment/config |
| `--output` | `-o` | Output format: `table`, `json`, `yaml` | `table` |
| `--per-page` | | Table rows per page; `0` disables pagination | `15` |
| `--help` | `-h` | Show help for any command | |

The CLI auto-detects ID prefixes: `omnivec source show abc123` is the same as `omnivec source show src-abc123`.

---

## Sources

### Understand and fix access failures

Use `omnivec source permissions <id>` or `omnivec destination permissions <id>`
to run a **read-only** access diagnosis. Add `--shell bash` for Bash/Azure Cloud
Shell commands; PowerShell is the default. `--output json` returns the same
structured report used by the portal.

The report distinguishes authentication failures, denied access, missing
resources, network failures, and operations requiring resource provisioning.
For a service access denial, supported connectors provide a scoped grant command
with the actual account resource ID, subscription, resource group and workload
principal ID. Commands are displayed, **never executed** by OmniVec. They use
deterministic assignment IDs for repeat execution and do not remove existing roles.
An administrator with the indicated role-assignment authority must review and run
them; being an OmniVec administrator alone is not sufficient.

If discovery is unavailable or the resource is in another subscription, use
`--resource-id` with the full **account** ARM resource ID from Azure Portal's JSON
View. Older deployments may also need `--principal-id` with the workload managed
identity's **Object/principal ID**, not its client ID. Incomplete or mismatched
details produce no executable grant command. Updated `azd up` deployments supply
the workload principal ID automatically; Helm users set
`azure.workloadIdentity.principalId` and `azure.subscriptionId`.

Exact Azure grant templates currently support managed-identity Cosmos DB sources,
Cosmos DB vector destinations, and Azure Blob sources in Azure public cloud.
Other connectors return explicit limitations and administrator guidance, not
guessed commands. A successful destination metadata read does **not** prove write,
delete, changefeed or checkpoint access. These checks run from the API workload,
not every worker pod. Only `read_verified` returns exit code 0; all other statuses
return a nonzero exit code, including unsupported or incomplete checks.

After granting access, rerun the same command. Propagation can take several minutes;
checks have bounded waits and do not retry permission grants.

A source stores connection and connector-level scan settings. Pipeline source
entries choose content fields/modes; SharePoint file filters and OneLake scan
fields also belong to their source configurations.

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

# Request asynchronous source synchronization
omnivec source sync <id>
omnivec source sync <id> --full   # reprocess all documents
omnivec source sync <id> --full --wait --minimum-documents 10 --timeout 5m

# Update a source
omnivec source update <id> --name "Renamed Source"

# Delete a source
omnivec source delete <id> -y
```

Source synchronization is asynchronous. The API returns a durable operation ID;
`--wait` polls that operation until every affected pipeline has embedded at least
`--minimum-documents` documents from its current generation. A CLI timeout only
stops waiting; it does not cancel the pipeline or its long-running watcher.
This is a readiness threshold, not proof that a large or empty source has
completed a full enumeration.

Implemented types are `azure-blob`, `cosmosdb`, `postgresql`, `mssql`,
`databricks`, `sharepoint`, `onelake-iceberg`, and benchmark-only `synthetic`.
`s3` and standalone `http` creation are rejected. Use the
[source configuration table](user-guide.md#supported-sources) for required fields,
permissions, and watcher prerequisites.

`--config` accepts **one JSON object**, not repeated `key=value` arguments.
`--file` / `-f` reads that same configuration object from a file, without a
wrapping `name`/`type`/`config` resource object. Names and types are separate flags.
Connection settings cannot be changed while a pipeline references the resource.

### CosmosDB attachment-source mode

By default a CosmosDB source extracts inline text from the per-pipeline
`content_fields`. Setting `attachments_field` switches the source into
**attachment mode**: for each document, the watcher iterates the named array,
applies user filters, and emits queue work per matching attachment with the blob
URL — DocGrok's pipeline-worker downloads each blob from Azure Storage and
embeds it (PDF, DOCX, etc.).

Save this as `attachment-source.json`:

```json
{
  "endpoint": "https://acct.documents.azure.com",
  "database": "app",
  "container": "cases",
  "attachments_field": "attachments",
  "attachment_file_types": ["pdf", "docx"],
  "attachment_name_regex": "^report-.*",
  "account_url": "https://acct.blob.core.windows.net",
  "attachment_blob_container": "docs"
}
```

```powershell
omnivec source create --name "Cases with PDFs" --type cosmosdb -f attachment-source.json
```

Note that `container` names the **cosmos** container (`cases` above). For
attachments stored in Azure Blob Storage, use the dedicated
`attachment_blob_container` key — it is honored ahead of `container` for
relative-URL resolution and SSRF guard so the two values do not collide.
Most deployments use absolute blob URLs in the document itself and can omit
both `account_url` and `attachment_blob_container`.

Filter keys (all optional, AND-combined; an attachment must pass every
configured filter to be selected):

| Key | Description |
|-----|-------------|
| `attachments_field` | Top-level field name of the attachments array (e.g. `attachments`) |
| `attachment_url_field` / `attachment_name_field` / `attachment_content_type_field` | Per-attachment keys (defaults: `url`, `name`, `contentType`) |
| `attachment_name_regex` | Case-insensitive regex over the attachment name |
| `attachment_file_types` | Comma-separated or list — extensions, no dot (e.g. `pdf,docx`) |
| `attachment_content_types` | Comma-separated or list — MIME types |
| `account_url` | Azure Blob account URL — SSRF host pin (URLs must match) |
| `attachment_blob_container` | Default blob container for relative URLs (preferred over `container`) |

Attachment URLs must be on `*.blob.core.windows.net`; if `account_url` is set,
URLs must match that exact host (SSRF guard). Attachment-mode pipelines must
use `processing_mode=queue`; do not rely on inline processing to download and
extract attachments.

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

For Cosmos DB, testing discovers the container's vector policies and indexes.
Use the applicable path as `--vector-index-path` and match model dimensions.
Other connector tests return their own connectivity/index evidence.

Destination types are `cosmosdb-vector`, `pgvector`, `mssql`, `garnet`,
`onelake-iceberg`, and benchmark-only `null`. Successful registration does not
imply search support or permission to write/delete. See the
[destination guide](user-guide.md#2-destinations).

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
  --model <registered-model-id-or-transform> \
  --content-fields content \
  --content-strategy truncate \
  --vector-index-path /embedding \
  --process-existing

# Update a pipeline
omnivec pipeline update <id> --name "Renamed" --description "Updated"

# Creation leaves the pipeline paused; activate it explicitly.
omnivec pipeline run <id>

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
| `--model` | Yes | — | Registered embedding model ID/name or existing DocGrok transform |
| `--content-fields` | No | `content` | Comma-separated field names to embed |
| `--content-mode` | No | `field` | Content extraction mode: `field`, `blob_url`, `http_url` |
| `--file-types` | No | — | Comma-separated file type filters (e.g., `txt,pdf,md`) |
| `--content-strategy` | No | `truncate` | Content strategy: `truncate` or `chunk` |
| `--chunk-size` | No | Server default: `1000` | Chunk size in the selected unit; minimum 100 |
| `--chunk-overlap` | No | Server default: `200` | Overlap; zero is supported and must be smaller than size |
| `--chunk-unit` | No | `chars` | `chars` or `tokens` (whitespace words, not BPE tokens) |
| `--store-text` | No | `false` | Persist chunk text |
| `--text-field` | No | `text` | Stored chunk-text field |
| `--chunk-doc-id-pattern` | No | Server-generated default | Chunk template; Cosmos requires `{chunk}` and adds an isolation prefix |
| `--doc-id-pattern` | No | `{source_hash}-{pipeline}` | Destination item template; also supplies chunk template unless its explicit flag is set |
| `--partition-key-pattern` | No | `{source_partition}` | Destination logical-partition template |
| `--collision-policy` | No | `reject` | Shared-destination safety policy: `reject` or explicit `overwrite` |
| `--vector-index-path` | Yes | — | Destination vector path/column, such as `/embedding` |
| `--processing-mode` | No | `queue` | `queue` or supported same-store `inline` |
| `--store-content` | No | Connector-dependent | `true`/`false` for single-document text storage |
| `--content-field` | No | `content` | Cosmos stored-content field |
| `--metadata-fields` | No | `all` | `all`, `none`, or supported optional fields |
| `--sharepoint-tenant-id`, `--sharepoint-client-id` | No | Deployment identity | Supply together for pipeline-scoped SharePoint federation |
| `--description` | No | — | Pipeline description |
| `--process-existing` | No | `true` | Initial backfill policy; use `--process-existing=false` to disable |

There is no `--no-process-existing` flag. The create command accepts one source;
use the REST API for a multi-source pipeline.

The CLI exposes update flags, but the API remains authoritative about immutability:
source entries, destination, vector path, document/partition/chunk-ID templates
cannot be changed in place. Cosmos strategy and chunk text-storage settings are
also immutable. Supported chunk size/overlap/unit changes affect future work.
See [lifecycle and sharing rules](user-guide.md#pipeline-lifecycle) and
[chunking details](pipeline-tuning.md#cosmos-text-chunking-queue-mode).

Pause does not cancel queued/in-flight work. Reset can delete records/chunk outputs
and request replay; it is not a harmless refresh. Deleting a pipeline does not
purge all vectors.

---

## Jobs

These commands manage metadata job records. Queue and inline pipelines may
process documents without creating one record per document; use pipeline metrics,
destination evidence, backlog, and DLQs as well.

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

`job retry` is not a Service Bus DLQ redrive.

---

## Deployments

Manage OmniVec Kubernetes deployments.

```bash
# List deployments (aliases: deploy, deployments)
omnivec deployment list

# Scale a deployment
omnivec deployment scale omnivec-dotnet-worker --replicas 5

# Pause (scale to 0) / Resume (scale to 1)
omnivec deployment pause omnivec-dotnet-worker
omnivec deployment resume omnivec-dotnet-worker

# Rolling restart
omnivec deployment restart omnivec-dotnet-worker -y
```

Use names returned by `deployment list`; optional watchers/models and Helm release
names can vary. Resume scales to one replica, not necessarily the previous count.
Review autoscaling and maintenance policy before changing replicas. More workers
do not increase model quota or destination capacity.

---

## MCP and Foundry Cloud Jobs

Cloud plans are approval-gated and require an authenticated OmniVec
administrator. Preparing or inspecting a plan does not provision resources.

```bash
# Inspect capabilities and saved jobs
omnivec cloud list
omnivec cloud show <job-id>

# Print exact Azure role grants for administrator review
omnivec cloud permissions <job-id>
omnivec cloud permissions <job-id> --shell bash

# Approve the immutable saved plan and wait up to five minutes
omnivec cloud approve <job-id> --yes --wait --timeout 5m
omnivec cloud wait <job-id> --timeout 5m
```

Prepare an MCP deployment:

```bash
omnivec cloud plan mcp \
  --resource-group-id /subscriptions/<sub>/resourceGroups/<rg> \
  --location eastus2 \
  --destination <dst-id> \
  --embedding-model <model-id> \
  --cosmos-account-id <cosmos-account-resource-id> \
  --embedding-account-id <azure-openai-account-resource-id> \
  --vector-field embedding \
  --fields id,title,content,source_ref
```

Prepare a Foundry agent and one retrieval-verified question:

```bash
omnivec cloud plan foundry \
  --mcp-deployment <succeeded-mcp-job-id> \
  --project-resource-id <foundry-project-resource-id> \
  --chat-deployment <chat-deployment>

omnivec cloud plan verify \
  --foundry-deployment <succeeded-foundry-job-id> \
  --question "What does the travel policy require? Cite the source."
```

The CLI prints permission commands but never executes role assignments on its
own identity.

---

## SharePoint to Foundry Demo

`omnivec demo sharepoint-foundry` runs the folder-scoped SharePoint ingestion,
pipeline activation, asynchronous full sync, source/pipeline-scoped marker
search, MCP deployment, Foundry agent creation, and retrieval-verified question
as one command. It requires `--approve` and defaults to a five-minute total
budget.

Use pre-created, permission-ready Azure resources for a timed demonstration.
Fresh Azure Function provisioning can exceed five minutes due to Azure
control-plane latency. Pass `--foundry-deployment <succeeded-job-id>` for the
fastest path, or `--mcp-deployment <succeeded-job-id>` to reuse only MCP. See
[`examples/sharepoint-foundry/README.md`](../examples/sharepoint-foundry/README.md)
for the complete command and synthetic policy question.

For an already registered source compatible with the selected Cosmos destination,
use the connector-neutral command:

```bash
omnivec demo source-foundry \
  --source <source-id> \
  --destination <cosmos-vector-destination-id> \
  --pipeline-model <pipeline-or-embedding-model-id> \
  --marker "unique text expected in one source document" \
  --question "Question the Foundry agent must answer. Cite the source." \
  --foundry-deployment <succeeded-foundry-job-id> \
  --timeout 5m \
  --approve
```

The existing Foundry deployment must ultimately target the selected destination.
Omit `--foundry-deployment` to create MCP/Foundry resources and provide the same
Azure provisioning flags documented for `sharepoint-foundry`.

---

## Models

Manage embedding models.

```bash
# List models
omnivec model list

# Show model details
omnivec model show <name>

# Test model connectivity
omnivec model test <name>

# Add an Azure OpenAI model
omnivec model add \
  --name my-azure-openai \
  --type azure-openai \
  --endpoint https://myresource.openai.azure.com \
  --auth-type managed-identity \
  --api-version 2024-06-01 \
  --model <azure-deployment-name> \
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
| `--name` | Yes | Unique name for this model |
| `--type` | Yes | `azure-openai`, `openai`, `cohere`, `custom` |
| `--endpoint` | Yes | Endpoint URL |
| `--model` | Yes | Model or deployment name |
| `--api-key` | No | API key |
| `--auth-type` | No | `key` (default) or `managed-identity`; grant the workload identity access first |
| `--client-id` | No | Optional managed-identity client ID |
| `--api-version` | No | API version (required for Azure OpenAI) |
| `--dimensions` | No | Embedding dimensions |

An Azure deployment name is not necessarily the underlying model name. Match the
actual output dimensions, and use the registered model returned by this command
when creating the pipeline. Use API-key authentication only with appropriate
secret handling; do not paste real keys into examples or source files.

---

## Transform Pipelines

Multi-step processing chains in DocGrok (PDF extraction, text chunking, embedding).

```bash
# List transform pipelines (alias: tp)
omnivec transform list

# Show details
omnivec transform show text-azure

# Create from a complete, valid DocGrok transform definition
omnivec transform create -f pipeline.json

# Update / Delete
omnivec transform update text-azure -f updated.json
omnivec transform delete my-pipeline -y
```

Inspect an existing transform and the available stage catalog before authoring
`pipeline.json`; a partial `steps` placeholder is not an executable definition.
A DocGrok transform is distinct from an OmniVec ingestion pipeline.

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
| `--index` | `-i` | Yes | Destination ID or exact case-insensitive name |
| `--top-k` | `-k` | No | Number of results (default: 5) |

This command calls the control-plane `/api/playground/search` endpoint with one
destination. For multi-destination, filtered, full-text, hybrid, or image queries,
use the [Search API](search-api.md). Prefer `-o json` for complete timing, warnings,
per-index errors, and untruncated results; scores are not relevance probabilities.

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

The global output flag selects `table` (default), `json`, or `yaml` for resource
results. Some action/interactive commands also print human-readable status; do
not assume every command's stdout is only a JSON document.

```bash
# JSON for scripting with jq
omnivec source list -o json | jq '.[].name'

# YAML
omnivec pipeline list -o yaml
```

---

## Walkthrough: End-to-End Example

Prerequisites: an accessible Azure OpenAI deployment, the Cosmos source/destination
containers from the [README](../README.md#4-connect-a-source), matching vector
policy/index, and workload identity grants. The following Bash example uses
placeholders for returned IDs; replace them after each create.

```bash
# 1. Check the system
omnivec status

# 2. Add an external embedding model
omnivec model add --name azure-openai-prod --type azure-openai \
  --endpoint https://myresource.openai.azure.com --auth-type managed-identity \
  --api-version 2024-06-01 --model <azure-deployment-name> --dimensions 1536

# 3. Create a CosmosDB source
omnivec source create --name "My Documents" --type cosmosdb \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"demo","container":"documents"}'
omnivec source test <source-id>

# 4. Create a CosmosDB vector destination
omnivec dest create --name "Vectors" --type cosmosdb-vector \
  --config '{"endpoint":"https://myaccount.documents.azure.com","database":"demo","container":"vectors"}'
omnivec dest test <destination-id>

# 5. Create a pipeline
omnivec pipeline create --name "My Pipeline" --source <source-id> \
  --destination <destination-id> --model <registered-model-id> \
  --content-fields content --vector-index-path /embedding \
  --processing-mode queue --store-content true --process-existing

# 6. Activate and inspect progress
omnivec pipeline run <pipeline-id>
omnivec pipeline show <pipeline-id>
omnivec metrics summary

# 7. After confirming destination writes, search and inspect warnings
omnivec search "my query" --index <destination-id> --top-k 5 -o json
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `connection refused` | Server unreachable | Check `omnivec config view`, verify the URL is correct |
| `401 Unauthorized` | Invalid, expired, or wrong-scope token | Check server and token precedence; validate with `auth login --token` |
| `source not found` | Wrong ID | Use `omnivec source list` to get the correct ID |
| No Jobs-page records | May be normal for queue/inline mode | Inspect pipeline status, metrics, queue/DLQ, and destination writes |
| Pipeline paused | New pipelines are created paused | Run `omnivec pipeline run <id>` when ready |
| No processing progress | Source, model, destination, or worker may be blocked | Inspect `pipeline show`, `deployment list`, and Diagnostics before scaling/resetting |
