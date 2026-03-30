# OmniVec End-to-End Demo — Fully Automated
# Creates environment, provisions infra, registers model, creates pipeline, verifies it works
# Tests both queue mode (CFP → jobs → worker → destination) and inline mode (CFP embeds directly into source)
#
# Usage:
#   pwsh scripts/e2e-demo.ps1               # Run all steps (1-11)
#   pwsh scripts/e2e-demo.ps1 -FromStep 5   # Skip infra, start from test account creation
#   pwsh scripts/e2e-demo.ps1 -FromStep 8   # Skip to pipeline + docs (assumes resources exist)
#   pwsh scripts/e2e-demo.ps1 -Quiet         # Minimal output (pass/fail per step)

param(
    [int]$FromStep = 1,
    [switch]$Quiet,
    [string]$AoaiEndpoint = $env:AOAI_ENDPOINT,
    [string]$AoaiKey = $env:AOAI_KEY,
    [string]$AoaiDeployment = $(if ($env:AOAI_DEPLOYMENT) { $env:AOAI_DEPLOYMENT } else { "text-embedding-3-small" }),
    [int]$AoaiDims = $(if ($env:AOAI_DIMS) { [int]$env:AOAI_DIMS } else { 1536 }),
    [string]$SharedRegistryToken = $env:OMNIVEC_SHARED_REGISTRY_TOKEN
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot/..").Path
$CLI = "$RootDir/bin/omnivec.exe"
$TOTAL_STEPS = 11

# ─── Logging helpers ────────────────────────────────────────────────────────
function Log       { param([string]$Msg) if (-not $Quiet) { Write-Host $Msg } }
function LogStep   { param([int]$N, [string]$Msg) Write-Host "`e[33m[Step $N/$TOTAL_STEPS] $Msg`e[0m" }
function LogOk     { param([string]$Msg) Write-Host "  `e[32m$Msg`e[0m" }
function LogWarn   { param([string]$Msg) Write-Host "  `e[33m$Msg`e[0m" }
function LogErr    { param([string]$Msg) Write-Host "  `e[31m$Msg`e[0m" }

# Auto-download CLI if not present, build from source as fallback
if (-not (Test-Path $CLI)) {
    New-Item -ItemType Directory -Path "$RootDir/bin" -Force | Out-Null
    $downloaded = $false

    # Try download first (fast)
    try {
        LogWarn "CLI not found — downloading from GitHub release..."
        # Get GitHub token from gh CLI, env var, or git credential
        $ghToken = $null
        try { $ghToken = (gh auth token 2>$null) } catch {}
        if (-not $ghToken) { $ghToken = $env:GITHUB_TOKEN }
        if (-not $ghToken) { $ghToken = $env:GH_TOKEN }

        $ghHeaders = @{ "Accept" = "application/vnd.github.v3+json" }
        if ($ghToken) { $ghHeaders["Authorization"] = "token $ghToken" }

        $releaseUrl = "https://api.github.com/repos/AzureCosmosDB/OmniVec/releases/latest"
        $release = Invoke-RestMethod -Uri $releaseUrl -Headers $ghHeaders -ErrorAction Stop
        $asset = $release.assets | Where-Object { $_.name -eq "omnivec.exe" } | Select-Object -First 1
        if ($asset) {
            $dlHeaders = @{ "Accept" = "application/octet-stream" }
            if ($ghToken) { $dlHeaders["Authorization"] = "token $ghToken" }
            Invoke-WebRequest -Uri $asset.url -OutFile $CLI -Headers $dlHeaders -ErrorAction Stop
            if ((Test-Path $CLI) -and (Get-Item $CLI).Length -gt 1MB) {
                $downloaded = $true
                LogOk "Downloaded: $CLI ($($release.tag_name))"
            }
        }
    } catch {
        LogWarn "Download failed ($($_.Exception.Message)), falling back to build..."
    }

    # Fallback: build from source
    if (-not $downloaded) {
        LogWarn "Building CLI from source..."
        $goExe = Get-Command go -ErrorAction SilentlyContinue
        if (-not $goExe) {
            $goExe = @("$env:ProgramFiles\Go\bin\go.exe", "$env:USERPROFILE\go\bin\go.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        } else {
            $goExe = $goExe.Source
        }
        if ($goExe) {
            Push-Location "$RootDir/cli"
            & $goExe build -o $CLI .
            Pop-Location
            LogOk "Built: $CLI"
        } else {
            LogErr "Cannot obtain CLI. Install Go (https://go.dev/dl/) or place omnivec.exe in bin/"
            exit 1
        }
    }
}

if (-not $Quiet) {
    Write-Host "`n`e[32m╔══════════════════════════════════════════════════════╗`e[0m"
    Write-Host "`e[32m║  OmniVec End-to-End Demo — Zero Manual Intervention  ║`e[0m"
    Write-Host "`e[32m╚══════════════════════════════════════════════════════╝`e[0m`n"
}

# ─── Configuration ───────────────────────────────────────────────────────────
$ENV_NAME        = "omnivec-e2e-demo"
$LOCATION        = "eastus2"
$SUBSCRIPTION    = "074d02eb-4d74-486a-b299-b262264d1536"
$AOAI_ENDPOINT   = $AoaiEndpoint
$AOAI_KEY        = $AoaiKey
$AOAI_DEPLOYMENT = $AoaiDeployment
$AOAI_DIMS       = $AoaiDims

if (-not $AOAI_ENDPOINT) {
    LogWarn "Azure OpenAI endpoint not set."
    Log "  Example: https://<resource>.openai.azure.com"
    $AOAI_ENDPOINT = Read-Host "  Enter Azure OpenAI endpoint"
    if (-not $AOAI_ENDPOINT) { LogErr "Endpoint required."; exit 1 }
}
if (-not $AOAI_KEY) {
    LogWarn "Azure OpenAI API key not set."
    $AOAI_KEY = Read-Host "  Enter Azure OpenAI API key"
    if (-not $AOAI_KEY) { LogErr "API key required."; exit 1 }
}
LogOk "Embedding: $AOAI_DEPLOYMENT (${AOAI_DIMS}d) @ $AOAI_ENDPOINT"

# ─── Helper: load azd env values ─────────────────────────────────────────────
function Load-AzdValues {
    $script:ADMIN_TOKEN      = azd env get-value OMNIVEC_ADMIN_TOKEN 2>$null
    $script:AKS_CLUSTER      = azd env get-value AZURE_AKS_CLUSTER_NAME 2>$null
    $script:RESOURCE_GROUP   = azd env get-value AZURE_RESOURCE_GROUP 2>$null
    $script:IDENTITY_CLIENT_ID = azd env get-value AZURE_IDENTITY_CLIENT_ID 2>$null
    $script:COSMOS_ENDPOINT  = azd env get-value AZURE_COSMOS_ENDPOINT 2>$null
    $script:INSTANCE_TOKEN   = $script:AKS_CLUSTER -replace 'omnivec-aks-',''
    $script:TEST_COSMOS_ACCOUNT = "omnivec-test-$($script:INSTANCE_TOKEN)"
}

# ─── Helper: run Python on API pod via stdin ─────────────────────────────────
function Invoke-PodPython {
    param([string]$Script)
    $Script | kubectl exec -i deployment/omnivec-api -n omnivec -- python3 -
}

# =============================================================================
# STEP 1: Create azd environment
# =============================================================================
if ($FromStep -le 1) {
    LogStep 1 "Creating azd environment: $ENV_NAME"
    azd env new $ENV_NAME --location $LOCATION --subscription $SUBSCRIPTION 2>$null
    azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
    azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_D4ds_v5"
    azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
    azd env set OMNIVEC_GPU_NODE_VM_SIZE "Standard_NC6s_v3"
    azd env set OMNIVEC_GPU_NODE_COUNT 0
    azd env set OMNIVEC_BUILD_MODE "acr"
    if ($SharedRegistryToken) {
        azd env set OMNIVEC_SHARED_REGISTRY_TOKEN $SharedRegistryToken
    } elseif (-not $SharedRegistryToken) {
        LogWarn "Shared registry token not set (needed for image import)."
        $SharedRegistryToken = Read-Host "  Enter shared registry token (omnivecregistry.azurecr.io)"
        if ($SharedRegistryToken) {
            azd env set OMNIVEC_SHARED_REGISTRY_TOKEN $SharedRegistryToken
        }
    }
    LogOk "Environment configured."
}

# =============================================================================
# STEP 2: Provision infrastructure
# =============================================================================
if ($FromStep -le 2) {
    LogStep 2 "Provisioning infrastructure (azd up ~15 min)..."
    azd up --no-prompt
    if ($LASTEXITCODE -ne 0) {
        LogWarn "azd up returned non-zero, continuing..."
    }
}

# =============================================================================
# STEP 3: Get connection details + wait for API
# =============================================================================
if ($FromStep -le 3) {
    LogStep 3 "Retrieving connection details..."
}
# Always load azd values (needed by all subsequent steps)
Load-AzdValues
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --overwrite-existing 2>$null

Log "  Admin Token: $ADMIN_TOKEN"
Log "  AKS:         $AKS_CLUSTER"

# Wait for external IP
Log "  Waiting for external IP..."
$SERVER = $null
for ($i = 0; $i -lt 60; $i++) {
    $SERVER = kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($SERVER) { break }
    Start-Sleep -Seconds 5
}
if (-not $SERVER) { LogErr "Failed to get external IP"; exit 1 }
$SERVER_URL = "http://$SERVER"
LogOk "Server: $SERVER_URL"

# Wait for API
Log "  Waiting for API health..."
for ($i = 0; $i -lt 30; $i++) {
    try { $h = Invoke-RestMethod -Uri "$SERVER_URL/health" -TimeoutSec 5 2>$null; if ($h.status -eq "healthy") { break } } catch {}
    Start-Sleep -Seconds 5
}
LogOk "API healthy."

# Auth headers for all API calls
$headers = @{ "Authorization" = "Bearer $ADMIN_TOKEN"; "Content-Type" = "application/json" }

# =============================================================================
# STEP 4: Configure CLI
# =============================================================================
if ($FromStep -le 4) {
    LogStep 4 "Configuring CLI..."
    & $CLI config set server $SERVER_URL
    & $CLI config set token $ADMIN_TOKEN
    & $CLI status
}

# =============================================================================
# STEP 5: Create test CosmosDB account + containers
# =============================================================================
if ($FromStep -le 5) {
    LogStep 5 "Creating test CosmosDB account..."
    $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null

    if (-not $TEST_COSMOS_ENDPOINT) {
        Log "  Creating account: $TEST_COSMOS_ACCOUNT"
        $armPayload = @{
            location = $LOCATION; kind = "GlobalDocumentDB"
            properties = @{
                databaseAccountOfferType = "Standard"; disableLocalAuth = $true
                enableAutomaticFailover = $false
                consistencyPolicy = @{ defaultConsistencyLevel = "Session" }
                locations = @(@{ locationName = $LOCATION; failoverPriority = 0; isZoneRedundant = $false })
                capabilities = @(@{ name = "EnableServerless" }, @{ name = "EnableNoSQLVectorSearch" })
            }
        }
        $armFile = [System.IO.Path]::GetTempFileName()
        $armPayload | ConvertTo-Json -Depth 10 | Set-Content -Path $armFile -Encoding UTF8
        $armUrl = "https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.DocumentDB/databaseAccounts/${TEST_COSMOS_ACCOUNT}?api-version=2024-05-15"
        az rest --method PUT --url $armUrl --body "@$armFile" -o none
        Remove-Item $armFile -ErrorAction SilentlyContinue

        Log "  Waiting for provisioning..."
        for ($i = 0; $i -lt 40; $i++) {
            $st = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query provisioningState -o tsv 2>$null
            if ($st -eq "Succeeded") { break }
            Start-Sleep -Seconds 10
        }
        $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null
    }
    LogOk "Endpoint: $TEST_COSMOS_ENDPOINT"

    # Grant RBAC
    Log "  Granting RBAC..."
    $PRINCIPAL_ID = az identity show --name "omnivec-identity-$INSTANCE_TOKEN" --resource-group $RESOURCE_GROUP --query principalId -o tsv 2>$null
    if (-not $PRINCIPAL_ID) { $PRINCIPAL_ID = az identity list --resource-group $RESOURCE_GROUP --query "[0].principalId" -o tsv 2>$null }

    az cosmosdb sql role assignment create --account-name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --role-definition-id "00000000-0000-0000-0000-000000000002" --principal-id $PRINCIPAL_ID --scope "/" -o none 2>$null
    $scope = "/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.DocumentDB/databaseAccounts/$TEST_COSMOS_ACCOUNT"
    az role assignment create --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Cosmos DB Account Reader Role" --scope $scope -o none 2>$null
    LogOk "RBAC assigned (Data Contributor + Account Reader). Waiting 30s..."
    Start-Sleep -Seconds 30

    # Create database + containers
    Log "  Creating containers..."
    az cosmosdb sql database create --account-name $TEST_COSMOS_ACCOUNT --name testdb --resource-group $RESOURCE_GROUP -o none 2>$null
    az cosmosdb sql container create --account-name $TEST_COSMOS_ACCOUNT --database-name testdb --name test-documents --resource-group $RESOURCE_GROUP --partition-key-path "/id" -o none 2>$null
    LogOk "test-documents created."

    # Vectors container with vector policy (via API pod)
    Invoke-PodPython @"
import os
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$TEST_COSMOS_ENDPOINT", credential=cred)
db = client.get_database_client("testdb")
vp = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": $AOAI_DIMS}]}
ip = {"includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": "/embedding/*"}], "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
try:
    db.create_container(id="vectors", partition_key={"paths": ["/id"], "kind": "Hash"}, vector_embedding_policy=vp, indexing_policy=ip)
    print("  vectors created (${AOAI_DIMS}d, cosine, quantizedFlat)")
except CosmosResourceExistsError:
    print("  vectors container already exists")
"@
    LogOk "All containers ready."
} else {
    # Load test endpoint for later steps
    $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null
}

# =============================================================================
# STEP 6: Register embedding model
# =============================================================================
if ($FromStep -le 6) {
    LogStep 6 "Registering Azure OpenAI embedding model..."
    $modelBody = @{
        name = "azure-openai-embed"; type = "azure-openai"; endpoint = $AOAI_ENDPOINT
        api_key = $AOAI_KEY; model = $AOAI_DEPLOYMENT; deployment = $AOAI_DEPLOYMENT
        dimensions = $AOAI_DIMS; api_version = "2024-06-01"
    } | ConvertTo-Json
    $modelResult = Invoke-RestMethod -Uri "$SERVER_URL/api/models" -Method POST -Headers $headers -Body $modelBody
    $MODEL_ID = $modelResult.id
    LogOk "Model: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)"
} else {
    $models = Invoke-RestMethod -Uri "$SERVER_URL/api/models" -Headers $headers
    $MODEL_ID = $models.models[0].id
}

# =============================================================================
# STEP 7: Create source + destination
# =============================================================================
if ($FromStep -le 7) {
    LogStep 7 "Creating source and destination..."

    # Clean up any existing resources from previous runs
    $existing = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Headers $headers
    foreach ($p in $existing.pipelines) { try { Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$($p.id)" -Method DELETE -Headers $headers 2>$null } catch {} }
    $existing = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Headers $headers
    foreach ($s in $existing.sources) { try { Invoke-RestMethod -Uri "$SERVER_URL/api/sources/$($s.id)" -Method DELETE -Headers $headers 2>$null } catch {} }
    $existing = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Headers $headers
    foreach ($d in $existing.destinations) { try { Invoke-RestMethod -Uri "$SERVER_URL/api/destinations/$($d.id)" -Method DELETE -Headers $headers 2>$null } catch {} }

    $srcBody = @{ name = "demo-cosmosdb-source"; type = "cosmosdb"; config = @{
        endpoint = $TEST_COSMOS_ENDPOINT; database = "testdb"; container = "test-documents"
        auth_type = "managed-identity"; client_id = $IDENTITY_CLIENT_ID
    }} | ConvertTo-Json -Depth 5
    $srcResult = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Method POST -Headers $headers -Body $srcBody
    $SOURCE_ID = $srcResult.source.id
    LogOk "Source: $SOURCE_ID"

    $dstBody = @{ name = "demo-vector-store"; type = "cosmosdb-vector"; config = @{
        endpoint = $TEST_COSMOS_ENDPOINT; database = "testdb"; container = "vectors"
        auth_type = "managed-identity"; client_id = $IDENTITY_CLIENT_ID; vector_dimensions = $AOAI_DIMS
    }} | ConvertTo-Json -Depth 5
    $dstResult = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Method POST -Headers $headers -Body $dstBody
    $DEST_ID = $dstResult.destination.id
    LogOk "Destination: $DEST_ID"
} else {
    $srcs = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Headers $headers
    $SOURCE_ID = $srcs.sources[0].id
    $dsts = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Headers $headers
    $DEST_ID = $dsts.destinations[0].id
}

# =============================================================================
# STEP 8: Queue mode — create pipeline, insert docs, resume
# =============================================================================
if ($FromStep -le 8) {
    LogStep 8 "Queue mode — creating pipeline, inserting docs, activating..."

    # Pipeline (paused, queue mode is default)
    $pipBody = @{
        name = "demo-pipeline-queue"; sources = @(@{ source_id = $SOURCE_ID; filters = @{} })
        destination_id = $DEST_ID; docgrok_pipeline = $MODEL_ID; process_existing = $true
        processing_mode = "queue"
    } | ConvertTo-Json -Depth 5
    $pipResult = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Method POST -Headers $headers -Body $pipBody
    $PIP_ID = $pipResult.pipeline.id
    LogOk "Pipeline (paused, queue mode): $PIP_ID"

    # Insert test documents
    Log "  Inserting test documents..."
    Invoke-PodPython @"
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$TEST_COSMOS_ENDPOINT", credential=cred)
c = client.get_database_client("testdb").get_container_client("test-documents")
docs = [
    {"id": "doc-001", "title": "Azure Cosmos DB", "content": "Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.", "category": "database"},
    {"id": "doc-002", "title": "Azure Kubernetes Service", "content": "AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.", "category": "compute"},
    {"id": "doc-003", "title": "Azure Blob Storage", "content": "Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.", "category": "storage"},
]
for doc in docs:
    c.upsert_item(doc)
    print(f"  Inserted: {doc['id']} - {doc['title']}")
"@

    # Resume pipeline
    Log "  Resuming pipeline..."
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/resume" -Method POST -Headers $headers | Out-Null
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/run" -Method POST -Headers $headers | Out-Null
    LogOk "Pipeline activated (queue mode). Waiting 60s for processing..."
    Start-Sleep -Seconds 60
} else {
    $pips = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Headers $headers
    $PIP_ID = $pips.pipelines[0].id
}

# =============================================================================
# STEP 9: Verify queue mode results
# =============================================================================
if ($PIP_ID) {
    LogStep 9 "Verifying queue mode results..."
    if (-not $Quiet) {
        & $CLI pipeline show $PIP_ID
        Write-Host ""
        & $CLI job list
    }

    $jobsResult = Invoke-RestMethod -Uri "$SERVER_URL/api/jobs?status=completed&limit=5" -Headers $headers
    $completedCount = $jobsResult.jobs.Count

    if ($completedCount -gt 0) {
        LogOk "$completedCount documents embedded via queue mode!"

        # Vector search test
        Log "  Testing vector search..."
        $searchBody = @{ query = "what is cosmos db"; destination_id = $DEST_ID; top_k = 3 } | ConvertTo-Json
        try {
            $searchResult = Invoke-RestMethod -Uri "$SERVER_URL/api/playground/search" -Method POST -Headers $headers -Body $searchBody
            LogOk "Search returned $($searchResult.results.Count) results:"
            foreach ($r in $searchResult.results) {
                Log "    [$([math]::Round($r.score, 4))] $($r.id)"
            }
        } catch {
            LogWarn "Search test skipped (may need more processing time)"
        }

        # Pipeline reset test
        Log "  Testing pipeline reset..."
        & $CLI pipeline reset $PIP_ID -y
    } else {
        LogWarn "No completed jobs yet. Check: omnivec pipeline show $PIP_ID"
    }

    # Stats
    $stats = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID" -Headers $headers
    Log "  Processed:  $($stats.stats.documents_processed)"
    Log "  Embedded:   $($stats.stats.embedded_count)"
    Log "  Completion: $($stats.stats.completion_pct)%"

    # Clean up queue pipeline before starting inline — they share the same source,
    # so only one should be active at a time to avoid change feed contention.
    Log "  Removing queue pipeline before inline test..."
    try { Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID" -Method DELETE -Headers $headers | Out-Null } catch {}
} else {
    LogStep 9 "Skipping queue mode verify (no queue pipeline)"
    # Clean up any existing pipelines when jumping to inline test
    $existing = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Headers $headers
    foreach ($p in $existing.pipelines) { try { Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$($p.id)" -Method DELETE -Headers $headers 2>$null } catch {} }
}

# =============================================================================
# STEP 10: Inline mode — create pipeline, resume, then insert docs
# =============================================================================
if ($FromStep -le 10) {
    LogStep 10 "Inline mode — creating pipeline, activating, inserting docs..."

    # Pipeline (paused, inline mode — CFP embeds directly into source container)
    $inlinePipBody = @{
        name = "demo-pipeline-inline"; sources = @(@{ source_id = $SOURCE_ID; filters = @{} })
        destination_id = $DEST_ID; docgrok_pipeline = $MODEL_ID; process_existing = $true
        processing_mode = "inline"
    } | ConvertTo-Json -Depth 5
    $inlinePipResult = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Method POST -Headers $headers -Body $inlinePipBody
    $INLINE_PIP_ID = $inlinePipResult.pipeline.id
    LogOk "Pipeline (paused, inline mode): $INLINE_PIP_ID"

    # Resume inline pipeline FIRST so CFP picks up changes in inline mode
    Log "  Resuming inline pipeline..."
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$INLINE_PIP_ID/resume" -Method POST -Headers $headers | Out-Null
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$INLINE_PIP_ID/run" -Method POST -Headers $headers | Out-Null
    LogOk "Pipeline active (inline mode). Waiting 30s for CFP lease rebalance..."
    Start-Sleep -Seconds 30

    # Insert test documents AFTER pipeline is active so CFP processes them in inline mode
    Log "  Inserting inline test documents..."
    Invoke-PodPython @"
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$TEST_COSMOS_ENDPOINT", credential=cred)
c = client.get_database_client("testdb").get_container_client("test-documents")
docs = [
    {"id": "doc-inline-001", "title": "Azure Functions", "content": "Azure Functions is an event-driven serverless compute platform that lets you run code without provisioning or managing infrastructure.", "category": "compute"},
    {"id": "doc-inline-002", "title": "Azure AI Search", "content": "Azure AI Search provides secure information retrieval at scale over user-owned content in traditional and generative AI search applications.", "category": "ai"},
    {"id": "doc-inline-003", "title": "Azure Service Bus", "content": "Azure Service Bus is a fully managed enterprise message broker with message queues and publish-subscribe topics for decoupled applications.", "category": "messaging"},
]
for doc in docs:
    c.upsert_item(doc)
    print(f"  Inserted: {doc['id']} - {doc['title']}")
"@
    LogOk "Docs inserted. Waiting 90s for inline processing..."
    Start-Sleep -Seconds 90
} else {
    # Load inline pipeline if skipping
    $allPips = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Headers $headers
    $INLINE_PIP_ID = ($allPips.pipelines | Where-Object { $_.processing_mode -eq "inline" } | Select-Object -First 1).id
}

# =============================================================================
# STEP 11: Verify inline mode results
# =============================================================================
LogStep 11 "Verifying inline mode results..."
if (-not $Quiet) {
    & $CLI pipeline show $INLINE_PIP_ID
}

# Inline mode embeds directly into the source container — check for embedding field
Log "  Checking source container for inline embeddings..."
$inlineCheck = $null
try {
    $inlineCheck = Invoke-PodPython @"
import os, json
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$TEST_COSMOS_ENDPOINT", credential=cred)
c = client.get_database_client("testdb").get_container_client("test-documents")
embedded = 0
checked = 0
for doc in c.query_items("SELECT c.id, IS_DEFINED(c.embedding) as has_emb FROM c WHERE STARTSWITH(c.id, 'doc-inline-')", enable_cross_partition_query=True):
    checked += 1
    if doc.get('has_emb'):
        embedded += 1
        print(f"  {doc['id']}: embedding present")
    else:
        print(f"  {doc['id']}: no embedding yet")
print(f"INLINE_RESULT:{embedded}/{checked}")
"@
    Write-Host $inlineCheck
} catch {
    LogWarn "Could not query inline embeddings: $($_.Exception.Message)"
}

# For inline mode, the source container is the source of truth — embeddings are
# patched directly into source docs. Pipeline stats may lag behind.
$inlineEmbeddedCount = 0
$inlineTotal = 0
$inlineCheckStr = if ($inlineCheck -is [array]) { $inlineCheck -join "`n" } else { "$inlineCheck" }
if ($inlineCheckStr -match "INLINE_RESULT:(\d+)/(\d+)") {
    $inlineEmbeddedCount = [int]$Matches[1]
    $inlineTotal = [int]$Matches[2]
}

$inlineStats = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$INLINE_PIP_ID" -Headers $headers
Log "  Pipeline stats — Processed: $($inlineStats.stats.documents_processed), Embedded: $($inlineStats.stats.embedded_count)"
Log "  Source container — Embedded: $inlineEmbeddedCount/$inlineTotal"

if ($inlineEmbeddedCount -gt 0) {
    LogOk "Inline mode working — $inlineEmbeddedCount/$inlineTotal documents embedded directly into source container!"
} else {
    LogWarn "No inline embeddings yet. The CFP may still be processing. Check: omnivec pipeline show $INLINE_PIP_ID"
}

# =============================================================================
# Summary
# =============================================================================
Write-Host ""
Write-Host "`e[32m╔══════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║           End-to-End Demo Complete!                  ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════╝`e[0m"
Write-Host ""
Write-Host "  Server:          `e[36m$SERVER_URL`e[0m"
Write-Host "  Admin Token:     `e[36m$ADMIN_TOKEN`e[0m"
Write-Host "  Source:          `e[36m$SOURCE_ID`e[0m"
Write-Host "  Destination:     `e[36m$DEST_ID`e[0m"
Write-Host "  Queue Pipeline:  `e[36m$PIP_ID`e[0m"
Write-Host "  Inline Pipeline: `e[36m$INLINE_PIP_ID`e[0m"
Write-Host "  Model:           `e[36m$MODEL_ID ($AOAI_DEPLOYMENT)`e[0m"
Write-Host ""
Write-Host "  `e[36mQueue mode:`e[0m  CFP detects changes -> creates jobs -> .NET worker embeds -> writes to destination"
Write-Host "  `e[36mInline mode:`e[0m CFP detects changes -> embeds directly -> patches back to source container"
Write-Host ""
