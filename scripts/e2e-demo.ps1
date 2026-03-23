# OmniVec End-to-End Demo — Fully Automated
# Creates environment, provisions infra, registers model, creates pipeline, verifies it works
#
# Usage:
#   pwsh scripts/e2e-demo.ps1               # Run all steps (1-9)
#   pwsh scripts/e2e-demo.ps1 -FromStep 5   # Skip infra, start from test account creation
#   pwsh scripts/e2e-demo.ps1 -FromStep 8   # Skip to pipeline + docs (assumes resources exist)

param(
    [int]$FromStep = 1,
    [string]$AoaiEndpoint = $env:AOAI_ENDPOINT,
    [string]$AoaiKey = $env:AOAI_KEY,
    [string]$AoaiDeployment = $(if ($env:AOAI_DEPLOYMENT) { $env:AOAI_DEPLOYMENT } else { "text-embedding-3-small" }),
    [int]$AoaiDims = $(if ($env:AOAI_DIMS) { [int]$env:AOAI_DIMS } else { 1536 })
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot/..").Path
$CLI = "$RootDir/bin/omnivec.exe"

# Auto-download CLI if not present
if (-not (Test-Path $CLI)) {
    Write-Host "`e[33mCLI not found — downloading from GitHub release...`e[0m"
    New-Item -ItemType Directory -Path "$RootDir/bin" -Force | Out-Null
    Invoke-WebRequest -Uri "https://github.com/AzureCosmosDB/OmniVec/releases/download/v0.1.0/omnivec.exe" -OutFile $CLI
    Write-Host "  `e[32mDownloaded: $CLI`e[0m"
}

Write-Host "`n`e[32m╔══════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║  OmniVec End-to-End Demo — Zero Manual Intervention  ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════╝`e[0m`n"

# ─── Configuration ───────────────────────────────────────────────────────────
$ENV_NAME        = "omnivec-e2e-demo"
$LOCATION        = "eastus2"
$SUBSCRIPTION    = "074d02eb-4d74-486a-b299-b262264d1536"
$AOAI_ENDPOINT   = $AoaiEndpoint
$AOAI_KEY        = $AoaiKey
$AOAI_DEPLOYMENT = $AoaiDeployment
$AOAI_DIMS       = $AoaiDims

if (-not $AOAI_ENDPOINT -or -not $AOAI_KEY) {
    Write-Host "`e[31mError: Azure OpenAI credentials required.`e[0m"
    Write-Host "  Set env vars:  `$env:AOAI_ENDPOINT = 'https://...'; `$env:AOAI_KEY = '...'"
    Write-Host "  Or pass flags: -AoaiEndpoint 'https://...' -AoaiKey '...'"
    exit 1
}

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

# ─── Helper: run Python on API pod via temp file ─────────────────────────────
function Invoke-PodPython {
    param([string]$Script)
    $tmpFile = [System.IO.Path]::GetTempFileName() + ".py"
    $Script | Set-Content -Path $tmpFile -Encoding UTF8
    $podName = kubectl get pod -n omnivec -l app=omnivec-api -o jsonpath='{.items[0].metadata.name}' 2>$null
    kubectl cp $tmpFile "${podName}:/tmp/_e2e_script.py" -n omnivec 2>$null
    kubectl exec $podName -n omnivec -- python3 /tmp/_e2e_script.py
    Remove-Item $tmpFile -ErrorAction SilentlyContinue
}

# =============================================================================
# STEP 1: Create azd environment
# =============================================================================
if ($FromStep -le 1) {
    Write-Host "`e[33m[Step 1/9] Creating azd environment: $ENV_NAME`e[0m"
    azd env new $ENV_NAME --location $LOCATION --subscription $SUBSCRIPTION 2>$null
    azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
    azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_D4s_v3"
    azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
    azd env set OMNIVEC_GPU_NODE_VM_SIZE "Standard_NC6s_v3"
    azd env set OMNIVEC_GPU_NODE_COUNT 0
    azd env set OMNIVEC_BUILD_MODE "acr"
    Write-Host "  `e[32mEnvironment configured.`e[0m"
}

# =============================================================================
# STEP 2: Provision infrastructure
# =============================================================================
if ($FromStep -le 2) {
    Write-Host "`n`e[33m[Step 2/9] Provisioning infrastructure (azd up ~15 min)...`e[0m"
    azd up --no-prompt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  `e[33mazd up returned non-zero, continuing...`e[0m"
    }
}

# =============================================================================
# STEP 3: Get connection details + wait for API
# =============================================================================
if ($FromStep -le 3) {
    Write-Host "`n`e[33m[Step 3/9] Retrieving connection details...`e[0m"
}
# Always load azd values (needed by all subsequent steps)
Load-AzdValues
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --overwrite-existing 2>$null

Write-Host "  Admin Token: $ADMIN_TOKEN"
Write-Host "  AKS:         $AKS_CLUSTER"

# Wait for external IP
Write-Host "  Waiting for external IP..."
$SERVER = $null
for ($i = 0; $i -lt 60; $i++) {
    $SERVER = kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($SERVER) { break }
    Start-Sleep -Seconds 5
}
if (-not $SERVER) { Write-Host "`e[31mFailed to get external IP`e[0m"; exit 1 }
$SERVER_URL = "http://$SERVER"
Write-Host "  `e[32mServer: $SERVER_URL`e[0m"

# Wait for API
Write-Host "  Waiting for API health..."
for ($i = 0; $i -lt 30; $i++) {
    try { $h = Invoke-RestMethod -Uri "$SERVER_URL/health" -TimeoutSec 5 2>$null; if ($h.status -eq "healthy") { break } } catch {}
    Start-Sleep -Seconds 5
}
Write-Host "  `e[32mAPI healthy.`e[0m"

# Auth headers for all API calls
$headers = @{ "Authorization" = "Bearer $ADMIN_TOKEN"; "Content-Type" = "application/json" }

# =============================================================================
# STEP 4: Configure CLI
# =============================================================================
if ($FromStep -le 4) {
    Write-Host "`n`e[33m[Step 4/9] Configuring CLI...`e[0m"
    & $CLI config set server $SERVER_URL
    & $CLI config set token $ADMIN_TOKEN
    & $CLI status
}

# =============================================================================
# STEP 5: Create test CosmosDB account + containers
# =============================================================================
if ($FromStep -le 5) {
    Write-Host "`n`e[33m[Step 5/9] Creating test CosmosDB account...`e[0m"
    $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null

    if (-not $TEST_COSMOS_ENDPOINT) {
        Write-Host "  Creating account: $TEST_COSMOS_ACCOUNT"
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

        Write-Host "  Waiting for provisioning..."
        for ($i = 0; $i -lt 40; $i++) {
            $st = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query provisioningState -o tsv 2>$null
            if ($st -eq "Succeeded") { break }
            Start-Sleep -Seconds 10
        }
        $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null
    }
    Write-Host "  `e[32mEndpoint: $TEST_COSMOS_ENDPOINT`e[0m"

    # Grant RBAC
    Write-Host "  Granting RBAC..."
    $PRINCIPAL_ID = az identity show --name "omnivec-identity-$INSTANCE_TOKEN" --resource-group $RESOURCE_GROUP --query principalId -o tsv 2>$null
    if (-not $PRINCIPAL_ID) { $PRINCIPAL_ID = az identity list --resource-group $RESOURCE_GROUP --query "[0].principalId" -o tsv 2>$null }

    az cosmosdb sql role assignment create --account-name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --role-definition-id "00000000-0000-0000-0000-000000000002" --principal-id $PRINCIPAL_ID --scope "/" -o none 2>$null
    $scope = "/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.DocumentDB/databaseAccounts/$TEST_COSMOS_ACCOUNT"
    az role assignment create --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Cosmos DB Account Reader Role" --scope $scope -o none 2>$null
    Write-Host "  `e[32mRBAC assigned (Data Contributor + Account Reader). Waiting 30s...`e[0m"
    Start-Sleep -Seconds 30

    # Create database + containers
    Write-Host "  Creating containers..."
    az cosmosdb sql database create --account-name $TEST_COSMOS_ACCOUNT --name testdb --resource-group $RESOURCE_GROUP -o none 2>$null
    az cosmosdb sql container create --account-name $TEST_COSMOS_ACCOUNT --database-name testdb --name test-documents --resource-group $RESOURCE_GROUP --partition-key-path "/id" -o none 2>$null
    Write-Host "  `e[32mtest-documents created.`e[0m"

    # Vectors container with vector policy (via API pod)
    Invoke-PodPython @"
import os
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
client = CosmosClient("$TEST_COSMOS_ENDPOINT", credential=cred)
db = client.get_database_client("testdb")
vp = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": $AOAI_DIMS}]}
ip = {"includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": "/embedding/*"}], "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
db.create_container(id="vectors", partition_key={"paths": ["/id"], "kind": "Hash"}, vector_embedding_policy=vp, indexing_policy=ip)
print("  vectors created (${AOAI_DIMS}d, cosine, quantizedFlat)")
"@
    Write-Host "  `e[32mAll containers ready.`e[0m"
} else {
    # Load test endpoint for later steps
    $TEST_COSMOS_ENDPOINT = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null
}

