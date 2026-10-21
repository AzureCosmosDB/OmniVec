# OmniVec

**Connect data sources, generate embeddings, and search your vectors on Azure.**

OmniVec runs on Azure Kubernetes Service (AKS) and provides a web console, CLI,
and REST API. Source watchers detect data changes; DocGrok extracts content and
routes embedding requests; workers write the resulting vectors to your chosen
destination.

> **Public preview:** Expect breaking changes, incomplete features, and limited
> support. This release is not yet ready for production use.

```text
Source -> source watcher -> processing -> embedding model -> destination
                                  DocGrok                    |
                                                             v
                                                       Vector search
```

Not every source/destination pair supports every processing or search mode.
Start with the Cosmos DB walkthrough below, then use the connector guides for
other combinations.

See the [v2.0.0 release notes](docs/releases/v2.0.0.md) for cumulative fixes,
breaking changes, upgrade instructions, and measured benchmark results.

## Start here

| I want to... | Read |
|--------------|------|
| Deploy OmniVec and run a first pipeline | [Quick start](#quick-start) below |
| Use the console and understand pipeline actions | [User guide](docs/user-guide.md) |
| Automate with the CLI | [CLI guide](docs/cli-guide.md) |
| Configure or upgrade a deployment | [Deployment guide](docs/deployment.md) |
| Understand components, identity, and data flow | [Architecture](docs/architecture.md) |
| Use search from an application | [Search API](docs/search-api.md) |
| Connect a SharePoint library | [SharePoint guide](docs/sharepoint-source.md) |
| Connect Fabric OneLake or configure write-back | [OneLake guide](docs/onelake-iceberg.md) |
| Tune batching, chunking, or broker lanes | [Pipeline tuning](docs/pipeline-tuning.md) |
| Diagnose a pipeline or use approval-gated recovery | [Diagnostics and Recovery Agent](docs/agent.md) |
| Deploy a Cosmos MCP server or connect a Foundry agent | [Cloud deployment guide](docs/cloud-deployments.md) |

## Quick start

### 1. Prepare

Install [Azure CLI](https://aka.ms/install-azure-cli),
[Azure Developer CLI](https://aka.ms/install-azd),
[PowerShell 7+](https://aka.ms/install-powershell), and
[Git](https://git-scm.com). Hooks install `kubectl` and Helm if needed.

You need an Azure subscription with resource-provisioning and role-assignment
permissions, available regional quota, and access to an embedding deployment.
The default configuration uses two `Standard_D4s_v5` system nodes, no GPU pool,
and Cosmos DB serverless metadata. **Azure resources and inference incur costs.**
See [deployment prerequisites](docs/deployment.md#prerequisites) before proceeding.

### 2. Deploy

Run in PowerShell 7 on Windows, or your shell on Linux/macOS:

```powershell
git clone https://github.com/AzureCosmosDB/OmniVec
cd OmniVec
az login
azd auth login
azd env new my-omnivec
azd up
```

Choose **Quick start** when prompted. Deployment provisions the infrastructure,
imports or builds images, deploys the Helm charts, and checks readiness.
Keep the environment name for later upgrades and diagnostics.

Open the reported OmniVec URL with `/ui`. Retrieve the admin token for sign-in:

```powershell
azd env get-value OMNIVEC_ADMIN_TOKEN
```

Treat it as a secret. Use HTTPS before sending credentials over an untrusted
network. For Microsoft Entra login, TLS requirements, interrupted deployments,
and image updates, see the [deployment guide](docs/deployment.md).

### 3. Register an embedding model

For this walkthrough, use an Azure OpenAI deployment of
`text-embedding-3-small` with **1536 output dimensions**. No GPU nodes are needed.
You can use another model, but its output dimensions must match the destination.

In **Models**, add an Azure OpenAI external model and supply:

| Setting | Value |
|---------|-------|
| Endpoint | Your resource endpoint, such as `https://<resource>.openai.azure.com` |
| Deployment | The exact Azure deployment name, which may differ from the model name |
| Authentication | Managed identity with the required Azure OpenAI access, or an API key |
| Dimensions | `1536` for this example |

Save, then **Test**. For managed identity, grant the OmniVec workload identity
**Cognitive Services OpenAI User** on the embedding resource. Registering a model
does not grant that permission or create an Azure OpenAI deployment.

Keep the returned model ID/name. Select that model when creating the pipeline;
do not assume a transform named `text-azure` uses the model you just registered.

### 4. Connect a source

Use a Cosmos DB for NoSQL data account separate from OmniVec's metadata account.
In Data Explorer, create database `demo` and container `documents`, partitioned
by `/id`. Add these two items:

```json
{
  "id": "doc-001",
  "title": "About OmniVec",
  "content": "OmniVec connects data sources to embedding models and vector stores for semantic search."
}
```

```json
{
  "id": "doc-002",
  "title": "About AKS",
  "content": "Azure Kubernetes Service runs containerized applications on managed Kubernetes clusters."
}
```

In **Connections > Sources**, add a Cosmos DB source with the account endpoint,
database `demo`, and container `documents`. In the classic console, use
**Sources > New Source**.

Use **Check access** and **Test Connection**. The permissions report can provide
scoped grant commands for an authorized Azure administrator. Cosmos data-plane
access and any account-discovery permissions are distinct; being an OmniVec
administrator does not grant either. Repeat the check after permissions propagate.
A read-only connection check does not prove destination writes or checkpoint access.

### 5. Connect a vector destination

In the same data account, enable the Cosmos DB vector-search capability and create
a **separate** container, `demo/vectors`. Configure:

| Setting | Value |
|---------|-------|
| Partition key | `/id` |
| Vector path | `/embedding` |
| Data type | `float32` |
| Dimensions | `1536` |
| Distance function | `cosine` |
| Vector index | `quantizedFlat` or `diskANN` on `/embedding` |

Both a vector embedding policy and an appropriate vector index are needed.
For a tiny sample, Cosmos may use a full scan until the index has enough vectors;
this walkthrough is not a performance benchmark. See the
[Cosmos DB vector-search documentation](https://learn.microsoft.com/azure/cosmos-db/nosql/vector-search).

In **Connections > Vector stores**, add a Cosmos DB Vector destination pointing
to `demo/vectors`. Test the connection and confirm that `/embedding` and its
dimensions are discovered. In the classic console, use **Destinations**.

> `/id` is suitable for this Cosmos-to-Cosmos example. **SharePoint requires a
> different partitioning contract**, such as `/document_id`; follow its guide.

### 6. Create and activate the pipeline

In **Pipelines**, create a pipeline with these settings:

| Setting | Value |
|---------|-------|
| Source | The `demo/documents` source |
| Content fields / mode | `content` / `field` |
| Model | The embedding model registered in step 3 |
| Destination | The `demo/vectors` destination |
| Vector index path | `/embedding` |
| Processing mode | `queue` |
| Content strategy | `truncate` |
| Process existing | Enabled |

`truncate` produces one embedding per document, subject to model input limits;
it does not promise to embed an arbitrarily long document in full. To retrieve
text previews from a separate Cosmos destination, explicitly enable content
storage (`store_content: true`, `content_field: "content"`). Otherwise, source
text is not copied there by default.

**New pipelines start paused.** After creation, select **Run** or **Resume**.
`process_existing` controls backfill; it does not activate the pipeline.

### Verify the result

Confirm all three stages rather than relying only on a green health badge:

1. **Ingestion:** the pipeline is active and its embedded count increases.
   Check watcher/worker diagnostics and errors if it does not.
2. **Storage:** the destination contains vectors for both sample documents with
   the expected dimensions and pipeline/source metadata.
3. **Retrieval:** open **Vector Search**, select the destination, and search for
   `vector ingestion platform`. Inspect the returned document references and,
   if content storage was enabled, their text.

Ranking and latency vary by model and index. Completion percentages require a
known source total; chunk counts and source-document counts are not interchangeable.
Queue pipelines do not necessarily create one Jobs-page record per document.

## Supported connectors

These tables describe implemented connectors, not a promise that every watcher
is enabled or every pair is compatible. Queue pipelines require Service Bus and
.NET workers. See the [configuration and compatibility guide](docs/user-guide.md).

| Source | API type | Ingestion / prerequisites |
|--------|----------|--------------------------|
| Azure Blob Storage | `azure-blob` | Initial enumeration and polling; optional Event Grid events; deletion requires event delivery |
| Cosmos DB for NoSQL | `cosmosdb` | Initial backfill and change feed |
| PostgreSQL | `postgresql` | Recognized timestamp-column polling, otherwise repeated table scans |
| SQL Server / Azure SQL | `mssql` | Initial scan and SQL CDC; CDC and suitable permissions required |
| Databricks Delta | `databricks` | Snapshot and Delta CDF through a SQL Warehouse; dedicated watcher |
| SharePoint Online | `sharepoint` | Microsoft Graph delta polling; Graph grants and dedicated watcher |
| Fabric OneLake | `onelake-iceberg` | Iceberg scans; optional separately deployed Fabric Spark Delta CDF job |
| Garnet HASH | `garnet` | Versioned JSON documents; inline polling to a vector set on the same endpoint |
| Isolated mock service | `mock` | Opt-in inline benchmark source with separate model and sink services |
| Synthetic traffic | `synthetic` | Explicit API-triggered publication for controlled benchmarks only |

`s3` and standalone `http` are declared API types but **not implemented**;
source creation rejects them. A URL-valued content field is not a standalone
HTTP source.

| Destination | API type | Storage / OmniVec search |
|-------------|----------|--------------------------|
| Cosmos DB Vector | `cosmosdb-vector` | Vector search; full-text/hybrid with the corresponding Cosmos policies |
| PostgreSQL + pgvector | `pgvector` | Vector column/index with matching dimensions; vector search |
| SQL Server / Azure SQL | `mssql` | Serialized embedding arrays and metadata; no OmniVec Search support |
| Garnet Vector Sets | `garnet` | Preview vector storage/search; requires `--enable-vector-set-preview`, not generic Redis |
| OneLake write-back | `onelake-iceberg` | Asynchronous Spark MERGE into existing source rows; search through an optional Garnet mirror |
| Isolated mock sink | `mock` | Opt-in HTTP benchmark sink; no persistence or search |
| Null sink | `null` | Benchmark-only discard sink; no persistence or search |

SharePoint supports Cosmos DB Vector destinations only. OneLake write-back
requires queue mode, `truncate`, and the same lakehouse/source table. Synthetic
sources and null sinks require `benchmark_only: true`; benchmark completion is
not evidence of production storage or search quality.

The desktop wizard exposes a subset of these types. Use the CLI/REST API for
implemented connectors missing from the picker. Registration does not deploy a
watcher or grant access.

## Operations and troubleshooting

The console's **Metrics** view separates live measurements from historical
ranges; unavailable values remain unknown rather than becoming zero.
**Diagnostics** gathers read-only system/pipeline evidence without a chat model.
**Recovery Agent** uses a registered chat model and explicit approvals for
supported repairs. See the [agent contract](docs/agent.md).

| Symptom | First checks |
|---------|--------------|
| UI unreachable | Correct environment URL, web service external IP, pod readiness, network/TLS setup |
| Authentication rejected | Correct server and credential scope; token expiry or Entra role assignment |
| Source/destination test fails | **Check access** report, network reachability, resource IDs, connector-specific grants |
| Azure OpenAI `DeploymentNotFound` | Exact Azure deployment name and endpoint, not just the model family |
| Active pipeline makes no progress | Watcher enabled, source content available, model/destination healthy, queue backlog and DLQ |
| Embedded count is zero/unknown | Confirm real destination writes, metadata, metrics freshness, and source totals before resetting |
| Vectors exist but search is empty | Searchable destination, matching model/dimensions/vector path, index readiness, response warnings |
| `ImagePullBackOff` | Image existence and pull permissions; follow the [upgrade/recovery guide](docs/deployment.md#update-or-recover-a-deployment) |

For deployment diagnostics, run from the repository root:

```powershell
pwsh scripts\diagnose.ps1 -EnvName my-omnivec
pwsh scripts\diagnose.ps1 -EnvName my-omnivec -Pipeline pip-example
```

On Linux/macOS, use `./scripts/diagnose.sh --env my-omnivec`, adding
`--pipeline pip-example` for one pipeline. Review proposed fixes before executing
them. Readiness and a successful connection test are not proof of completed ingestion.

**Pause is not a cancellation fence.** Already queued or in-flight work can
finish. Reset can replay documents, remove job records, and clean up chunk
outputs; it restores the previous active/paused state on success. Plan destructive
recovery with producers, workers, and queued messages quiesced.

## Automated demos

The [CLI walkthrough](docs/cli-guide.md#walkthrough-end-to-end-example) mirrors
the manual flow. The [SharePoint-to-Foundry example](examples/sharepoint-foundry/README.md)
adds scoped ingestion, MCP, and a retrieval-verified question.

Repository scripts also provide opt-in demos:

| Script | Purpose |
|--------|---------|
| [e2e-demo.ps1](scripts/e2e-demo.ps1) / [e2e-demo.sh](scripts/e2e-demo.sh) | Provision/use a deployment, create sample resources, ingest, and search |
| [e2e-blob-demo.ps1](scripts/e2e-blob-demo.ps1) | Blob-to-Cosmos demo; accepts an existing registered `-ModelId` |

These scripts can create resources, consume inference, and delete fixtures.
Inspect their parameters and obtain approval for the target environment first.
`-Existing` / `--existing` uses existing infrastructure; `-FromStep` /
`--from-step` resumes a demo step, not an arbitrary deployment transaction.
An ingestion-only run such as `-NoSearch` still needs separate retrieval verification.

## Cleanup

Deleting a pipeline removes its configuration, **not all vectors or external
resources**. Paused pipelines may still have queued work.

To remove an entire deployment, review the selected azd environment and back up
required data before using `azd down`. This is destructive. External source/model
accounts and separately created Fabric, MCP, or Foundry resources may remain.
See [environment removal](docs/deployment.md#remove-an-environment).

## Repository layout

| Directory | Responsibility |
|-----------|----------------|
| `api/` | Python FastAPI control plane and bookkeeping |
| `web/` | Static console and nginx proxy |
| `search/` | Standalone search service |
| `connectors/ingestion/` | .NET source ingestion and Python OneLake watcher |
| `connectors/worker/dotnet/` | Queue-based embedding and destination writes |
| `connectors/fabric_spark/` | Separately configured Fabric CDF and MERGE jobs |
| `docgrok/` | Embedding routing, model integration, and document transforms |
| `agent/` | Diagnostics and approval-gated recovery |
| `cli/` | Go command-line client |
| `infra/`, `helm/`, `hooks/` | Azure infrastructure and application deployment |

## License

MIT. See [LICENSE](LICENSE), [contributing guidance](CONTRIBUTING.md), and
[security reporting](SECURITY.md).
