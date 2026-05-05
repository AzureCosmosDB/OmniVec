#!/usr/bin/env pwsh
# OmniVec E2E Demo — Azure Blob (txt or pdf) → Cosmos DB (vectors)
#
# Windows/PowerShell mirror of scripts/e2e-blob-demo.sh. Keeps the
# same shape: dedicated demo RG + AAD-only SA per env (policy-safe),
# uploads via an in-cluster K8s Job that inherits workload identity,
# registers the Azure-OpenAI embedding model, creates source /
# destination / pipeline, activates and polls until embeddings land.
#
# Usage:
#   pwsh scripts/e2e-blob-demo.ps1 -Env my-omnivec -FileType txt
#   pwsh scripts/e2e-blob-demo.ps1 -Env my-omnivec -FileType pdf `
#       -AoaiEndpoint https://my-aoai.openai.azure.com -AoaiKey $env:AOAI_KEY

[CmdletBinding()]
param(
    [string]$Env,
    [ValidateSet("txt","pdf")] [string]$FileType = "txt",
    [string]$AdminToken,
    [string]$AoaiEndpoint,
    [string]$AoaiKey,
    [string]$AoaiDeployment = "text-embedding-3-small",
    [int]$AoaiDims = 1536,
    [string]$Container,
    [string]$SamplesDir,
    [switch]$SkipQueue,
    [switch]$Cleanup,
    [switch]$NoSearch
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false  # handle az/kubectl errors explicitly

# ── Defaults ────────────────────────────────────────────────────────────────
$FileType = $FileType.ToLower()
if (-not $Container)   { $Container   = "e2e-blob-$FileType" }
if (-not $SamplesDir)  { $SamplesDir  = Join-Path $PSScriptRoot "samples\blob-$FileType" }
if (-not $AdminToken -and $env:OMNIVEC_ADMIN_TOKEN) { $AdminToken   = $env:OMNIVEC_ADMIN_TOKEN }
if (-not $AoaiEndpoint -and $env:AOAI_ENDPOINT)     { $AoaiEndpoint = $env:AOAI_ENDPOINT }
if (-not $AoaiKey      -and $env:AOAI_KEY)          { $AoaiKey      = $env:AOAI_KEY }

# ── Logging helpers ─────────────────────────────────────────────────────────
function Log      { param($m) Write-Host "  $m" }
function LogStep  { param($n, $m) Write-Host "`n`e[36m─── Step $n : $m`e[0m" }
function LogOk    { param($m) Write-Host "  `e[32m✓`e[0m $m" }
function LogWarn  { param($m) Write-Host "  `e[33m!`e[0m $m" }
function LogErr   { param($m) [Console]::Error.WriteLine("  `e[31m✗`e[0m $m") }

function Get-AzdValue {
    param($Key)
    $v = & azd env get-value $Key 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $v) { return "" }
    $s = ($v -join "").Trim() -replace "`r|`n",""
    # Guard against azd printing an "ERROR: ..." message to stdout on missing keys.
    if ($s -match '^(ERROR|Suggestion:|key not found)') { return "" }
    return $s
}