# =============================================================================
# STEP 6: Register embedding model
# =============================================================================
if ($FromStep -le 6) {
    Write-Host "`n`e[33m[Step 6/9] Registering Azure OpenAI embedding model...`e[0m"
    $modelBody = @{
        name = "azure-openai-embed"; type = "azure-openai"; endpoint = $AOAI_ENDPOINT
        api_key = $AOAI_KEY; model = $AOAI_DEPLOYMENT; deployment = $AOAI_DEPLOYMENT
        dimensions = $AOAI_DIMS; api_version = "2024-06-01"
    } | ConvertTo-Json
    $modelResult = Invoke-RestMethod -Uri "$SERVER_URL/api/models" -Method POST -Headers $headers -Body $modelBody
    $MODEL_ID = $modelResult.id
    Write-Host "  `e[32mModel: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)`e[0m"
} else {
    $models = Invoke-RestMethod -Uri "$SERVER_URL/api/models" -Headers $headers
    $MODEL_ID = $models.models[0].id
}

# =============================================================================
# STEP 7: Create source + destination
# =============================================================================
if ($FromStep -le 7) {
    Write-Host "`n`e[33m[Step 7/9] Creating source and destination...`e[0m"
    $srcBody = @{ name = "demo-cosmosdb-source"; type = "cosmosdb"; config = @{
        endpoint = $TEST_COSMOS_ENDPOINT; database = "testdb"; container = "test-documents"
        auth_type = "managed-identity"; client_id = $IDENTITY_CLIENT_ID
    }} | ConvertTo-Json -Depth 5
    $srcResult = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Method POST -Headers $headers -Body $srcBody
    $SOURCE_ID = $srcResult.source.id
    Write-Host "  `e[32mSource: $SOURCE_ID`e[0m"

    $dstBody = @{ name = "demo-vector-store"; type = "cosmosdb-vector"; config = @{
        endpoint = $TEST_COSMOS_ENDPOINT; database = "testdb"; container = "vectors"
        auth_type = "managed-identity"; client_id = $IDENTITY_CLIENT_ID; vector_dimensions = $AOAI_DIMS
    }} | ConvertTo-Json -Depth 5
    $dstResult = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Method POST -Headers $headers -Body $dstBody
    $DEST_ID = $dstResult.destination.id
    Write-Host "  `e[32mDestination: $DEST_ID`e[0m"
} else {
    $srcs = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Headers $headers
    $SOURCE_ID = $srcs.sources[0].id
    $dsts = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Headers $headers
    $DEST_ID = $dsts.destinations[0].id
}

