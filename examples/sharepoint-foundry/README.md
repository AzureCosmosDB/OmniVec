# Isolated SharePoint to Foundry example

[README](../../README.md) | [SharePoint setup](../../docs/sharepoint-source.md) | [Cloud deployments](../../docs/cloud-deployments.md)

The three `.txt` files in this directory contain synthetic policies with distinctive
test values. They are not real company policies.

## Prerequisites

Use an authorized test environment with a working SharePoint watcher, Graph
site/library access, embedding model, and a **separate Cosmos vector container
partitioned by `/document_id`**, not `/id`. Match its vector dimensions to the
registered model. Also prepare the hosting resource group, Foundry project/chat
deployment, deployment allowlist, and necessary Azure grants.

You can prepare the container through OmniVec without adopting an existing one:

```powershell
omnivec cloud plan container `
  --cosmos-account-id "<cosmos-account-resource-id>" `
  --database "<existing-database>" `
  --container "sharepoint-foundry-demo-20260923" `
  --embedding-model "<registered-azure-openai-model-id>" `
  --partition-key-path /document_id
```

Review the returned plan, then approve that job explicitly with
`omnivec cloud approve <job-id> --wait`. Register its resulting container as a
destination and test it. Preparing a plan does not provision the container.

## Upload once, then use OmniVec

Create a new folder named **OmniVec-Foundry-Demo-20260923** at the root of the
document library already connected to OmniVec. Upload all three `.txt` files.
Do not put this folder inside an existing watched folder such as `Policies`.
Do not alter any existing document. After the prerequisites are in place,
uploading the fixtures is the only source-data change required.

## CLI workflow

For a timed demonstration, pre-create the isolated Cosmos vector destination,
Foundry project/chat deployment, deployment allowlist, and Azure role grants.
Fresh Azure Function provisioning is subject to Azure control-plane latency and
cannot be guaranteed to finish in five minutes.

After uploading the files, run the remaining workflow through one OmniVec CLI
command:

```powershell
omnivec demo sharepoint-foundry `
  --site-id "<graph-site-id>" `
  --drive-id "<graph-drive-id>" `
  --folder "OmniVec-Foundry-Demo-20260923" `
  --destination "<isolated-cosmos-destination-id>" `
  --pipeline-model "<registered-model-id-or-matching-transform>" `
  --embedding-model "<registered-azure-openai-model-id>" `
  --resource-group-id "/subscriptions/<sub>/resourceGroups/<rg>" `
  --cosmos-account-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DocumentDB/databaseAccounts/<account>" `
  --embedding-account-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>" `
  --project-resource-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>" `
  --chat-deployment "<chat-deployment>" `
  --timeout 5m `
  --approve
```

For a cross-tenant SharePoint app, also pass
`--sharepoint-tenant-id <tenant-guid>` and
`--sharepoint-client-id <app-client-guid>`. Same-tenant deployments use the
OmniVec workload identity by default.

The command creates and tests the folder-scoped SharePoint source, creates and
activates the queue pipeline, starts an asynchronous full sync, and waits until
`NORTHSTAR-DEMO-TRAVEL-2026` is searchable using the exact new source and
pipeline IDs. It then deploys or reuses the MCP server, creates or reuses the
Foundry agent, asks the test question, and prints the answer plus inspectable
vector-search source references. A timeout stops the CLI wait but does not
cancel ingestion.

To avoid provisioning on each run, pre-create the MCP/Foundry resources
against the same destination and replace the Azure provisioning flags with:

```powershell
  --foundry-deployment "<succeeded-foundry-job-id>"
```

Use `--mcp-deployment "<succeeded-mcp-job-id>"` to reuse only the MCP server
while creating a fresh Foundry agent.

`--approve` authorizes the exact plans, resource costs, identity grants, and one
billable inference request. OmniVec never grants its own deployment identity
additional Azure roles. If a plan lacks access, inspect it and print the exact
administrator-reviewed commands:

```powershell
omnivec cloud show <job-id>
omnivec cloud permissions <job-id>
```

The expected answer is **47 USD per day; receipts for individual meals over
18 USD; submission within 12 calendar days after the trip ends.**

## Manual portal workflow

After the upload, the same workflow can be completed in the OmniVec portal:

1. Prepare the isolated vector container from the prerequisites. If using the
   console's container planner, verify its plan selects `/document_id`; use
   the CLI's `--partition-key-path` override if the form only exposes `/id`.
   Review and separately approve the plan.
2. **Destinations:** register that newly created Cosmos vector container using
   its returned endpoint/database/container. Test the connection and vector
   policy. Use managed identity, not an account key.
3. **Sources:** register a new SharePoint source using the connected site's
   existing site ID, drive ID and approved identity. Restrict `folder_path` to
   `OmniVec-Foundry-Demo-20260923`; do not register the entire drive.
4. **Pipelines:** create a new pipeline from this new source to this new
   destination, using the same embedding model, vector field `embedding`,
   queue mode, process-existing enabled and stored text enabled in field `content`.
   Explicitly **Run or Resume** the new pipeline, then wait for the three source
   documents to be processed. Inspect processing
   status and search results; registration/readiness is not ingestion proof.
5. **Vector Search:** search the new destination for
   `NORTHSTAR-DEMO-TRAVEL-2026`. Confirm the synthetic travel policy is returned.
6. **MCP servers:** select the new destination and matching embedding model.
   Use returned fields `id,title,content,source_ref`, not `text`. Supply actual
   account resource IDs and an approved hosting resource group/region.
   Review and approve the new Function/storage/hosting and identity grants.
7. **Foundry agents:** select the completed MCP deployment and an existing
   Foundry project/chat deployment. Review and approve agent creation.
8. **Foundry agents > Test a deployed Foundry agent:** select the new agent and
   prepare a test plan for this question:

   **Under NORTHSTAR-DEMO-TRAVEL-2026, what is the domestic daily meal limit,
   when are individual meal receipts required, and how soon must the claim
   be submitted? Cite the source.**

   Review and approve the separate inference/embedding cost. Inspect the answer
   and source references displayed in the saved test job.

Expected answer: **47 USD per day; receipts for individual meals over 18 USD;
submission within 12 calendar days after the trip ends.** The returned source
must identify the new synthetic travel document, not an older SharePoint policy.

Do not call this an end-to-end pass until new-document ingestion, new-container
search, MCP retrieval and the Foundry answer/reference have all been observed.
A successful agent creation alone is not sufficient.

## Approval and isolation

Provisioning is disabled by default. An administrator must separately approve
enabling the scoped deployment identity, the resource plans and billable
inference. No SharePoint write access is needed by OmniVec for this example.
Existing sources, pipelines, vector containers and agents are not changed.
Example resources remain billable until an operator approves their cleanup.
See [deployment permissions and recovery](../../docs/cloud-deployments.md).
