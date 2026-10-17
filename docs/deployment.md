# Deployment and upgrades

[README](../README.md) | [User guide](user-guide.md) | [Architecture](architecture.md)

This guide covers the maintained Azure Developer CLI (`azd`) and Bicep deployment.
For a first deployment and pipeline, start with the [README](../README.md#quick-start).
OmniVec is a public preview; a successful deployment is not production-readiness
certification.

## Prerequisites

| Tool | Install | Verify |
|------|---------|--------|
| Azure CLI | [Instructions](https://aka.ms/install-azure-cli) | `az version` |
| Azure Developer CLI | [Instructions](https://aka.ms/install-azd) | `azd version` |
| PowerShell 7+ | [Instructions](https://aka.ms/install-powershell) | `pwsh --version` |
| Git | [Instructions](https://git-scm.com) | `git --version` |

Use PowerShell 7 on Windows, or the provided POSIX hooks on Linux/macOS.
Deployment hooks install `kubectl` and Helm if missing. You need Azure permissions
to provision the resources and role assignments in the templates, available
regional quota, and separate access to any external data/model resources.

The default quick start uses two `Standard_D4s_v5` system nodes, no GPU pool, and
Cosmos DB serverless metadata. AKS, storage, messaging, observability, and external
inference can incur charges. Review current regional pricing and quota before
deployment. B-series VMs are not supported for
[AKS system pools](https://learn.microsoft.com/azure/aks/use-system-pools#system-and-user-node-pools).

## Deploy a named environment

From the repository root:

```powershell
az login
azd auth login
azd env new my-omnivec
azd up
```

Choose **Quick start** for the defaults, or set the main choices before deployment:

```powershell
azd env set AZURE_LOCATION eastus2
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE Standard_D4s_v5
azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
azd env set OMNIVEC_GPU_NODE_VM_SIZE ""
azd env set OMNIVEC_GPU_NODE_COUNT 0
azd env set OMNIVEC_METADATA_STORE cosmosdb-serverless
azd env set OMNIVEC_SERVICE_BUS_SKU Standard
azd env set OMNIVEC_SERVICE_BUS_CAPACITY 1
azd up
```

Provisioning time depends on Azure capacity, permissions, image availability,
and model choices; do not treat a sample duration as a deadline or guarantee.

### What the hooks do

1. **Preprovision:** validate prerequisites, detect an existing environment, and
   collect missing configuration.
2. **Bicep:** reconcile Azure resources, including AKS, ACR, metadata Cosmos DB,
   Key Vault, storage, messaging, identity, and observability.
3. **Postprovision:** import/build images, deploy Helm releases, and check rollout
   convergence and the public health endpoint.

`azure.yaml` selects PowerShell hooks on Windows and shell hooks elsewhere.
`deploy.sh` is a compatibility wrapper around this path, not a separate Terraform
installer. Review a migration before using it on a Terraform-managed installation;
do not let both stacks manage the same resources.

### Find the UI and credentials

Use the URL reported by deployment, with `/ui` for the console. Retrieve the admin
token when needed rather than copying it into source files, issue reports, or
screenshots:

```powershell
azd env list
azd env get-value OMNIVEC_ADMIN_TOKEN
kubectl --kubeconfig "$HOME\.kube\omnivec-my-omnivec" get svc omnivec-web -n omnivec
```

On Bash, use `"$HOME/.kube/omnivec-my-omnivec"`. Replace the environment name.
Hooks keep this kubeconfig separate and do not switch your default context.
Never assume an unqualified `kubectl` command targets the OmniVec cluster.

The default web service exposes HTTP. Configure a trusted HTTPS endpoint before
sending credentials over an untrusted network; do not treat the preview's default
endpoint as hardened public ingress.

## Microsoft Entra sign-in

The console can use **Sign in with Microsoft**; the public browser origin must
be HTTPS. Configure TLS/DNS for the chosen origin as part of your ingress setup:

```powershell
azd env set OMNIVEC_ENTRA_ENABLED true
azd env set OMNIVEC_PUBLIC_URL https://omnivec.example.com
azd up
```

Setting `OMNIVEC_PUBLIC_URL` registers the origin; it is not a substitute for
configuring its TLS endpoint.

Postprovision creates or reconciles a single-tenant app registration, defines
`OmniVec.Admin`, `OmniVec.Operator`, and `OmniVec.Viewer` roles, assigns the
deploying user the admin role, and saves the client ID in the environment.
The deploying identity needs application-registration and enterprise-app
role-assignment permissions. Assign other users/groups through the reported
Enterprise application URL. The admin token remains available for break-glass
access; protect and rotate it according to your operational policy.

Console sign-in and connector access are different: Entra login does not grant
workers access to Cosmos DB, Blob, Graph, Fabric, or embedding resources.

## Environment reference

Use `azd env set NAME VALUE` unless a setting explicitly says **shell environment**.
Defaults below describe the deployment hooks, not every possible custom Helm
installation.

| Variable | Default / requirement | Purpose |
|----------|-----------------------|---------|
| `AZURE_LOCATION` | Required | Azure region |
| `OMNIVEC_SYSTEM_NODE_VM_SIZE` | Prompted; quick start `Standard_D4s_v5` | Non-burstable system-pool SKU, at least 4 vCPUs |
| `OMNIVEC_SYSTEM_NODE_COUNT` | `2` | System nodes |
| `OMNIVEC_GPU_NODE_VM_SIZE` | Empty | Optional GPU SKU |
| `OMNIVEC_GPU_NODE_COUNT` | `0` | GPU nodes; zero for external-model-only use |
| `OMNIVEC_METADATA_STORE` | Prompted | `cosmosdb-serverless` or `cosmosdb-provisioned` |
| `OMNIVEC_SERVICE_BUS_SKU` | `Standard` | `Standard` or `Premium`; size for workload and message limits |
| `OMNIVEC_SERVICE_BUS_CAPACITY` | `1` | Premium messaging units: 1, 2, 4, 8, or 16; ignored for Standard |
| `OMNIVEC_IMAGE_TAG` | `stable` | Shared-image channel/tag to import |
| `OMNIVEC_SHARED_REGISTRY_TOKEN` | Prompted if needed | Access to shared images; source build is an alternative |
| `OMNIVEC_BUILD_MODE` | Auto-detect | `acr` (cloud build) or `docker` (local build) |
| `OMNIVEC_BUILD` | `false` | Request source builds |
| `OMNIVEC_BUILD_CONCURRENCY` | `3` | ACR build parallelism, 1-5 |
| `OMNIVEC_SKIP_IMPORT` | `false` | Require existing local ACR images; fail if any are missing |
| `OMNIVEC_FORCE_IMPORT` | `false`; shell environment | Refresh local tags from the selected shared channel |
| `OMNIVEC_RECOVER_PENDING_HELM` | Enabled | Set false to require manual pending-release recovery |
| `OMNIVEC_SHAREPOINT_ENABLED` | `false` | Deploy the SharePoint watcher |
| `OMNIVEC_ONELAKE_ICEBERG_ENABLED` | `false` | Deploy the OneLake Iceberg watcher, not its separate Fabric CDF job |
| `OMNIVEC_AGENT_DEFAULT_MODEL_ID` | Empty | Existing chat-model ID; does not register a model or grant access |
| `OMNIVEC_AGENT_IMAGE_TAG` | `latest` | Pin an already-published agent tag in the environment's ACR |
| `OMNIVEC_AGENT_ALLOW_K8S_REMEDIATION` | `false` | Enable namespace-scoped agent restart/scale permissions; actions still require approval |
| `OMNIVEC_ADMIN_TOKEN` | Generated | Admin bearer token |
| `OMNIVEC_ENTRA_ENABLED` | `false` | Reconcile Entra login configuration |
| `OMNIVEC_PUBLIC_URL` | Required for Entra | Public HTTPS origin |
| `OMNIVEC_ENTRA_CLIENT_ID` | Created if needed | Reuse an existing OmniVec app registration |

Connector grants are separate from watcher flags. Follow the
[SharePoint](sharepoint-source.md) and [OneLake](onelake-iceberg.md) guides before
enabling their watchers. Databricks has a separate `databricksWatcher.enabled`
Helm setting.

## Update or recover a deployment

Review configuration changes, select the intended environment, then rerun
`azd up`. Do not deploy concurrently to the same environment.

The hooks preserve completed image work, recover eligible stale local locks,
reconcile pending Helm operations, retry transient Azure/AKS transport errors,
and check actual convergence after an initial rollout timeout. They do not adopt
unrelated Kubernetes resources. Configuration, authorization, image, and workload
failures still stop with diagnostics.

### Imported images

Existing local ACR `:latest` images are normally preserved, not compared with the
shared registry. To refresh from the selected channel, set the override in the
calling shell:

```powershell
$env:OMNIVEC_FORCE_IMPORT = "true"
azd up
Remove-Item Env:\OMNIVEC_FORCE_IMPORT
```

For Bash:

```bash
OMNIVEC_FORCE_IMPORT=true azd up
```

Interrupted updates retain a local recovery marker so a retry still restarts
affected workloads. Partial imports fail rather than silently accepting mixed
image versions. ACR checks retry transient failures instead of classifying them
as missing images.

### Source builds and code-only updates

```powershell
azd env set OMNIVEC_BUILD true
azd up
```

Source builds use minimal per-image contexts and immutable `src-<fingerprint>`
tags. Matching fingerprints are reused; independent images build with bounded
concurrency. Helm rolls workloads whose image tags changed.

For an already-provisioned environment, skip Bicep reconciliation with:

```powershell
.\deploy.ps1 -CodeOnly --environment my-omnivec
```

POSIX equivalent:

```bash
./deploy.sh --code-only --environment my-omnivec
```

Code-only updates still run the resumable image/Helm/health path. They do not
provision missing infrastructure or external connector grants.

### Recovery boundaries

- Pending Helm install/upgrade/rollback recovery occurs after acquiring the local
  environment lock. Set `OMNIVEC_RECOVER_PENDING_HELM=false` for manual recovery.
- Windows locks are released by the OS. POSIX hooks remove a same-host stale lock
  only if its PID has stopped; they refuse to take over another host's lock.
  These locks do not serialize the entire Bicep deployment across machines.
- Resource-group tags retain configuration needed by another checkout, including
  image and optional watcher settings. Authentication tokens remain local and
  are omitted from deployment output.
- Image imports have a 15-minute deadline; POSIX imports may retry once. ACR
  source builds have a one-hour server-side limit. A client timeout does not prove
  a remote operation was cancelled: inspect it before retrying.
- Helm dependencies use an isolated disposable cache. Readiness deadlines stop
  retries; diagnose stalled workloads instead of repeatedly applying them.

Success requires every deployment to roll out, a web external IP, and a healthy
public `/health`. That proves deployment reachability, not document ingestion,
search quality, or connector write/delete access. Complete the README's
[end-to-end verification](../README.md#verify-the-result) separately.

## Release channels

| Git ref | Shared tags | Intended use |
|---------|-------------|--------------|
| `main` | `stable`, `sha-<short>` | Stable testing channel |
| `dev` | `dev`, `sha-<short>` | Active development |
| `v*` tag | `stable`, version tag, `sha-<short>` | Versioned release |

To select the development channel, set `OMNIVEC_IMAGE_TAG=dev` in the azd
environment. Existing imported local tags may still need the refresh described
above. A channel name is not a production-support guarantee.

The image matrix and signing steps live in
[build-images.yml](../.github/workflows/build-images.yml). Registry credentials
need access to the repositories in that matrix; do not assume a fixed image
count. Promotion is a reviewed merge/tag operation, not part of routine deployment.

## Remove an environment

**Destructive:** `azd down` deletes resources managed by the selected environment.
Inspect the environment and back up required data first. Its `--purge` and
`--force` options remove recovery/confirmation safeguards; do not use them as
routine troubleshooting.

External source accounts, embedding deployments, Fabric jobs, MCP/Foundry
resources created separately, and their charges may remain. Deleting OmniVec
does not automatically erase all derived data in those services.
