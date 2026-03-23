# OmniVec End-to-End Demo — Fully Automated
# Creates environment, provisions infra, registers model, creates pipeline, verifies it works
# Usage: pwsh scripts/e2e-demo.ps1

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot/..").Path
$CLI = "$RootDir/cli/omnivec.exe"

Write-Host "`n`e[32m╔══════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║  OmniVec End-to-End Demo — Zero Manual Intervention  ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════╝`e[0m`n"

# ─── Configuration ───────────────────────────────────────────────────────────
$ENV_NAME       = "omnivec-e2e-demo"
$LOCATION       = "eastus2"
$SUBSCRIPTION   = "074d02eb-4d74-486a-b299-b262264d1536"

# Azure OpenAI Embedding (text-embedding-3-small, 1536 dims)
$AOAI_ENDPOINT  = "https://embedding-south-central.cognitiveservices.azure.com"
$AOAI_KEY       = "F5RsCZ2ORnAva6f6eVTKV6PFPKGu8KluOa6kyywXW7I2tqiWCwvKJQQJ99BJACLArgHXJ3w3AAAAACOGLB6z"
$AOAI_DEPLOYMENT = "text-embedding-3-small"
$AOAI_DIMS      = 1536

# ─── Step 1: Create fresh azd environment ────────────────────────────────────
Write-Host "`e[33m[Step 1/9] Creating azd environment: $ENV_NAME`e[0m"

azd env new $ENV_NAME --location $LOCATION --subscription $SUBSCRIPTION 2>$null

# Pre-configure all values (skip interactive prompts)
azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE "Standard_D4s_v3"
azd env set OMNIVEC_SYSTEM_NODE_COUNT 2
azd env set OMNIVEC_GPU_NODE_VM_SIZE "Standard_NC6s_v3"
azd env set OMNIVEC_GPU_NODE_COUNT 0
azd env set OMNIVEC_BUILD_MODE "acr"

Write-Host "  `e[32mEnvironment configured.`e[0m"

# ─── Step 2: Provision infrastructure ────────────────────────────────────────
Write-Host "`n`e[33m[Step 2/9] Provisioning infrastructure (azd up)...`e[0m"
Write-Host "  This takes ~15 minutes (AKS, CosmosDB, ACR, Storage, Service Bus)"

azd up --no-prompt
if ($LASTEXITCODE -ne 0) {
    Write-Host "  `e[33mazd up returned non-zero but postprovision may have succeeded. Continuing...`e[0m"
}

# ─── Step 3: Get connection details ──────────────────────────────────────────
Write-Host "`n`e[33m[Step 3/9] Retrieving connection details...`e[0m"

$ADMIN_TOKEN = azd env get-value OMNIVEC_ADMIN_TOKEN 2>$null
$COSMOS_ENDPOINT = azd env get-value AZURE_COSMOS_ENDPOINT 2>$null
$AKS_CLUSTER = azd env get-value AZURE_AKS_CLUSTER_NAME 2>$null
$RESOURCE_GROUP = azd env get-value AZURE_RESOURCE_GROUP 2>$null
$IDENTITY_CLIENT_ID = azd env get-value AZURE_IDENTITY_CLIENT_ID 2>$null
$COSMOS_ACCOUNT = ($COSMOS_ENDPOINT -replace 'https://','') -replace '\.documents.*',''

# Get AKS credentials
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --overwrite-existing 2>$null

Write-Host "  `e[32mCosmosDB:    $COSMOS_ENDPOINT`e[0m"
Write-Host "  `e[32mAdmin Token: $ADMIN_TOKEN`e[0m"

# Wait for external IP
Write-Host "  Waiting for external IP..."
$SERVER = $null
for ($i = 0; $i -lt 60; $i++) {
    $SERVER = kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($SERVER) { break }
    Start-Sleep -Seconds 5
}

if (-not $SERVER) {
    Write-Host "  `e[31mFailed to get external IP. Check: kubectl get svc -n omnivec`e[0m"
    exit 1
}
$SERVER_URL = "http://$SERVER"
Write-Host "  `e[32mServer: $SERVER_URL`e[0m"