# Stdout-only API calls (so $( ... ) captures only the response body).
# Token/URL are read from script: scope variables.
function Invoke-ApiCall {
    param($Method, $Path, $Body)
    $uri = "$script:SERVER_URL$Path"
    $headers = @{
        "Authorization" = "Bearer $script:ADMIN_TOKEN"
        "Content-Type"  = "application/json"
    }
    try {
        if ($Body) {
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers `
                -Body (ConvertTo-Json -InputObject $Body -Depth 20 -Compress) -TimeoutSec 60
        }
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 60
    } catch {
        LogErr "API $Method $Path failed: $($_.Exception.Message)"
        throw
    }
}

function Invoke-ApiTry {
    param($Method, $Path, $Body)
    try { return Invoke-ApiCall -Method $Method -Path $Path -Body $Body } catch { return $null }
}

# ── Banner ──────────────────────────────────────────────────────────────────
Write-Host "`n`e[32m╔═══════════════════════════════════════════════════════════╗`e[0m"
Write-Host ("`e[32m║  OmniVec E2E Demo — Azure Blob ({0,-3}) → Cosmos DB Vectors  ║`e[0m" -f $FileType)
Write-Host "`e[32m╚═══════════════════════════════════════════════════════════╝`e[0m"

# ── Samples ─────────────────────────────────────────────────────────────────
function Ensure-SamplesTxt {
    param($Dir)
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

function Ensure-SamplesPdf {
    param($Dir)
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $gen = Join-Path $PSScriptRoot "gen_sample_pdfs.py"
    if (-not (Test-Path $gen)) { LogErr "gen_sample_pdfs.py not found at $PSScriptRoot"; exit 1 }
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) { LogErr "python/python3 not found — cannot generate PDF samples"; exit 1 }
    & $py.Path $gen $Dir | Out-Null
}

if ($FileType -eq "pdf") {
    $hasPdf = (Test-Path $SamplesDir) -and (@(Get-ChildItem -Path $SamplesDir -Filter *.pdf -ErrorAction SilentlyContinue).Count -gt 0)
    if (-not $hasPdf) {
        LogWarn "Samples directory missing or empty — generating PDF defaults at: $SamplesDir"
        Ensure-SamplesPdf -Dir $SamplesDir
        LogOk "Created sample .pdf files."
    }
} else {
    $hasTxt = (Test-Path $SamplesDir) -and (@(Get-ChildItem -Path $SamplesDir -Filter *.txt -ErrorAction SilentlyContinue).Count -gt 0)
    if (-not $hasTxt) {
        LogWarn "Samples directory missing or empty — generating defaults at: $SamplesDir"
        Ensure-SamplesTxt -Dir $SamplesDir
        LogOk "Created 3 sample .txt files."
    }
}

# ── Select azd env ─────────────────────────────────────────────────────────
if ($Env) {
    azd env select $Env | Out-Null
    LogOk "Using azd env: $Env"
} else {
    $current = (azd env list --output json 2>$null | ConvertFrom-Json) | Where-Object IsDefault
    if (-not $current) { LogErr "No azd environment selected. Pass -Env <name> or run azd env select."; exit 1 }
    $Env = $current.Name
    LogOk "Using azd env: $Env"
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
    if (-not $pair[1]) { LogErr "Missing azd env value: $($pair[0]). Run 'azd up' first or pass flags."; exit 1 }
}
$script:ADMIN_TOKEN = $AdminToken

$COSMOS_ENDPOINT = Get-AzdValue "AZURE_COSMOS_ENDPOINT"
if (-not $COSMOS_ENDPOINT) {
    $COSMOS_ENDPOINT = az cosmosdb list --resource-group $RESOURCE_GROUP `
        --query "[?contains(name,'omnivec-cosmos')].documentEndpoint | [0]" -o tsv 2>$null
    $COSMOS_ENDPOINT = "$COSMOS_ENDPOINT".Trim() -replace "`r|`n",""
}
if (-not $COSMOS_ENDPOINT) { LogErr "Could not locate OmniVec Cosmos account in RG $RESOURCE_GROUP"; exit 1 }

# kubectl
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    $kubectlLocal = Join-Path $HOME ".azure-kubectl/kubectl"
    if (Test-Path $kubectlLocal) {
        $env:PATH = (Split-Path $kubectlLocal) + [IO.Path]::PathSeparator + $env:PATH
    }
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    LogWarn "kubectl not found — installing via 'az aks install-cli'..."
    $kubectlDir = Join-Path $HOME ".azure-kubectl"
    New-Item -ItemType Directory -Force -Path $kubectlDir | Out-Null
    az aks install-cli --install-location (Join-Path $kubectlDir "kubectl") --only-show-errors 2>&1 | Out-Null
    $env:PATH = "$kubectlDir" + [IO.Path]::PathSeparator + $env:PATH
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        LogErr "Failed to install kubectl. Install manually and re-run."; exit 1
    }
    LogOk "kubectl installed at $kubectlDir"
}

$AKS_NAME = (az aks list --resource-group $RESOURCE_GROUP --query "[0].name" -o tsv 2>$null)
$AKS_NAME = "$AKS_NAME".Trim() -replace "`r|`n",""
if (-not $AKS_NAME) { LogErr "No AKS cluster found in RG $RESOURCE_GROUP"; exit 1 }
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_NAME --overwrite-existing --only-show-errors 2>&1 | Out-Null

$EXT_IP = (kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null)
if (-not $EXT_IP) { $EXT_IP = (kubectl get svc omnivec-api -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null) }
if (-not $EXT_IP) { LogErr "No external IP found on omnivec-web or omnivec-api — is the cluster up?"; exit 1 }
$script:SERVER_URL = "http://$EXT_IP"
$SEARCH_IP = (kubectl get svc omnivec-search -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null)
$SEARCH_TOKEN = Get-AzdValue "OMNIVEC_SEARCH_TOKEN"

LogOk "RG              : $RESOURCE_GROUP"
LogOk "Storage account : $STORAGE_ACCT"
LogOk "Cosmos endpoint : $COSMOS_ENDPOINT"
LogOk "API             : $script:SERVER_URL"
if ($SEARCH_IP) { LogOk "Search          : http://$SEARCH_IP" } else { LogWarn "omnivec-search external IP not yet available" }

# ── Step 1b: dedicated demo RG + SA (per env, AAD-only) ────────────────────
$ENV_EFFECTIVE = $Env
$DEMO_RG = "rg-omnivec-demo-$ENV_EFFECTIVE"
# Deterministic SA name: omnivecdemo + 10-char md5 hash (lowercase alnum, <=24)
$md5 = [System.Security.Cryptography.MD5]::Create()
$hashBytes = $md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($ENV_EFFECTIVE))
$ENV_HASH = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })
$ENV_HASH = $ENV_HASH.Substring(0, 10)
$DEMO_SA = "omnivecdemo$ENV_HASH"
$DEMO_LOC = $env:DEMO_SA_LOCATION
if (-not $DEMO_LOC) {
    $DEMO_LOC = (az group show -n $RESOURCE_GROUP --query location -o tsv 2>$null)
    $DEMO_LOC = "$DEMO_LOC".Trim() -replace "`r|`n",""
}
if (-not $DEMO_LOC) { $DEMO_LOC = "eastus2" }

LogStep "1b" "Preparing dedicated demo RG+SA ($DEMO_RG / $DEMO_SA)"

# 1. Demo RG (idempotent)
az group show -n $DEMO_RG 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log "Creating resource group $DEMO_RG in $DEMO_LOC..."
    az group create -n $DEMO_RG -l $DEMO_LOC --only-show-errors 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { LogErr "Failed to create resource group $DEMO_RG"; exit 1 }
    LogOk "Created RG: $DEMO_RG"
} else {
    LogOk "Using existing RG: $DEMO_RG"
}

# 2. Demo SA (idempotent, AAD-only → passes subscription policy)
az storage account show --name $DEMO_SA --resource-group $DEMO_RG 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log "Creating demo storage account $DEMO_SA in $DEMO_RG ($DEMO_LOC)..."
    $demoErr = az storage account create `
        --name $DEMO_SA --resource-group $DEMO_RG --location $DEMO_LOC `
        --sku Standard_LRS --kind StorageV2 `
        --allow-shared-key-access false --allow-blob-public-access false `
        --min-tls-version TLS1_2 --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        LogErr "Failed to create $DEMO_SA"
        $demoErr | ForEach-Object { [Console]::Error.WriteLine("    $_") }
        exit 1
    }
    LogOk "Created SA: $DEMO_SA"
} else {
    LogOk "Using existing demo SA: $DEMO_SA"
}

# 3. Grant workload MI "Storage Blob Data Contributor" on the demo SA
if (-not $IDENTITY_CID) { LogErr "No workload identity client id resolved from azd env"; exit 1 }
$DEMO_SA_ID = (az storage account show --name $DEMO_SA --resource-group $DEMO_RG --query id -o tsv 2>$null)
$DEMO_SA_ID = "$DEMO_SA_ID".Trim() -replace "`r|`n",""
if (-not $DEMO_SA_ID) { LogErr "Could not resolve demo SA id"; exit 1 }

$HAS = (az role assignment list --assignee $IDENTITY_CID --scope $DEMO_SA_ID `
    --query "[?roleDefinitionName=='Storage Blob Data Contributor'] | [0].id" -o tsv 2>$null)
$HAS = "$HAS".Trim() -replace "`r|`n",""
if (-not $HAS) {
    Log "Granting 'Storage Blob Data Contributor' to workload MI ($IDENTITY_CID)..."
    $grantErr = az role assignment create --assignee $IDENTITY_CID `
        --role "Storage Blob Data Contributor" --scope $DEMO_SA_ID --only-show-errors 2>&1
    $grantOk = ($LASTEXITCODE -eq 0)
    if (-not $grantOk) {
        Start-Sleep -Seconds 5
        $HAS = (az role assignment list --assignee $IDENTITY_CID --scope $DEMO_SA_ID `
            --query "[?roleDefinitionName=='Storage Blob Data Contributor'] | [0].id" -o tsv 2>$null)
        $HAS = "$HAS".Trim() -replace "`r|`n",""
        if ($HAS) { $grantOk = $true }
    }
    if ($grantOk) {
        LogOk "Role granted — waiting 30s for propagation"
        Start-Sleep -Seconds 30
    } else {
        LogErr "Could not grant role assignment:"
        $grantErr | ForEach-Object { [Console]::Error.WriteLine("    $_") }
        [Console]::Error.WriteLine("  Ask a subscription Owner to run:")
        [Console]::Error.WriteLine("    az role assignment create --assignee $IDENTITY_CID ``")
        [Console]::Error.WriteLine("      --role 'Storage Blob Data Contributor' --scope $DEMO_SA_ID")
        exit 1
    }
} else {
    LogOk "Workload MI already has Storage Blob Data Contributor on $DEMO_SA"
}

# 4. Retarget uploads + OmniVec source to the demo SA
$STORAGE_ACCT = $DEMO_SA
$BLOB_ENDPOINT = "https://${DEMO_SA}.blob.core.windows.net"
LogOk "Upload target: $BLOB_ENDPOINT"

# ── Validate API + token ───────────────────────────────────────────────────
LogStep 2 "Validating API + admin token"
try { Invoke-RestMethod -Uri "$script:SERVER_URL/health" -TimeoutSec 10 | Out-Null }
catch { LogErr "API /health unreachable at $script:SERVER_URL"; exit 1 }
$ok = $false
try { Invoke-ApiCall GET "/api/auth/whoami" | Out-Null; $ok = $true } catch {}
if (-not $ok) {
    try { Invoke-ApiCall GET "/api/sources" | Out-Null; $ok = $true } catch {}
}
if (-not $ok) { LogErr "Admin token rejected by API"; exit 1 }
LogOk "Admin token accepted"

# ── AOAI creds ─────────────────────────────────────────────────────────────
if (-not $AoaiEndpoint) { $AoaiEndpoint = Read-Host "  Azure OpenAI endpoint (https://<res>.openai.azure.com)" }
if (-not $AoaiKey) {
    $sec = Read-Host "  Azure OpenAI API key" -AsSecureString
    $AoaiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if (-not $AoaiEndpoint -or -not $AoaiKey) { LogErr "AOAI endpoint + key required"; exit 1 }

# ── Register embedding model (idempotent) ──────────────────────────────────
LogStep 3 "Registering Azure OpenAI embedding model"
$MODEL_NAME = "e2e-blob-embed"
$existing = Invoke-ApiTry GET "/api/models"
$MODEL_ID = $null
if ($existing -and $existing.models) {
    $MODEL_ID = ($existing.models | Where-Object { $_.name -eq $MODEL_NAME } | Select-Object -First 1).id
}
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
    $r = Invoke-ApiCall POST "/api/models" $modelBody
    $MODEL_ID = $r.id
    LogOk "Registered model: $MODEL_ID ($AoaiDeployment, ${AoaiDims}d)"
} else {
    LogOk "Re-using existing model: $MODEL_ID"
}

# ── Upload samples via in-cluster K8s Job ──────────────────────────────────
LogStep 4 "Uploading samples via in-cluster Job ($FileType)"

$samples = @(Get-ChildItem -Path $SamplesDir -Filter "*.$FileType" -ErrorAction SilentlyContinue)
if (-not $samples -or $samples.Count -eq 0) { LogErr "No .$FileType samples in $SamplesDir"; exit 1 }
$SAMPLE_COUNT = $samples.Count

$API_POD_UP = (kubectl get pods -n omnivec -l app=omnivec-api -o jsonpath='{.items[0].metadata.name}' 2>$null)
if (-not $API_POD_UP) { LogErr "No omnivec-api pod running — cluster not ready"; exit 1 }
$API_IMAGE = (kubectl get pod -n omnivec $API_POD_UP -o jsonpath='{.spec.containers[0].image}' 2>$null)
$API_SA    = (kubectl get pod -n omnivec $API_POD_UP -o jsonpath='{.spec.serviceAccountName}' 2>$null)
if (-not $API_IMAGE -or -not $API_SA) { LogErr "Could not resolve api image/serviceAccount"; exit 1 }

$SAFE_NAME = ($Container.ToLower() -replace '[^a-z0-9-]','-')
if ($SAFE_NAME.Length -gt 40) { $SAFE_NAME = $SAFE_NAME.Substring(0,40) }
$SAFE_NAME = $SAFE_NAME.TrimEnd('-')
$JOB_NAME = "omnivec-e2e-upload-$SAFE_NAME"
$CM_NAME  = "omnivec-e2e-samples-$SAFE_NAME"

# Idempotent cleanup
kubectl delete job $JOB_NAME -n omnivec --ignore-not-found --wait=true 2>&1 | Out-Null
kubectl delete configmap $CM_NAME -n omnivec --ignore-not-found 2>&1 | Out-Null
kubectl delete configmap "$CM_NAME-script" -n omnivec --ignore-not-found 2>&1 | Out-Null

# Stage samples into a ConfigMap (binary-safe for PDFs)
Log "Staging $SAMPLE_COUNT $FileType file(s) into ConfigMap $CM_NAME..."
$cmArgs = @("create","configmap",$CM_NAME,"-n","omnivec")
foreach ($f in $samples) { $cmArgs += "--from-file=$($f.Name)=$($f.FullName)" }
& kubectl @cmArgs 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { LogErr "Failed to create ConfigMap $CM_NAME"; exit 1 }

# Python uploader (same as .sh)
$PY_UPLOAD = @'
import os, sys, pathlib
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

sa   = os.environ["SA_NAME"]
cnt  = os.environ["CONTAINER_NAME"]
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
svc  = BlobServiceClient(f"https://{sa}.blob.core.windows.net", credential=cred)
cc   = svc.get_container_client(cnt)
try:
    cc.create_container()
    print(f"container created: {cnt}")
except Exception as e:
    print(f"container exists or create skipped: {type(e).__name__}")

uploaded = 0
for p in sorted(pathlib.Path("/samples").iterdir()):
    if not p.is_file() or p.name.startswith(".."):
        continue
    data = p.read_bytes()
    cc.upload_blob(name=p.name, data=data, overwrite=True)
    print(f"uploaded {p.name} ({len(data)} bytes)")
    uploaded += 1

if uploaded == 0:
    print("ERROR: no samples found in /samples", file=sys.stderr)
    sys.exit(2)
print(f"OK: uploaded {uploaded} blob(s) to {sa}/{cnt}")
'@

$tmpPy = New-TemporaryFile
Set-Content -Path $tmpPy -Value $PY_UPLOAD -Encoding UTF8 -NoNewline
try {
    kubectl create configmap "$CM_NAME-script" -n omnivec "--from-file=upload.py=$tmpPy" 2>&1 | Out-Null
} finally {
    Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
}

$JOB_YAML = @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB_NAME
  namespace: omnivec
spec:
  backoffLimit: 2
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        app: omnivec-e2e-upload
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: $API_SA
      restartPolicy: Never
      containers:
      - name: uploader
        image: $API_IMAGE
        imagePullPolicy: IfNotPresent
        env:
        - name: SA_NAME
          value: "$STORAGE_ACCT"
        - name: CONTAINER_NAME
          value: "$Container"
        command: ["python", "/scripts/upload.py"]
        volumeMounts:
        - name: samples
          mountPath: /samples
        - name: script
          mountPath: /scripts
      volumes:
      - name: samples
        configMap:
          name: $CM_NAME
      - name: script
        configMap:
          name: $CM_NAME-script
"@

$tmpYaml = New-TemporaryFile
Set-Content -Path $tmpYaml -Value $JOB_YAML -Encoding UTF8
try {
    kubectl apply -f $tmpYaml 2>&1 | Out-Null
} finally {
    Remove-Item $tmpYaml -Force -ErrorAction SilentlyContinue
}
LogOk "Job $JOB_NAME submitted"

Log "Waiting for upload job to complete..."
kubectl wait --for=condition=complete --timeout=300s "job/$JOB_NAME" -n omnivec 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    LogErr "Upload job did not complete in 5 minutes"
    kubectl logs -n omnivec "job/$JOB_NAME" --tail=100 2>&1 | ForEach-Object { [Console]::Error.WriteLine("    $_") }
    exit 1
}
LogOk "Upload job completed"

$jobStatus = (kubectl get "job/$JOB_NAME" -n omnivec -o jsonpath='{.status.succeeded}' 2>$null)
if ("$jobStatus" -ne "1") {
    LogErr "Upload job failed. Logs:"
    kubectl logs -n omnivec "job/$JOB_NAME" --tail=100 2>&1 | ForEach-Object { [Console]::Error.WriteLine("    $_") }
    exit 1
}

kubectl logs -n omnivec "job/$JOB_NAME" 2>&1 | ForEach-Object { Write-Host "  $_" }

kubectl delete configmap $CM_NAME "$CM_NAME-script" -n omnivec --ignore-not-found 2>&1 | Out-Null

LogOk "Container $Container populated with $SAMPLE_COUNT $FileType file(s)"

# ── Cosmos database + vectors container ────────────────────────────────────
LogStep 5 "Ensuring Cosmos database + vectors container"
$COSMOS_ACCT = ($COSMOS_ENDPOINT -replace "https://","" -split "\.")[0]
$DB_NAME = "e2eblob"
$VEC_CONTAINER = "vectors"

az cosmosdb sql database create --account-name $COSMOS_ACCT --resource-group $RESOURCE_GROUP `
    --name $DB_NAME --only-show-errors 2>&1 | Out-Null

$API_POD = (kubectl get pods -n omnivec -l app=omnivec-api -o jsonpath='{.items[0].metadata.name}' 2>$null)
if (-not $API_POD) { LogErr "No omnivec-api pod running"; exit 1 }

$PY_SCRIPT = @"
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
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PY_SCRIPT))
$out = kubectl exec -n omnivec $API_POD -- sh -c "echo $encoded | base64 -d | python3 -" 2>&1 | Out-String
if ($out -match "OK:") { LogOk ($out -replace "`r|`n"," ").Trim() }
else { LogErr "Vectors container setup failed: $out"; exit 1 }

# ── Source + destination + pipeline ────────────────────────────────────────
LogStep 6 "Creating source, destination, and pipeline"
$SOURCE_NAME = "e2e-blob-source"
$DEST_NAME   = "e2e-blob-dest"
$PIPE_NAME   = "e2e-blob-pipeline"

foreach ($kind in @("pipelines","sources","destinations")) {
    $list = Invoke-ApiTry GET "/api/$kind"
    if ($list -and $list.$kind) {
        foreach ($it in $list.$kind) {
            if ($it.name -in @($SOURCE_NAME,$DEST_NAME,$PIPE_NAME)) {
                try { Invoke-ApiTry DELETE "/api/$kind/$($it.id)" | Out-Null } catch {}
            }
        }
    }
}

$srcBody = @{
    name = $SOURCE_NAME; type = "azure-blob"
    config = @{
        account_url = $BLOB_ENDPOINT
        container   = $Container
        file_type   = $FileType
        auth_type   = "managed-identity"
    }
}
$src = Invoke-ApiCall POST "/api/sources" $srcBody
$SOURCE_ID = if ($src.source) { $src.source.id } else { $src.id }
if (-not $SOURCE_ID) { LogErr "Source creation returned no id. Response: $($src | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Source: $SOURCE_ID"

$dstBody = @{
    name = $DEST_NAME; type = "cosmosdb-vector"
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
$dst = Invoke-ApiCall POST "/api/destinations" $dstBody
$DEST_ID = if ($dst.destination) { $dst.destination.id } else { $dst.id }
if (-not $DEST_ID) { LogErr "Destination creation returned no id. Response: $($dst | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Destination: $DEST_ID"

# DocGrok pipelines registration (text + pdf, idempotent)
$WORKER_URL = "http://pipeline-worker-svc.omnivec.svc.cluster.local:8080"

function Register-DocGrokPipeline {
    param($display, $modelId)
    $body = '{"name":"' + $display + '","worker_url":"' + $WORKER_URL + '","model_id":"' + $modelId + '","type":"embedding"}'
    $resp = $body | kubectl exec -i -n omnivec $API_POD -- curl -sS -X POST `
        "http://docgrok.omnivec.svc.cluster.local/admin/pipelines" `
        -H "content-type: application/json" --data-binary "@-" 2>&1
    try {
        $obj = $resp | ConvertFrom-Json
        if ($obj.id) {
            LogOk "DocGrok pipeline registered: $display -> id=$($obj.id) (model=$modelId)"
            return $obj.id
        }
    } catch {}
    LogWarn "DocGrok pipeline $display registration failed: $resp"
    return $null
}

$DG_TEXT_ID = Register-DocGrokPipeline "DocGrok Text" $MODEL_ID
$DG_PDF_ID  = Register-DocGrokPipeline "DocGrok PDF"  $MODEL_ID
$DG_PIPELINE_ID = if ($FileType -eq "pdf") { $DG_PDF_ID } else { $DG_TEXT_ID }
if (-not $DG_PIPELINE_ID) { LogErr "DocGrok $FileType pipeline registration failed — cannot create OmniVec pipeline"; exit 1 }

$PIP_MODE = if ($SkipQueue) { "inline" } else { "queue" }
if ($SkipQueue) { Log "Pipeline mode: inline (-SkipQueue)" }

$pipBody = @{
    name = $PIPE_NAME
    sources = @(@{
        source_id      = $SOURCE_ID
        filters        = @{}
        content_fields = @("content")
        file_types     = @($FileType)
    })
    destination_id    = $DEST_ID
    docgrok_pipeline  = $DG_PIPELINE_ID
    vector_index_path = "embedding"
    process_existing  = $true
    processing_mode   = $PIP_MODE
}
$pipe = Invoke-ApiCall POST "/api/pipelines" $pipBody
$PIPE_ID = if ($pipe.pipeline) { $pipe.pipeline.id } else { $pipe.id }
if (-not $PIPE_ID) { LogErr "Pipeline creation returned no id. Response: $($pipe | ConvertTo-Json -Depth 4)"; exit 1 }
LogOk "Pipeline: $PIPE_ID ($PIP_MODE mode)"

# ── Activate + poll ────────────────────────────────────────────────────────
LogStep 7 "Activating pipeline and waiting for embeddings"
Invoke-ApiCall POST "/api/sources/$SOURCE_ID/sync" @{} | Out-Null
LogOk "Pipeline activated — controller will enumerate blobs"

$expected = $SAMPLE_COUNT
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
    $out = kubectl exec -n omnivec $API_POD -- sh -c "echo $encoded | base64 -d | python3 -" 2>&1 | Out-String
    if ($out -match 'COUNT=(\d+)') {
        $n = [int]$Matches[1]
        if ($n -ne $lastCount) { Log "  vectors embedded: $n / $expected"; $lastCount = $n }
        if ($n -ge $expected) { LogOk "All $expected files embedded"; break }
    }
    Start-Sleep -Seconds 10
}
if ($lastCount -lt $expected) {
    LogWarn "Only $lastCount / $expected vectors after 5 minutes. Check: kubectl logs -n omnivec deploy/omnivec-controller"
}

# ── Search validation ──────────────────────────────────────────────────────
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
            -Body (ConvertTo-Json -InputObject $searchBody -Depth 20 -Compress) -TimeoutSec 30
        LogOk "Got $($resp.results.Count) result(s):"
        $resp.results | Select-Object -First 3 | ForEach-Object {
            $txt = if ($_.text) { $_.text.Substring(0, [Math]::Min(80, $_.text.Length)) } else { "" }
            Log "    [$($_.rank)] score=$([Math]::Round([double]$_.score, 4))  $txt..."
        }
    } catch { LogWarn "Search query failed: $($_.Exception.Message)" }
} elseif ($NoSearch) {
    LogWarn "Skipping search (-NoSearch passed)"
} else {
    LogWarn "Skipping search (no IP or token)"
}

# ── Cleanup ────────────────────────────────────────────────────────────────
if ($Cleanup) {
    LogStep 9 "Cleanup"
    foreach ($kind in @("pipelines","sources","destinations")) {
        try {
            $list = Invoke-ApiTry GET "/api/$kind"
            if ($list -and $list.$kind) {
                foreach ($it in $list.$kind) {
                    if ($it.name -in @($SOURCE_NAME,$DEST_NAME,$PIPE_NAME)) {
                        Invoke-ApiTry DELETE "/api/$kind/$($it.id)" | Out-Null
                    }
                }
            }
        } catch {}
    }
    az storage container delete --account-name $STORAGE_ACCT --name $Container `
        --auth-mode login --only-show-errors 2>&1 | Out-Null
    LogOk "Demo objects deleted"
}

Write-Host "`n`e[32m╔══════════════════════════╗`e[0m"
Write-Host "`e[32m║  E2E demo completed      ║`e[0m"
Write-Host "`e[32m╚══════════════════════════╝`e[0m`n"
Write-Host "  Source container : $Container ($SAMPLE_COUNT files)"
Write-Host "  Destination      : $DB_NAME/$VEC_CONTAINER @ $COSMOS_ACCT"
Write-Host "  Pipeline         : $PIPE_ID"
if ($SEARCH_IP) { Write-Host "  Search service   : http://$SEARCH_IP" }