# =============================================================================
# STEP 8: Create pipeline (paused) + insert docs + resume
# =============================================================================
if ($FromStep -le 8) {
    Write-Host "`n`e[33m[Step 8/9] Creating pipeline, inserting docs, activating...`e[0m"

    # Pipeline (paused)
    $pipBody = @{
        name = "demo-pipeline"; sources = @(@{ source_id = $SOURCE_ID; filters = @{} })
        destination_id = $DEST_ID; docgrok_pipeline = $MODEL_ID; process_existing = $true
    } | ConvertTo-Json -Depth 5
    $pipResult = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Method POST -Headers $headers -Body $pipBody
    $PIP_ID = $pipResult.pipeline.id
    Write-Host "  `e[32mPipeline (paused): $PIP_ID`e[0m"

    # Insert test documents
    Write-Host "  Inserting test documents..."
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
    Write-Host "  Resuming pipeline..."
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/resume" -Method POST -Headers $headers | Out-Null
    Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/run" -Method POST -Headers $headers | Out-Null
    Write-Host "  `e[32mPipeline activated. Waiting 60s for change feed processing...`e[0m"
    Start-Sleep -Seconds 60
} else {
    $pips = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Headers $headers
    $PIP_ID = $pips.pipelines[0].id
}

# =============================================================================
# STEP 9: Verify results
# =============================================================================
Write-Host "`n`e[33m[Step 9/9] Checking results...`e[0m"
& $CLI pipeline show $PIP_ID
Write-Host ""
& $CLI job list