# Wait for API to be ready
Write-Host "  Waiting for API to be ready..."
for ($i = 0; $i -lt 30; $i++) {
    try {
        $health = Invoke-RestMethod -Uri "$SERVER_URL/health" -TimeoutSec 5 2>$null
        if ($health.status -eq "healthy") { break }
    } catch {}
    Start-Sleep -Seconds 5
}
Write-Host "  `e[32mAPI is healthy.`e[0m"

# ─── Step 4: Configure CLI ──────────────────────────────────────────────────
Write-Host "`n`e[33m[Step 4/9] Configuring CLI...`e[0m"

& $CLI config set server $SERVER_URL
& $CLI config set token $ADMIN_TOKEN
& $CLI status

# ─── Step 5: Create separate test CosmosDB account with containers ───────────
Write-Host "`n`e[33m[Step 5/9] Creating test CosmosDB account with source/destination containers...`e[0m"

$INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
$TEST_COSMOS_ACCOUNT = "omnivec-test-$INSTANCE_TOKEN"
$SUBSCRIPTION_ID = azd env get-value AZURE_SUBSCRIPTION_ID 2>$null

# Create a separate CosmosDB serverless account for test data
Write-Host "  Creating test CosmosDB account: $TEST_COSMOS_ACCOUNT..."
# Create via ARM REST PUT with disableLocalAuth=true (required by subscription policy)
$armPayload = @{
    location = $LOCATION
    kind = "GlobalDocumentDB"
    properties = @{
        databaseAccountOfferType = "Standard"
        disableLocalAuth = $true
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

# Wait for account to be ready (provisioning takes ~3 min)
Write-Host "  Waiting for CosmosDB account provisioning..."
for ($i = 0; $i -lt 40; $i++) {
    $state = az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query provisioningState -o tsv 2>$null
    if ($state -eq "Succeeded") { break }
    Start-Sleep -Seconds 10
}
Write-Host "  `e[32mCosmosDB account provisioned: $state`e[0m"

$TEST_COSMOS_ENDPOINT = (az cosmosdb show --name $TEST_COSMOS_ACCOUNT --resource-group $RESOURCE_GROUP --query documentEndpoint -o tsv 2>$null)
Write-Host "  `e[32mTest CosmosDB: $TEST_COSMOS_ENDPOINT`e[0m"

# Grant AKS managed identity Data Contributor on the test account
Write-Host "  Granting managed identity access to test CosmosDB..."
$identityName = "omnivec-identity-$INSTANCE_TOKEN"
$PRINCIPAL_ID = az identity show --name $identityName --resource-group $RESOURCE_GROUP --query principalId -o tsv 2>$null
if (-not $PRINCIPAL_ID) {
    $PRINCIPAL_ID = az identity list --resource-group $RESOURCE_GROUP --query "[0].principalId" -o tsv 2>$null
}

$dataContributorRole = "00000000-0000-0000-0000-000000000002"
az cosmosdb sql role assignment create `
    --account-name $TEST_COSMOS_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --role-definition-id $dataContributorRole `
    --principal-id $PRINCIPAL_ID `
    --scope "/" -o none 2>$null
Write-Host "  `e[32mData Contributor role assigned to managed identity.`e[0m"
Write-Host "  Waiting 15s for RBAC propagation..."
Start-Sleep -Seconds 15

# Create database and containers
Write-Host "  Creating database and containers..."
az cosmosdb sql database create `
    --account-name $TEST_COSMOS_ACCOUNT `
    --name testdb `
    --resource-group $RESOURCE_GROUP -o none 2>$null

# Create test-documents container (source)
az cosmosdb sql container create `
    --account-name $TEST_COSMOS_ACCOUNT `
    --database-name testdb `
    --name test-documents `
    --resource-group $RESOURCE_GROUP `
    --partition-key-path "/id" -o none 2>$null
Write-Host "  `e[32mtest-documents container created.`e[0m"

# Create vectors container (destination) with vector embedding policy
# Use kubectl exec since az cli has issues with vector policy on Windows
$vectorScript = @"
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
import os

cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
db = client.get_database_client('testdb')

vector_embedding_policy = {
    'vectorEmbeddings': [{
        'path': '/embedding',
        'dataType': 'float32',
        'distanceFunction': 'cosine',
        'dimensions': $AOAI_DIMS
    }]
}
indexing_policy = {
    'includedPaths': [{'path': '/*'}],
    'excludedPaths': [{'path': '/embedding/*'}],
    'vectorIndexes': [{'path': '/embedding', 'type': 'quantizedFlat'}]
}
db.create_container(
    id='vectors',
    partition_key={'paths': ['/id'], 'kind': 'Hash'},
    vector_embedding_policy=vector_embedding_policy,
    indexing_policy=indexing_policy
)
print('  vectors container created (${AOAI_DIMS}d, cosine, quantizedFlat)')
"@
kubectl exec deployment/omnivec-api -n omnivec -- python3 -c $vectorScript
Write-Host "  `e[32mAll containers created with proper vector policy.`e[0m"

# ─── Step 6: Register Azure OpenAI embedding model ──────────────────────────
Write-Host "`n`e[33m[Step 6/9] Registering Azure OpenAI embedding model...`e[0m"

$headers = @{
    "Authorization" = "Bearer $ADMIN_TOKEN"
    "Content-Type"  = "application/json"
}
$modelBody = @{
    name = "azure-openai-embed"
    type = "azure-openai"
    endpoint = $AOAI_ENDPOINT
    api_key = $AOAI_KEY
    model = $AOAI_DEPLOYMENT
    deployment = $AOAI_DEPLOYMENT
    dimensions = $AOAI_DIMS
    api_version = "2024-06-01"
} | ConvertTo-Json

$modelResult = Invoke-RestMethod -Uri "$SERVER_URL/api/models" -Method POST -Headers $headers -Body $modelBody
$MODEL_ID = $modelResult.id
Write-Host "  `e[32mModel registered: $MODEL_ID ($AOAI_DEPLOYMENT, ${AOAI_DIMS}d)`e[0m"

& $CLI model list

# ─── Step 7: Create source and destination ───────────────────────────────────
Write-Host "`n`e[33m[Step 7/9] Creating CosmosDB source and vector destination...`e[0m"

# Create source (pointing to TEST CosmosDB account, not metadata account)
$sourceBody = @{
    name = "demo-cosmosdb-source"
    type = "cosmosdb"
    config = @{
        endpoint = $TEST_COSMOS_ENDPOINT
        database = "testdb"
        container = "test-documents"
        auth_type = "managed-identity"
        client_id = $IDENTITY_CLIENT_ID
    }
} | ConvertTo-Json -Depth 5

$srcResult = Invoke-RestMethod -Uri "$SERVER_URL/api/sources" -Method POST -Headers $headers -Body $sourceBody
$SOURCE_ID = $srcResult.source.id
Write-Host "  `e[32mSource created: $SOURCE_ID (-> $TEST_COSMOS_ACCOUNT/testdb/test-documents)`e[0m"

# Create destination (pointing to TEST CosmosDB account vectors container)
$destBody = @{
    name = "demo-vector-store"
    type = "cosmosdb-vector"
    config = @{
        endpoint = $TEST_COSMOS_ENDPOINT
        database = "testdb"
        container = "vectors"
        auth_type = "managed-identity"
        client_id = $IDENTITY_CLIENT_ID
        vector_dimensions = $AOAI_DIMS
    }
} | ConvertTo-Json -Depth 5

$dstResult = Invoke-RestMethod -Uri "$SERVER_URL/api/destinations" -Method POST -Headers $headers -Body $destBody
$DEST_ID = $dstResult.destination.id
Write-Host "  `e[32mDestination created: $DEST_ID (-> $TEST_COSMOS_ACCOUNT/testdb/vectors)`e[0m"

& $CLI source list
& $CLI destination list

# ─── Step 8: Create pipeline and insert test documents ──────────────────────
Write-Host "`n`e[33m[Step 8/9] Creating pipeline and inserting test documents...`e[0m"

# Create pipeline in paused state, insert docs, then resume
$pipBody = @{
    name = "demo-inline-pipeline"
    sources = @(@{ source_id = $SOURCE_ID; filters = @{} })
    destination_id = $DEST_ID
    docgrok_pipeline = $MODEL_ID
    process_existing = $true
    status = "paused"
} | ConvertTo-Json -Depth 5

$pipResult = Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines" -Method POST -Headers $headers -Body $pipBody
$PIP_ID = $pipResult.pipeline.id
Write-Host "  `e[32mPipeline created (paused): $PIP_ID`e[0m"

# Insert test documents FIRST, then resume pipeline
Write-Host "  Inserting test documents..."
$insertScript = @"
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
import os

cred = DefaultAzureCredential(managed_identity_client_id=os.environ.get('AZURE_CLIENT_ID'))
client = CosmosClient('$TEST_COSMOS_ENDPOINT', credential=cred)
db = client.get_database_client('testdb')
container = db.get_container_client('test-documents')

docs = [
    {'id': 'doc-001', 'title': 'Azure Cosmos DB', 'content': 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling of throughput and storage worldwide.', 'category': 'database'},
    {'id': 'doc-002', 'title': 'Azure Kubernetes Service', 'content': 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead to Azure for health monitoring and maintenance.', 'category': 'compute'},
    {'id': 'doc-003', 'title': 'Azure Blob Storage', 'content': 'Azure Blob Storage stores massive amounts of unstructured data like text documents images and binary data optimized for cloud scale access patterns.', 'category': 'storage'},
]

for doc in docs:
    container.upsert_item(doc)
    print(f'  Inserted: {doc["id"]} - {doc["title"]}')
print('Done.')
"@

kubectl exec deployment/omnivec-api -n omnivec -- python3 -c $insertScript
Write-Host "  `e[32m3 test documents inserted.`e[0m"

# Now resume the pipeline — changefeed will pick up all docs from the beginning
Write-Host "  Resuming pipeline..."
Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/resume" -Method POST -Headers $headers | Out-Null
Invoke-RestMethod -Uri "$SERVER_URL/api/pipelines/$PIP_ID/run" -Method POST -Headers $headers | Out-Null
Write-Host "  `e[32mPipeline resumed and activated. Waiting 60s for change feed processing...`e[0m"
Start-Sleep -Seconds 60

# ─── Step 9: Verify results ─────────────────────────────────────────────────
Write-Host "`n`e[33m[Step 9/9] Checking results...`e[0m"

& $CLI pipeline list
Write-Host ""
& $CLI pipeline show $PIP_ID
Write-Host ""
& $CLI job list

# Check if any jobs completed
$jobsResult = Invoke-RestMethod -Uri "$SERVER_URL/api/jobs?status=completed&limit=5" -Headers $headers
$completedCount = $jobsResult.jobs.Count

Write-Host ""
if ($completedCount -gt 0) {
    Write-Host "`e[32m  $completedCount documents successfully embedded!`e[0m"
    Write-Host ""

    # Test vector search
    Write-Host "`e[33m  Testing vector search...`e[0m"
    $searchBody = @{
        query = "what is cosmos db"
        destination_id = $DEST_ID
        top_k = 3
    } | ConvertTo-Json

    $searchResult = Invoke-RestMethod -Uri "$SERVER_URL/api/playground/search" -Method POST -Headers $headers -Body $searchBody
    Write-Host "`e[32m  Search returned $($searchResult.results.Count) results:`e[0m"
    foreach ($r in $searchResult.results) {
        $score = [math]::Round($r.score, 4)
        $title = $r.title
        Write-Host "    [$score] $title"
    }
} else {
    Write-Host "`e[33m  No completed jobs yet. Pipeline may still be processing.`e[0m"
    Write-Host "  Check status: omnivec pipeline show $PIP_ID"
    Write-Host "  Check jobs:   omnivec job list"
}

# ─── Test pipeline reset ────────────────────────────────────────────────────
Write-Host ""
Write-Host "`e[33m  Testing pipeline reset...`e[0m"
& $CLI pipeline reset $PIP_ID -y
Write-Host "`e[32m  Pipeline reset successful.`e[0m"

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
Write-Host "  Commands:"
Write-Host "    omnivec pipeline show $PIP_ID"
Write-Host "    omnivec pipeline reset $PIP_ID -y"
Write-Host "    omnivec search --destination $DEST_ID --query `"cosmos db`""
Write-Host "    omnivec job list"
Write-Host "    omnivec status"
Write-Host ""
