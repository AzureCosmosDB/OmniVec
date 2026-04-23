#!/usr/bin/env pwsh
# OmniVec E2E Demo — Azure Blob (txt) → Cosmos DB (vectors)
#
# Exercises the full pipeline against an existing azd deployment:
#   1. Upload sample .txt files to a blob container
#   2. Register an embedding model (Azure OpenAI)
#   3. Create an azure-blob source + cosmosdb-vector destination
#   4. Create + activate a queue-mode pipeline
#   5. Poll until vectors land in the destination container
#   6. Run a semantic query via the omnivec-search service
#
# Prereqs:
#   - azd environment already provisioned (azd up) — pass -Env <name>
#   - Azure CLI signed in to the same subscription
#   - Azure OpenAI resource with a text-embedding deployment

[CmdletBinding()]
param(
    [string]$Env,
    [string]$AdminToken,
    [string]$AoaiEndpoint,
    [string]$AoaiKey,
    [string]$AoaiDeployment = "text-embedding-3-small",
    [int]$AoaiDims = 1536,
    [string]$Container = "e2e-blob-txt",
    [string]$SamplesDir,
    [switch]$Cleanup,
    [switch]$NoSearch
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false  # we handle az/kubectl errors explicitly

# ── Logging helpers ─────────────────────────────────────────────────────────
function Log      { param($m) Write-Host "  $m" }
function LogStep  { param($n, $m) Write-Host "`n`e[36m─── Step $n : $m`e[0m" }
function LogOk    { param($m) Write-Host "  `e[32m✓`e[0m $m" }
function LogWarn  { param($m) Write-Host "  `e[33m!`e[0m $m" }
function LogErr   { param($m) Write-Host "  `e[31m✗`e[0m $m" -ForegroundColor Red }

function Get-AzdValue {
    param($Key)
    try { (azd env get-values --output json 2>$null | ConvertFrom-Json).$Key } catch { $null }
}

function Invoke-Api {
    param($Method, $Path, $Body)
    $uri = "$script:SERVER_URL$Path"
    $headers = @{
        "Authorization" = "Bearer $script:ADMIN_TOKEN"
        "Content-Type"  = "application/json"
    }
    if ($Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers `
            -Body ($Body | ConvertTo-Json -Depth 10) -TimeoutSec 60
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 60
}

# ── Banner ──────────────────────────────────────────────────────────────────
Write-Host "`n`e[32m╔═══════════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║  OmniVec E2E Demo — Azure Blob (txt) → Cosmos DB Vectors  ║`e[0m"
Write-Host "`e[32m╚═══════════════════════════════════════════════════════════╝`e[0m"

# ── Defaults ────────────────────────────────────────────────────────────────
if (-not $SamplesDir) {
    $SamplesDir = Join-Path $PSScriptRoot "samples\blob-txt"
}

function Ensure-Samples {
    param([string]$Dir)
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    @'
Azure Cosmos DB Overview

Azure Cosmos DB is a fully managed, globally distributed, multi-model database
service built for modern app development. It provides turnkey global
distribution across any number of Azure regions, elastic scale-out of both
throughput and storage, and single-digit-millisecond read and write latencies
at the 99th percentile. Cosmos DB offers multiple APIs including NoSQL (SQL),
MongoDB, Cassandra, Gremlin (graph), and Table. Integrated vector search over
the NoSQL API makes it a strong fit for retrieval-augmented generation (RAG)
workloads where the application data and its embeddings live side-by-side.

Key features:
- Guaranteed low latency with five consistency levels
- Automatic and instant scalability
- Serverless and provisioned throughput modes
- Native vector indexes: flat, quantizedFlat, diskANN
- Change feed for event-driven processing
'@ | Set-Content -Path (Join-Path $Dir "azure-cosmos-db.txt") -Encoding UTF8
    @'
Azure Blob Storage

Azure Blob Storage is Microsoft's object storage solution for the cloud. Blob
Storage is optimized for storing massive amounts of unstructured data such as
text or binary files: documents, images, audio, video, logs, and backups.

Access tiers:
- Hot: Optimized for frequently accessed data
- Cool: Lower storage cost, higher access cost; for infrequently accessed data
- Archive: Lowest storage cost, highest access cost; for rarely accessed data

Event Grid integration emits BlobCreated / BlobDeleted events that can drive
real-time ingestion pipelines — for example, producing vector embeddings in
Azure Cosmos DB or pgvector the moment a new document lands in a container.
This is the foundation for OmniVec's blob-source ingestion path: Event Grid
delivers the blob URL to the API, which creates a job; a worker downloads the
file, chunks and embeds its text, and writes vectors to the configured
destination store.
'@ | Set-Content -Path (Join-Path $Dir "azure-blob-storage.txt") -Encoding UTF8
    @'
Azure Kubernetes Service (AKS)

Azure Kubernetes Service simplifies deploying a managed Kubernetes cluster in
Azure by offloading the operational overhead to Azure. As a hosted Kubernetes
service, Azure handles critical tasks like health monitoring and maintenance.
You only manage and maintain the agent nodes.

Common AKS use cases include:
- Running microservices with horizontal pod autoscaling (HPA)
- Hosting web applications behind a LoadBalancer or ingress controller
- Workload identity federation with Entra ID for passwordless Azure auth
- GPU-backed ML inference pods using Kubernetes node pools with GPUs
- Running stateful workloads via persistent volumes backed by Azure Disks
  or Azure Files

AKS integrates with Azure Monitor, Microsoft Entra ID, Azure Policy, and
Azure Key Vault for end-to-end observability, identity, and secret management.
'@ | Set-Content -Path (Join-Path $Dir "azure-kubernetes-service.txt") -Encoding UTF8
}

$hasTxt = (Test-Path $SamplesDir) -and (@(Get-ChildItem -Path $SamplesDir -Filter *.txt -ErrorAction SilentlyContinue).Count -gt 0)
if (-not $hasTxt) {
    LogWarn "Samples directory missing or empty — generating defaults at: $SamplesDir"
    Ensure-Samples -Dir $SamplesDir
    LogOk "Created 3 sample .txt files."
}

# ── Select azd environment ──────────────────────────────────────────────────
if ($Env) {
    azd env select $Env | Out-Null
    LogOk "Using azd env: $Env"
} else {
    $current = (azd env list --output json 2>$null | ConvertFrom-Json) | Where-Object IsDefault
    if (-not $current) {
        LogErr "No azd environment selected. Pass -Env <name> or run azd env select."
        exit 1
    }
    LogOk "Using azd env: $($current.Name)"
}

# ── Resolve deployment details ──────────────────────────────────────────────
LogStep 1 "Resolving deployment details from azd"
$RESOURCE_GROUP = Get-AzdValue "AZURE_RESOURCE_GROUP"
$STORAGE_ACCT   = Get-AzdValue "AZURE_STORAGE_ACCOUNT_NAME"
$BLOB_ENDPOINT  = Get-AzdValue "AZURE_STORAGE_BLOB_ENDPOINT"
$IDENTITY_CID   = Get-AzdValue "AZURE_IDENTITY_CLIENT_ID"
if (-not $IDENTITY_CID) { $IDENTITY_CID = Get-AzdValue "OMNIVEC_IDENTITY_CLIENT_ID" }
if (-not $AdminToken)   { $AdminToken   = Get-AzdValue "OMNIVEC_ADMIN_TOKEN" }

foreach ($pair in @(
    @("AZURE_RESOURCE_GROUP", $RESOURCE_GROUP),
    @("AZURE_STORAGE_ACCOUNT_NAME", $STORAGE_ACCT),
    @("OMNIVEC_ADMIN_TOKEN", $AdminToken)
)) {
    if (-not $pair[1]) {
        LogErr "Missing azd env value: $($pair[0]). Run 'azd up' first or pass flags."
        exit 1
    }
}
$script:ADMIN_TOKEN = $AdminToken

# Cosmos endpoint — prefer the OmniVec infra account (already has vector capability)
$COSMOS_ENDPOINT = Get-AzdValue "AZURE_COSMOS_ENDPOINT"
if (-not $COSMOS_ENDPOINT) {
    $COSMOS_ENDPOINT = az cosmosdb list --resource-group $RESOURCE_GROUP `
        --query "[?contains(name,'omnivec-cosmos')].documentEndpoint | [0]" -o tsv 2>$null
}
if (-not $COSMOS_ENDPOINT) {
    LogErr "Could not locate OmniVec Cosmos account in RG $RESOURCE_GROUP"
    exit 1
}

# External IP — omnivec-api is ClusterIP; go through omnivec-web which proxies /api/*
# Ensure kubectl is available; install via 'az aks install-cli' if missing.
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    $kubectlLocal = Join-Path $HOME ".azure-kubectl/kubectl"
    if (Test-Path $kubectlLocal) {
        $env:PATH = "$(Split-Path $kubectlLocal)" + [IO.Path]::PathSeparator + $env:PATH
    }
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    LogWarn "kubectl not found — installing via 'az aks install-cli'..."
    $kubectlDir = Join-Path $HOME ".azure-kubectl"
    New-Item -ItemType Directory -Force -Path $kubectlDir | Out-Null
    az aks install-cli --install-location (Join-Path $kubectlDir "kubectl") --only-show-errors 2>&1 | Out-Null
    $env:PATH = "$kubectlDir" + [IO.Path]::PathSeparator + $env:PATH
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        LogErr "Failed to install kubectl. Install manually and re-run."
        exit 1
    }
    LogOk "kubectl installed at $kubectlDir"
}

$kubeCtx = az aks list --resource-group $RESOURCE_GROUP --query "[0].name" -o tsv
if (-not $kubeCtx) { LogErr "No AKS cluster found in RG $RESOURCE_GROUP"; exit 1 }
try {
    az aks get-credentials --resource-group $RESOURCE_GROUP --name $kubeCtx `
        --overwrite-existing --only-show-errors 2>&1 | Out-Null
} catch {}
$EXT_IP = kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
if (-not $EXT_IP) {
    $EXT_IP = kubectl get svc omnivec-api -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
}
if (-not $EXT_IP) {
    LogErr "No external IP found on omnivec-web or omnivec-api — is the cluster up?"
    exit 1
}
$script:SERVER_URL = "http://$EXT_IP"
$SEARCH_IP = kubectl get svc omnivec-search -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
$SEARCH_TOKEN = Get-AzdValue "OMNIVEC_SEARCH_TOKEN"

LogOk "RG              : $RESOURCE_GROUP"
LogOk "Storage account : $STORAGE_ACCT"
LogOk "Cosmos endpoint : $COSMOS_ENDPOINT"
LogOk "API             : $script:SERVER_URL"
if ($SEARCH_IP) { LogOk "Search          : http://$SEARCH_IP" } else { LogWarn "omnivec-search external IP not yet available" }

# ── Validate API health + token ─────────────────────────────────────────────
LogStep 2 "Validating API + admin token"
try {
    Invoke-RestMethod -Uri "$script:SERVER_URL/health" -TimeoutSec 10 | Out-Null
} catch {
    LogErr "API /health unreachable at $script:SERVER_URL"
    exit 1
}
try {
    Invoke-Api GET "/api/auth/whoami" | Out-Null
    LogOk "Admin token valid"
} catch {
    # /auth/whoami may not exist — fall back to listing sources
    try { Invoke-Api GET "/api/sources" | Out-Null; LogOk "Admin token accepted" }
    catch { LogErr "Admin token rejected by API"; exit 1 }
}

# ── AOAI creds ──────────────────────────────────────────────────────────────
if (-not $AoaiEndpoint) { $AoaiEndpoint = Read-Host "  Azure OpenAI endpoint (https://<res>.openai.azure.com)" }
if (-not $AoaiKey)      { $AoaiKey      = Read-Host "  Azure OpenAI API key" -AsSecureString | ForEach-Object { [System.Runtime.InteropServices.Marshal]::PtrToStringAuto([System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) } }
if (-not $AoaiEndpoint -or -not $AoaiKey) { LogErr "AOAI endpoint + key required"; exit 1 }

# ── Register embedding model (idempotent) ───────────────────────────────────
LogStep 3 "Registering Azure OpenAI embedding model"
$MODEL_NAME = "e2e-blob-embed"
$existingModels = try { Invoke-Api GET "/api/models" } catch { @{ models = @() } }
$MODEL_ID = ($existingModels.models | Where-Object { $_.name -eq $MODEL_NAME } | Select-Object -First 1).id
if (-not $MODEL_ID) {
    $modelBody = @{
        name        = $MODEL_NAME
        type        = "azure-openai"
        endpoint    = $AoaiEndpoint
        api_key     = $AoaiKey
        model       = $AoaiDeployment
        deployment  = $AoaiDeployment
        dimensions  = $AoaiDims
        api_version = "2024-06-01"
    }
    $r = Invoke-Api POST "/api/models" $modelBody
    $MODEL_ID = $r.id
    LogOk "Registered model: $MODEL_ID ($AoaiDeployment, ${AoaiDims}d)"
} else {
    LogOk "Re-using existing model: $MODEL_ID"
}

# ── Blob container + upload samples ─────────────────────────────────────────
LogStep 4 "Preparing blob container + uploading samples"
# Check whether this storage account allows shared-key auth (many secure
# defaults disable it). If disabled, fall back to AAD (the caller needs
# Storage Blob Data Contributor on the account).
$allowKey = az storage account show --name $STORAGE_ACCT --resource-group $RESOURCE_GROUP `
    --query "allowSharedKeyAccess" -o tsv 2>$null
if ($allowKey -eq "true") {
    $storageKey = az storage account keys list --account-name $STORAGE_ACCT `
        --resource-group $RESOURCE_GROUP --query "[0].value" -o tsv 2>$null
    $authArgs = @("--account-key", $storageKey)
    Log "Using shared-key auth for local upload"
} else {
    $authArgs = @("--auth-mode", "login")
    Log "Shared keys disabled — using AAD (signed-in user) for local upload"
    # Make sure the caller has the role — grant if missing (best-effort).
    $me = az ad signed-in-user show --query id -o tsv 2>$null
    $saId = az storage account show --name $STORAGE_ACCT --resource-group $RESOURCE_GROUP --query id -o tsv 2>$null
    if ($me -and $saId) {
        $hasRole = az role assignment list --assignee $me --scope $saId `
            --role "Storage Blob Data Contributor" --query "[0].id" -o tsv 2>$null
        if (-not $hasRole) {
            Log "Granting 'Storage Blob Data Contributor' to signed-in user (one-time)"
            az role assignment create --assignee-object-id $me --assignee-principal-type User `
                --role "Storage Blob Data Contributor" --scope $saId --only-show-errors 2>$null | Out-Null
            Log "Waiting 45s for RBAC propagation..."
            Start-Sleep -Seconds 45
        }
    }
}
az storage container create `
    --account-name $STORAGE_ACCT `
    --name $Container `
    @authArgs `
    --only-show-errors | Out-Null
LogOk "Container ready: $Container"

$samples = Get-ChildItem -Path $SamplesDir -Filter *.txt
if (-not $samples) { LogErr "No .txt samples in $SamplesDir"; exit 1 }
foreach ($f in $samples) {
    $uploadOut = az storage blob upload `
        --account-name $STORAGE_ACCT `
        --container-name $Container `
        --name $f.Name `
        --file $f.FullName `
        @authArgs `
        --overwrite `
        --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        LogWarn "Upload failed for $($f.Name) (rc=$LASTEXITCODE). Waiting 30s for RBAC propagation and retrying..."
        Start-Sleep -Seconds 30
        $uploadOut = az storage blob upload `
            --account-name $STORAGE_ACCT `
            --container-name $Container `
            --name $f.Name `
            --file $f.FullName `
            @authArgs `
            --overwrite `
            --only-show-errors 2>&1
    }
    if ($LASTEXITCODE -ne 0) {
        LogErr "Upload failed for $($f.Name) after retry:"
        Write-Host $uploadOut
        exit 1
    }
    LogOk "Uploaded $($f.Name)"
}

# ── Cosmos database + vectors container ─────────────────────────────────────
LogStep 5 "Ensuring Cosmos database + vectors container"
$COSMOS_ACCT = ($COSMOS_ENDPOINT -replace "https://", "" -split "\.")[0]
$DB_NAME = "e2eblob"
$VEC_CONTAINER = "vectors"

az cosmosdb sql database create --account-name $COSMOS_ACCT --resource-group $RESOURCE_GROUP `
    --name $DB_NAME --only-show-errors 2>$null | Out-Null

# Create vectors container with vector embedding policy via API pod (reuse api-pod exec)
$apiPod = kubectl get pods -n omnivec -l app=omnivec-api -o jsonpath='{.items[0].metadata.name}' 2>$null
if (-not $apiPod) { LogErr "No omnivec-api pod running"; exit 1 }
$pyScript = @"
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$COSMOS_ENDPOINT", credential=cred)
db = client.get_database_client("$DB_NAME")
vp = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": $AoaiDims}]}
ip = {"vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
try:
    db.create_container(id="$VEC_CONTAINER", partition_key={"paths": ["/id"], "kind": "Hash"}, vector_embedding_policy=vp, indexing_policy=ip)
    print("OK: vectors container created")
except Exception as e:
    if "Conflict" in str(e) or "already exists" in str(e).lower():
        print("OK: vectors container already exists")
    else:
        print(f"ERR: {e}")
        raise
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pyScript))
$out = kubectl exec -n omnivec $apiPod -- sh -c "echo $encoded | base64 -d | python3 -" 2>&1
if ($out -match "OK:") { LogOk ($out -join " ").Trim() } else { LogErr "Vectors container setup failed: $out"; exit 1 }

# ── Source + destination + pipeline ─────────────────────────────────────────
LogStep 6 "Creating source, destination, and pipeline"
$SOURCE_NAME = "e2e-blob-source"
$DEST_NAME   = "e2e-blob-dest"
$PIPE_NAME   = "e2e-blob-pipeline"

# Clean up existing demo objects for idempotency
foreach ($kind in @("pipelines", "sources", "destinations")) {
    try {
        $list = Invoke-Api GET "/api/$kind"
        $items = $list.$kind
        foreach ($it in $items) {
            if ($it.name -in @($SOURCE_NAME, $DEST_NAME, $PIPE_NAME)) {
                try { Invoke-Api DELETE "/api/$kind/$($it.id)" | Out-Null } catch {}
            }
        }
    } catch {}
}

$srcBody = @{
    name = $SOURCE_NAME
    type = "azure-blob"
    config = @{
        account_url = $BLOB_ENDPOINT
        container   = $Container
        file_type   = "txt"
        auth_type   = "managed-identity"
    }
}
$src = Invoke-Api POST "/api/sources" $srcBody
$SOURCE_ID = $src.source.id
if (-not $SOURCE_ID) { $SOURCE_ID = $src.id }
if (-not $SOURCE_ID) { LogErr "Source creation returned no id. Response: $($src | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Source: $SOURCE_ID"

$dstBody = @{
    name = $DEST_NAME
    type = "cosmosdb-vector"
    config = @{
        endpoint          = $COSMOS_ENDPOINT
        database          = $DB_NAME
        container         = $VEC_CONTAINER
        auth_type         = "managed-identity"
        client_id         = $IDENTITY_CID
        vector_dimensions = $AoaiDims
        vector_field      = "embedding"
    }
}
$dst = Invoke-Api POST "/api/destinations" $dstBody
$DEST_ID = $dst.destination.id
if (-not $DEST_ID) { $DEST_ID = $dst.id }
if (-not $DEST_ID) { LogErr "Destination creation returned no id. Response: $($dst | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Destination: $DEST_ID"

# ── DocGrok pipelines: register docgrok-text and docgrok-pdf (idempotent) ───
# Both pipelines route via the pipeline-worker service; the worker auto-detects
# text vs PDF by blob extension. Having two named pipelines lets OmniVec users
# pick the right one in the UI and makes routing explicit.
$WORKER_URL = "http://pipeline-worker-svc.omnivec.svc.cluster.local:8080"
$dgTextDisplayName = "DocGrok Text"
$dgPdfDisplayName  = "DocGrok PDF"

function Register-DocGrokPipeline($displayName, $modelId) {
    $body = '{"name":"' + $displayName + '","worker_url":"' + $WORKER_URL + '","model_id":"' + $modelId + '","type":"embedding"}'
    try {
        $resp = $body | kubectl exec -i -n omnivec $apiPod -- curl -sS -X POST "http://docgrok.omnivec.svc.cluster.local/admin/pipelines" -H "content-type: application/json" --data-binary "@-" 2>&1
        $obj = $resp | ConvertFrom-Json
        LogOk "DocGrok pipeline registered: $displayName -> id=$($obj.id) (model=$modelId)"
        return $obj.id
    } catch {
        LogWarn "DocGrok pipeline $displayName registration failed: $($_.Exception.Message)"
        return $null
    }
}
$dgTextId = Register-DocGrokPipeline $dgTextDisplayName $MODEL_ID
$dgPdfId  = Register-DocGrokPipeline $dgPdfDisplayName  $MODEL_ID

$pipBody = @{
    name = $PIPE_NAME
    sources = @(@{
        source_id      = $SOURCE_ID
        filters        = @{}
        content_fields = @("content")
        file_types     = @("txt")
    })
    destination_id    = $DEST_ID
    docgrok_pipeline  = $dgTextId
    vector_index_path = "embedding"
    process_existing  = $true
    processing_mode   = "queue"
}
$pipe = Invoke-Api POST "/api/pipelines" $pipBody
$PIPE_ID = $pipe.pipeline.id
if (-not $PIPE_ID) { $PIPE_ID = $pipe.id }
if (-not $PIPE_ID) { LogErr "Pipeline creation returned no id. Response: $($pipe | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Pipeline: $PIPE_ID (queue mode)"

# ── Activate pipeline and poll for vectors ──────────────────────────────────
LogStep 7 "Activating pipeline and waiting for embeddings"
Invoke-Api POST "/api/sources/$SOURCE_ID/sync" @{} | Out-Null
LogOk "Pipeline activated — controller will enumerate blobs"

$expected = $samples.Count
$deadline = (Get-Date).AddMinutes(5)
$lastCount = -1
while ((Get-Date) -lt $deadline) {
    $countScript = @"
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$COSMOS_ENDPOINT", credential=cred)
c = client.get_database_client("$DB_NAME").get_container_client("$VEC_CONTAINER")
q = list(c.query_items("SELECT VALUE COUNT(1) FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True))
print(f"COUNT={q[0]}")
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($countScript))
    $out = kubectl exec -n omnivec $apiPod -- sh -c "echo $encoded | base64 -d | python3 -" 2>&1 | Out-String
    if ($out -match 'COUNT=(\d+)') {
        $n = [int]$Matches[1]
        if ($n -ne $lastCount) {
            Log "  vectors embedded: $n / $expected"
            $lastCount = $n
        }
        if ($n -ge $expected) { LogOk "All $expected files embedded"; break }
    }
    Start-Sleep -Seconds 10
}
if ($lastCount -lt $expected) {
    LogWarn "Only $lastCount / $expected vectors after 5 minutes. Check: kubectl logs -n omnivec deploy/omnivec-controller"
}

# ── Query via omnivec-search ────────────────────────────────────────────────
if (-not $NoSearch -and $SEARCH_IP -and $SEARCH_TOKEN) {
    LogStep 8 "Querying via omnivec-search"
    $searchBody = @{
        query  = "how does kubernetes help run microservices"
        top_k  = 3
        indexes = @(@{
            id = "e2e-blob"
            store = @{
                type      = "cosmosdb"
                endpoint  = $COSMOS_ENDPOINT
                database  = $DB_NAME
                container = $VEC_CONTAINER
                auth      = @{ mode = "managed_identity" }
            }
            vector = @{ field = "embedding"; dims = $AoaiDims; metric = "cosine" }
            embedding = @{ policy = "model"; model_id = $MODEL_ID }
            content_fields = @("content")
        })
        merge = @{ strategy = "rrf" }
    }
    try {
        $resp = Invoke-RestMethod -Method POST -Uri "http://$SEARCH_IP/search" `
            -Headers @{ "Authorization" = "Bearer $SEARCH_TOKEN"; "Content-Type" = "application/json" } `
            -Body ($searchBody | ConvertTo-Json -Depth 10) -TimeoutSec 30
        LogOk "Got $($resp.results.Count) result(s):"
        $resp.results | Select-Object -First 3 | ForEach-Object {
            $txt = if ($_.text) { $_.text.Substring(0, [Math]::Min(80, $_.text.Length)) } else { "" }
            Log "    [$($_.rank)] score=$([Math]::Round([double]$_.score, 4))  $txt..."
        }
    } catch {
        LogWarn "Search query failed: $($_.Exception.Message)"
    }
} elseif ($NoSearch) {
    LogWarn "Skipping search (-NoSearch passed)"
} else {
    LogWarn "Skipping search (no IP or token)"
}

# ── Cleanup ─────────────────────────────────────────────────────────────────
if ($Cleanup) {
    LogStep 9 "Cleanup"
    foreach ($kind in @("pipelines", "sources", "destinations")) {
        try {
            $list = Invoke-Api GET "/api/$kind"
            foreach ($it in $list.$kind) {
                if ($it.name -in @($SOURCE_NAME, $DEST_NAME, $PIPE_NAME)) {
                    Invoke-Api DELETE "/api/$kind/$($it.id)" | Out-Null
                }
            }
        } catch {}
    }
    az storage container delete --account-name $STORAGE_ACCT --name $Container `
        --auth-mode login --only-show-errors 2>$null | Out-Null
    LogOk "Demo objects deleted"
}

Write-Host "`n`e[32m╔══════════════════════════╗`e[0m"
Write-Host "`e[32m║  E2E demo completed      ║`e[0m"
Write-Host "`e[32m╚══════════════════════════╝`e[0m`n"
Write-Host "  Source container : $Container ($($samples.Count) files)"
Write-Host "  Destination      : $DB_NAME/$VEC_CONTAINER @ $COSMOS_ACCT"
Write-Host "  Pipeline         : $PIPE_ID"
if ($SEARCH_IP) { Write-Host "  Search service   : http://$SEARCH_IP" }
