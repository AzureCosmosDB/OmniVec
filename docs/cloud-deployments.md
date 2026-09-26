# MCP and Foundry deployment from the console

The **MCP servers** and **Foundry agents** sections execute approved Azure
operations through the API workload identity. They do not run commands on the
operator's workstation. Opening either section, refreshing status, and preparing
a plan never provision Azure resources.

## Operator setup

Build and deploy the updated API and web images. Both API Dockerfiles package the
existing `mcp_servers/cosmos` implementation and its Python 3.11 Linux dependencies
at image-build time. No package installation, arbitrary shell command, or source
download runs inside the live API. The host configuration is explicitly included
in the Docker build context.

Provisioning remains off until an operator configures Helm values:

- `api.cloudDeployments.enabled`: opt in explicitly; defaults to `false`.
- `api.cloudDeployments.allowedScopes`: full existing Azure resource-group or
  resource IDs. Include the hosting resource group, Cosmos account, embedding
  account and, for Foundry, project and MCP Function scopes as appropriate.
  An allowed resource group includes descendants; subscription-wide and wildcard
  entries are rejected. A matching allowlist does **not** grant Azure permissions.
- `azure.workloadIdentity.principalId`: the actual API workload identity object
  ID, used to generate administrator commands. This is not its application/client
  ID. Changing this identity invalidates prepared plans.

Equivalent process settings are `OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED=true`,
`OMNIVEC_DEPLOYMENT_SCOPES` (comma-separated full IDs), and `AZURE_PRINCIPAL_ID`.
Leave deployment disabled while reviewing plans and granting permissions.

Only authenticated OmniVec administrators may prepare, inspect or approve jobs.
Internal-host authentication bypass does not authorize deployment endpoints.
Plan review shows the required deployer privileges and copyable PowerShell/Bash
commands with real scopes and principal IDs. No executable command is produced
when the principal ID is unknown. Administrators must review these privileged
role grants; the worker never grants its own identity additional permissions.
Equivalent narrower custom roles may replace the documented built-in roles.

## Deploy an MCP server

1. Select an enabled Cosmos vector destination and its actual Azure OpenAI
   embedding model. Supply the existing target subscription/resource group,
   supported Flex Consumption region, and full Cosmos/embedding account IDs.
2. Select the vector field and projected fields. Include the stored text field:
   chunk pipelines often use `text`, other pipelines may use `content`. Fields
   must be top-level identifiers. The selected model must be the model that
   generated the stored vectors, not merely one with equal dimensions.
3. Prepare and review the plan. It names the new Function, Flex Consumption plan
   and storage account, and explains costs and service identity grants.
4. Enable the feature only after operator approval, select the consent checkbox,
   and approve deployment. The browser may close; work persists in metadata.

Before provisioning, the worker reads the actual Cosmos vector embedding/index
policy and Azure embedding deployment. This version supports standard dimensions
for `text-embedding-ada-002`, `text-embedding-3-small`, and
`text-embedding-3-large`, not dimension-reduced embeddings.

The Function uses a system-assigned identity. It receives Cosmos Data Reader
access scoped to the selected container, OpenAI User on the embedding account,
and Blob Data Owner on its new host/deployment storage. Storage disallows shared
keys and public blobs. The public HTTPS MCP endpoint requires a Function key.
Deployment uses Entra-authenticated OneDeploy and the prebuilt package, with
remote build disabled.

Readiness verifies anonymous requests are rejected, authenticated initialization
identifies this deployment, the expected tools are advertised, and one bounded
authenticated `vector_search` call can embed and query the selected Cosmos
container. **It does not prove Foundry-to-Function connectivity or guarantee
that a specific source has finished ingestion.** Success explicitly reports
these verification limits.

## Create a Foundry agent

Select a successfully deployed MCP job, the full resource ID of an **existing**
Foundry project and an existing chat deployment in that Foundry account.
Review and approve a separate plan.

The worker reads the project's actual endpoint, transfers the Function key
directly to a `CustomKeys` project connection as `x-functions-key`, and creates a
prompt agent restricted to `list_allowed_containers` and `vector_search`.
Function keys never enter browser responses, job metadata, prompts, logs or
source files. Treat secure connection management as privileged access.

No project, model deployment, conversation or inference request is created.
Agent creation is not an end-to-end retrieval test. A later, separately approved
inference test is needed to verify retrieval, model access and network reachability.

The **Test a deployed Foundry agent** form creates a separate approval plan for
one question against the saved agent version. It disables SDK retries, persists
the inference-start marker before the call, and never replays an uncertain
billable request. The completed job shows the answer and inspectable MCP
vector-search source references. An answer without retrieval evidence fails the
check. Source references do not by themselves certify the answer's factual
correctness; compare the answer with the source.

For fresh isolation, **MCP servers > Need a new isolated Cosmos vector container?**
can provision a new `/id`-partitioned cosine/DiskANN container in an existing
database through a separate approved plan. It refuses to adopt an existing
unowned container. Register the new container as a destination after completion.
It neither creates a Cosmos account/database nor changes their throughput.

See the [synthetic SharePoint example](../examples/sharepoint-foundry/README.md)
for the upload-once, all-remaining-steps-in-OmniVec workflow.

## Recovery and limitations

Jobs use `awaiting_approval`, `queued`, `running`, `succeeded`, `failed`, and
`interrupted` states. Metadata stores immutable plans, hashes, approvals, stages,
attempt counts and safe failure details. Approval binds the exact plan hash.
Unapproved plans expire after 30 minutes; changed destination/model metadata or
packaged code requires a new plan.

Workers claim jobs with Cosmos ETag concurrency control. Each API replica runs
at most one job at a time. Work has a 28-minute execution budget and a 30-minute
ownership lease, with bounded request/polling timeouts. While provisioning is
enabled, a lost worker is marked interrupted after lease expiry; another replica
does not silently replay it. A disabled worker does not poll metadata; expired
leases are reconciled when the feature is enabled again.

Retry requires another explicit approval and reuses the same owned resources.
There are at most three approved attempts. Unowned resource-name collisions
fail closed. Partial resources are **retained and may continue to incur charges**;
no automatic deletion or rollback occurs. Inspect their Azure Activity Logs and
Function deployment logs using the actual resource IDs in the plan.

Agent versions are reconciled using deployment ownership metadata. Once agent
creation has started, an ambiguous outcome cannot trigger another blind create:
if reconciliation cannot find the created version, the job requires operator
investigation. No automatic model inference occurs during recovery; an approved test is a
separate job and cannot be replayed after its inference-start marker is saved.

This first version supports Azure public cloud and public Function endpoints.
It does not provision networking for private-only data services, create resource
groups/projects/model deployments, rotate keys, or clean up deployments. Region
quota, Azure Policy, provider registration, RBAC propagation and data-service
firewalls can still block deployment. Permission denial and network timeout are
reported separately; do not add data roles to fix a network timeout.

The UI shows the latest 100 jobs; older records remain in metadata. Queued work
stays queued if operators disable provisioning; an executing job stops at the
next checked operation, but a request already accepted by Azure may complete.
No live deployment/inference certification is implied by the mocked deployment
and browser tests.