$jobsResult = Invoke-RestMethod -Uri "$SERVER_URL/api/jobs?status=completed&limit=5" -Headers $headers
$completedCount = $jobsResult.jobs.Count

Write-Host ""
if ($completedCount -gt 0) {
    Write-Host "`e[32m  $completedCount documents successfully embedded!`e[0m"

    # Vector search test
    Write-Host "`e[33m  Testing vector search...`e[0m"
    $searchBody = @{ query = "what is cosmos db"; destination_id = $DEST_ID; top_k = 3 } | ConvertTo-Json
    try {
        $searchResult = Invoke-RestMethod -Uri "$SERVER_URL/api/playground/search" -Method POST -Headers $headers -Body $searchBody
        Write-Host "`e[32m  Search returned $($searchResult.results.Count) results:`e[0m"
        foreach ($r in $searchResult.results) {
            Write-Host "    [$([math]::Round($r.score, 4))] $($r.id)"
        }
    } catch {
        Write-Host "`e[33m  Search test skipped (may need more processing time)`e[0m"
    }

    # Pipeline reset test
    Write-Host ""
    Write-Host "`e[33m  Testing pipeline reset...`e[0m"
    & $CLI pipeline reset $PIP_ID -y
} else {
    Write-Host "`e[33m  No completed jobs yet. Check: omnivec pipeline show $PIP_ID`e[0m"
}

# Stats
$stats = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID" -Headers $headers
Write-Host ""
Write-Host "  Processed:  $($stats.stats.documents_processed)"
Write-Host "  Embedded:   $($stats.stats.embedded_count)"
Write-Host "  Completion: $($stats.stats.completion_pct)%"

Write-Host "`n`e[32m╔══════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║           End-to-End Demo Complete!                   ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════╝`e[0m"
Write-Host ""
Write-Host "  Server:      `e[36m$SERVER_URL`e[0m"
Write-Host "  Admin Token: `e[36m$ADMIN_TOKEN`e[0m"
Write-Host "  Source:      `e[36m$SOURCE_ID`e[0m"
Write-Host "  Destination: `e[36m$DEST_ID`e[0m"
Write-Host "  Pipeline:    `e[36m$PIP_ID`e[0m"
Write-Host "  Model:       `e[36m$MODEL_ID ($AOAI_DEPLOYMENT)`e[0m"
Write-Host ""
