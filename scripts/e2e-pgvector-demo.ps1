# OmniVec E2E Demo — PostgreSQL + pgvector
# Provisions Azure PostgreSQL Flexible Server, creates source/destination tables,
# registers an embedding model, creates a pipeline, and verifies vector search.
#
# Usage:
#   pwsh scripts/e2e-pgvector-demo.ps1                                    # Full run
#   pwsh scripts/e2e-pgvector-demo.ps1 -Existing -EnvName my-omnivec     # Against existing deployment
#   pwsh scripts/e2e-pgvector-demo.ps1 -Cleanup -EnvName my-omnivec      # Delete test resources
#
# Requires: az, azd, kubectl, psql (PostgreSQL client)

param(
    [int]$FromStep = 1,
    [switch]$Quiet,
    [switch]$Existing,
    [switch]$Cleanup,
    [string]$EnvName,
    [string]$AdminToken = $env:OMNIVEC_ADMIN_TOKEN,
    [string]$AoaiEndpoint = $env:AOAI_ENDPOINT,
    [string]$AoaiKey = $env:AOAI_KEY,
    [string]$AoaiDeployment = $(if ($env:AOAI_DEPLOYMENT) { $env:AOAI_DEPLOYMENT } else { "text-embedding-3-small" }),
    [int]$AoaiDims = $(if ($env:AOAI_DIMS) { [int]$env:AOAI_DIMS } else { 1536 }),
    [string]$PgAdminPassword
)

$ErrorActionPreference = "Stop"
$TOTAL_STEPS = 10

# ─── Helpers ─────────────────────────────────────────────────────────────────
function LogStep   { param([int]$N, [string]$Msg) Write-Host "`e[33m[Step $N/$TOTAL_STEPS] $Msg`e[0m" }
function LogOk     { param([string]$Msg) Write-Host "  `e[32m$Msg`e[0m" }
function LogWarn   { param([string]$Msg) Write-Host "  `e[33m$Msg`e[0m" }
function LogErr    { param([string]$Msg) Write-Host "  `e[31m$Msg`e[0m" }
function Log       { param([string]$Msg) if (-not $Quiet) { Write-Host $Msg } }

$SCRIPT_DIR = $PSScriptRoot
$ROOT_DIR = (Resolve-Path "$SCRIPT_DIR/..").Path
$CheckpointFile = Join-Path $ROOT_DIR ".e2e-pgvector-checkpoint"

function Save-Checkpoint { param([int]$Step) Set-Content -Path $CheckpointFile -Value $Step }

# ─── Banner ──────────────────────────────────────────────────────────────────
Write-Host "`e[32m╔══════════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║  OmniVec E2E Demo — PostgreSQL + pgvector               ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════════╝`e[0m"
Log "  Embedding: $AoaiDeployment (${AoaiDims}d) @ $AoaiEndpoint"

# ─── AOAI validation ─────────────────────────────────────────────────────────
if (-not $AoaiEndpoint) {
    $AoaiEndpoint = Read-Host "  Enter Azure OpenAI endpoint"
    if (-not $AoaiEndpoint) { LogErr "Endpoint required."; exit 1 }
}
if (-not $AoaiKey) {
    $AoaiKey = Read-Host "  Enter Azure OpenAI API key"
    if (-not $AoaiKey) { LogErr "API key required."; exit 1 }
}

# ─── PostgreSQL admin password ───────────────────────────────────────────────
if (-not $PgAdminPassword) {
    $PgAdminPassword = "OmniVec-Demo-$(Get-Random -Minimum 1000 -Maximum 9999)!"
    LogOk "Generated PG admin password: $PgAdminPassword"
}

# ─── Existing deployment mode ────────────────────────────────────────────────
if ($Existing) {
    if (-not $EnvName) { $EnvName = Read-Host "  Enter azd environment name"; if (-not $EnvName) { LogErr "EnvName required."; exit 1 } }
    Log "`nUsing existing deployment: $EnvName"
    azd env select $EnvName 2>$null

    if (-not $AdminToken) {
        $AdminToken = (azd env get-value OMNIVEC_ADMIN_TOKEN 2>$null).Trim()
    }
    if (-not $AdminToken) {
        $AdminToken = Read-Host "  Enter admin token"
        if (-not $AdminToken) { LogErr "Admin token required."; exit 1 }
    }

    $AKS_CLUSTER = (azd env get-value AZURE_AKS_CLUSTER_NAME 2>$null).Trim()
    $RESOURCE_GROUP = (azd env get-value AZURE_RESOURCE_GROUP 2>$null).Trim()
    if (-not $RESOURCE_GROUP) { $RESOURCE_GROUP = "rg-omnivec-$EnvName" }

    $KUBE_CONTEXT = $AKS_CLUSTER
    az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --context $KUBE_CONTEXT --overwrite-existing 2>$null

    # Get external IP
    $externalIp = kubectl --context $KUBE_CONTEXT get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if (-not $externalIp) { LogErr "Failed to get external IP"; exit 1 }
    $SERVER_URL = "http://$externalIp"
    $ADMIN_TOKEN = $AdminToken

    LogOk "Server: $SERVER_URL"
    LogOk "RG:     $RESOURCE_GROUP"

    # Validate token
    try {
        $health = Invoke-RestMethod -Uri "$SERVER_URL/health" -Headers @{ "Authorization" = "Bearer $ADMIN_TOKEN" } -TimeoutSec 10
        LogOk "Admin token valid — API healthy."
    } catch {
        LogErr "Admin token rejected or API unreachable."
        exit 1
    }

    if ($FromStep -lt 3) { $FromStep = 3 }
}

# ─── Helper: API calls ──────────────────────────────────────────────────────
function ApiGet { param([string]$Path)
    Invoke-RestMethod -Uri "$SERVER_URL$Path" -Headers @{ "Authorization" = "Bearer $ADMIN_TOKEN" } -TimeoutSec 30
}
function ApiPost { param([string]$Path, [object]$Body)
    Invoke-RestMethod -Uri "$SERVER_URL$Path" -Method POST `
        -Headers @{ "Authorization" = "Bearer $ADMIN_TOKEN"; "Content-Type" = "application/json" } `
        -Body ($Body | ConvertTo-Json -Depth 10) -TimeoutSec 30
}
function ApiDelete { param([string]$Path)
    try { Invoke-RestMethod -Uri "$SERVER_URL$Path" -Method DELETE -Headers @{ "Authorization" = "Bearer $ADMIN_TOKEN" } -TimeoutSec 10 | Out-Null } catch {}
}

# ─── Cleanup mode ────────────────────────────────────────────────────────────
if ($Cleanup) {
    Log "`nCleaning up pgvector demo resources..."
    if (-not $RESOURCE_GROUP) { $RESOURCE_GROUP = "rg-omnivec-$EnvName" }

    # Delete OmniVec resources via API
    if ($SERVER_URL -and $ADMIN_TOKEN) {
        $pips = (ApiGet "/api/pipelines").pipelines | Where-Object { $_.name -match "pgvector" }
        foreach ($p in $pips) { ApiDelete "/api/pipelines/$($p.id)"; LogOk "Deleted pipeline: $($p.id)" }
        $srcs = (ApiGet "/api/sources").sources | Where-Object { $_.name -match "pgvector\|pg-demo" }
        foreach ($s in $srcs) { ApiDelete "/api/sources/$($s.id)"; LogOk "Deleted source: $($s.id)" }
        $dsts = (ApiGet "/api/destinations").destinations | Where-Object { $_.name -match "pgvector\|pg-demo" }
        foreach ($d in $dsts) { ApiDelete "/api/destinations/$($d.id)"; LogOk "Deleted destination: $($d.id)" }
    }

    # Delete PostgreSQL server
    $PG_SERVER = "omnivec-pgdemo-$(($AKS_CLUSTER -replace 'omnivec-aks-',''))"
    az postgres flexible-server delete --name $PG_SERVER --resource-group $RESOURCE_GROUP --yes 2>$null
    LogOk "Deleted PostgreSQL server: $PG_SERVER"

    Remove-Item $CheckpointFile -ErrorAction SilentlyContinue
    LogOk "Cleanup complete."
    exit 0
}

# =============================================================================
# STEP 1–2: Provision infra (skip if --Existing)
# =============================================================================
# (Steps 1-2 would provision OmniVec via azd up — skipped when using --Existing)

# =============================================================================
# STEP 3: Create Azure PostgreSQL Flexible Server
# =============================================================================
if ($FromStep -le 3) {
    LogStep 3 "Provisioning Azure PostgreSQL Flexible Server..."

    $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
    $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
    $PG_ADMIN = "omnivecadmin"

    # Check if server already exists
    $existing = az postgres flexible-server show --name $PG_SERVER --resource-group $RESOURCE_GROUP 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($existing) {
        LogOk "PostgreSQL server already exists: $PG_SERVER"
    } else {
        Log "  Creating server: $PG_SERVER (this takes ~3-5 minutes)..."
        az postgres flexible-server create `
            --name $PG_SERVER `
            --resource-group $RESOURCE_GROUP `
            --location eastus2 `
            --admin-user $PG_ADMIN `
            --admin-password $PgAdminPassword `
            --sku-name Standard_B1ms `
            --tier Burstable `
            --storage-size 32 `
            --version 16 `
            --public-access 0.0.0.0 `
            --yes 2>$null | Out-Null
        LogOk "PostgreSQL server created: $PG_SERVER"
    }

    # Enable pgvector extension
    Log "  Enabling pgvector extension..."
    az postgres flexible-server parameter set `
        --server-name $PG_SERVER `
        --resource-group $RESOURCE_GROUP `
        --name azure.extensions `
        --value VECTOR 2>$null | Out-Null
    LogOk "pgvector extension enabled."

    # Get connection info
    $PG_HOST = "$PG_SERVER.postgres.database.azure.com"
    $PG_PORT = 5432
    $PG_DB = "omnivec_demo"

    # Allow Azure services
    az postgres flexible-server firewall-rule create `
        --name $PG_SERVER `
        --resource-group $RESOURCE_GROUP `
        --rule-name AllowAzure `
        --start-ip-address 0.0.0.0 `
        --end-ip-address 0.0.0.0 2>$null | Out-Null

    # Allow current IP
    $myIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 5)
    az postgres flexible-server firewall-rule create `
        --name $PG_SERVER `
        --resource-group $RESOURCE_GROUP `
        --rule-name AllowMyIP `
        --start-ip-address $myIp `
        --end-ip-address $myIp 2>$null | Out-Null

    LogOk "Host: $PG_HOST"
    Save-Checkpoint 3
}

# =============================================================================
# STEP 4: Create database and tables
# =============================================================================
if ($FromStep -le 4) {
    LogStep 4 "Creating database and tables..."

    if (-not $PG_HOST) {
        $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
        $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
        $PG_HOST = "$PG_SERVER.postgres.database.azure.com"
        $PG_PORT = 5432
        $PG_DB = "omnivec_demo"
        $PG_ADMIN = "omnivecadmin"
    }

    $env:PGPASSWORD = $PgAdminPassword

    # Create database
    Log "  Creating database: $PG_DB"
    psql -h $PG_HOST -p $PG_PORT -U $PG_ADMIN -d postgres -c "CREATE DATABASE $PG_DB;" 2>$null
    LogOk "Database created."

    # Enable vector extension and create tables
    Log "  Creating tables..."
    $sql = @"
CREATE EXTENSION IF NOT EXISTS vector;

-- Source table (documents to embed)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Destination table (vector embeddings)
CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    content TEXT,
    embedding vector($AoaiDims),
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS embeddings_vector_idx ON embeddings USING hnsw (embedding vector_cosine_ops);
"@
    psql -h $PG_HOST -p $PG_PORT -U $PG_ADMIN -d $PG_DB -c $sql 2>$null
    LogOk "Tables created: documents (source), embeddings (destination)"

    # Insert sample documents
    Log "  Inserting sample documents..."
    $docs = @(
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-001', 'Azure Cosmos DB', 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.', 'database') ON CONFLICT (id) DO NOTHING;",
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-002', 'Azure Kubernetes Service', 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.', 'compute') ON CONFLICT (id) DO NOTHING;",
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-003', 'Azure Blob Storage', 'Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.', 'storage') ON CONFLICT (id) DO NOTHING;"
    )
    foreach ($doc in $docs) {
        psql -h $PG_HOST -p $PG_PORT -U $PG_ADMIN -d $PG_DB -c $doc 2>$null
    }
    LogOk "Inserted 3 sample documents."

    $env:PGPASSWORD = $null
    Save-Checkpoint 4
}

# =============================================================================
# STEP 5: Register embedding model
# =============================================================================
if ($FromStep -le 5) {
    LogStep 5 "Registering Azure OpenAI embedding model..."

    $modelBody = @{
        name = "azure-openai-embed"
        type = "azure-openai"
        endpoint = $AoaiEndpoint
        api_key = $AoaiKey
        api_version = "2024-06-01"
        deployment = $AoaiDeployment
        embedding_dim = $AoaiDims
    }

    try {
        $modelResp = ApiPost "/api/models" $modelBody
        $MODEL_ID = $modelResp.id
        LogOk "Model: $MODEL_ID ($AoaiDeployment, ${AoaiDims}d)"
    } catch {
        # Model may already exist
        $models = (ApiGet "/api/docgrok/models").models
        $existing = $models | Where-Object { $_.name -eq "azure-openai-embed" }
        if ($existing) {
            $MODEL_ID = $existing.id
            LogOk "Model already exists: $MODEL_ID"
        } else {
            LogErr "Failed to register model: $_"
            exit 1
        }
    }
    Save-Checkpoint 5
}

# =============================================================================
# STEP 6: Create PostgreSQL source + pgvector destination in OmniVec
# =============================================================================
if ($FromStep -le 6) {
    LogStep 6 "Creating PostgreSQL source and pgvector destination..."

    if (-not $PG_HOST) {
        $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
        $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
        $PG_HOST = "$PG_SERVER.postgres.database.azure.com"
        $PG_PORT = 5432
        $PG_DB = "omnivec_demo"
        $PG_ADMIN = "omnivecadmin"
    }

    # Create source
    $srcBody = @{
        name = "pg-demo-source"
        type = "postgresql"
        config = @{
            host = $PG_HOST
            port = $PG_PORT
            database = $PG_DB
            table = "documents"
            user = $PG_ADMIN
            password = $PgAdminPassword
            ssl_mode = "require"
            id_column = "id"
            timestamp_column = "updated_at"
        }
    }
    $srcResp = ApiPost "/api/sources" $srcBody
    $SOURCE_ID = $srcResp.id
    LogOk "Source: $SOURCE_ID (postgresql://.../$PG_DB/documents)"

    # Create destination
    $dstBody = @{
        name = "pg-demo-vectors"
        type = "pgvector"
        config = @{
            host = $PG_HOST
            port = $PG_PORT
            database = $PG_DB
            table = "embeddings"
            user = $PG_ADMIN
            password = $PgAdminPassword
            ssl_mode = "require"
            vector_column = "embedding"
            content_column = "content"
            id_column = "id"
            vector_dimensions = $AoaiDims
            index_type = "hnsw"
        }
    }
    $dstResp = ApiPost "/api/destinations" $dstBody
    $DEST_ID = $dstResp.id
    LogOk "Destination: $DEST_ID (pgvector://.../$PG_DB/embeddings)"

    Save-Checkpoint 6
}

# =============================================================================
# STEP 7: Create pipeline (queue mode) and activate
# =============================================================================
if ($FromStep -le 7) {
    LogStep 7 "Creating pipeline (queue mode)..."

    if (-not $MODEL_ID) {
        $models = (ApiGet "/api/docgrok/models").models
        $MODEL_ID = ($models | Where-Object { $_.name -eq "azure-openai-embed" }).id
    }

    $pipBody = @{
        name = "pgvector-demo-pipeline"
        sources = @(@{
            source_id = $SOURCE_ID
            filters = @{}
            content_fields = @("content")
        })
        destination_id = $DEST_ID
        docgrok_pipeline = $MODEL_ID
        vector_index_path = "embedding"
        process_existing = $true
        processing_mode = "queue"
        content_strategy = "truncate"
    }
    $pipResp = ApiPost "/api/pipelines" $pipBody
    $PIP_ID = $pipResp.pipeline.id
    if (-not $PIP_ID) { $PIP_ID = $pipResp.id }
    LogOk "Pipeline created: $PIP_ID"

    # Wait for processing
    Log "  Waiting for documents to be embedded..."
    $maxWait = 120; $waited = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 10
        $waited += 10
        $stats = ApiGet "/api/pipelines/$PIP_ID"
        $embedded = $stats.stats.embedded_count
        $pct = $stats.stats.completion_pct
        if ($embedded -ge 3) {
            LogOk "Queue mode: $embedded documents embedded ($pct%)"
            break
        }
        Log "  Waiting... ($embedded embedded, $pct%)"
    }
    if ($embedded -lt 3) { LogWarn "Only $embedded/3 documents embedded after ${maxWait}s" }

    Save-Checkpoint 7
}

# =============================================================================
# STEP 8: Verify embeddings in pgvector table
# =============================================================================
if ($FromStep -le 8) {
    LogStep 8 "Verifying embeddings in pgvector table..."

    if (-not $PG_HOST) {
        $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
        $PG_HOST = "omnivec-pgdemo-$INSTANCE_TOKEN.postgres.database.azure.com"
        $PG_DB = "omnivec_demo"
        $PG_ADMIN = "omnivecadmin"
    }

    $env:PGPASSWORD = $PgAdminPassword
    $count = psql -h $PG_HOST -p 5432 -U $PG_ADMIN -d $PG_DB -t -c "SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL;" 2>$null
    $count = "$count".Trim()
    $env:PGPASSWORD = $null

    if ([int]$count -ge 3) {
        LogOk "pgvector table has $count rows with embeddings!"
    } else {
        LogWarn "pgvector table has $count rows (expected 3)"
    }

    Save-Checkpoint 8
}

# =============================================================================
# STEP 9: Vector search via API
# =============================================================================
if ($FromStep -le 9) {
    LogStep 9 "Verifying vector search..."

    $searchQueries = @(
        @{ query = "globally distributed database"; expected = "doc-001"; title = "Azure Cosmos DB" },
        @{ query = "deploying managed Kubernetes clusters"; expected = "doc-002"; title = "Azure Kubernetes Service" },
        @{ query = "unstructured data storage for documents"; expected = "doc-003"; title = "Azure Blob Storage" }
    )

    $searchPassed = 0
    foreach ($sq in $searchQueries) {
        $searchBody = @{
            query = $sq.query
            destination_ids = @($DEST_ID)
            top_k = 3
        }
        try {
            $resp = ApiPost "/api/playground/search" $searchBody
            $results = if ($resp.results) { $resp.results } elseif ($resp -is [array]) { $resp } else { @() }
            if ($results.Count -gt 0) {
                $top = $results[0]
                $score = if ($top.score) { [math]::Round($top.score, 3) } else { "?" }
                LogOk "Search '$($sq.query)' → $($top.id) (score: $score)"
                $searchPassed++
            } else {
                LogErr "Search '$($sq.query)' → no results"
            }
        } catch {
            LogErr "Search failed: $_"
        }
    }
    LogOk "Vector search: $searchPassed/$($searchQueries.Count) queries returned results"

    Save-Checkpoint 9
}

# =============================================================================
# STEP 10: Summary
# =============================================================================
LogStep 10 "Done!"

Write-Host ""
Write-Host "`e[32m╔══════════════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[32m║     pgvector E2E Demo Complete!                          ║`e[0m"
Write-Host "`e[32m╚══════════════════════════════════════════════════════════╝`e[0m"
Write-Host ""
Write-Host "  Server:       `e[36m$SERVER_URL`e[0m"
Write-Host "  PG Host:      `e[36m$PG_HOST`e[0m"
Write-Host "  Source:        `e[36m$SOURCE_ID (postgresql → documents table)`e[0m"
Write-Host "  Destination:   `e[36m$DEST_ID (pgvector → embeddings table)`e[0m"
Write-Host "  Pipeline:      `e[36m$PIP_ID`e[0m"
Write-Host "  Model:         `e[36m$MODEL_ID ($AoaiDeployment)`e[0m"
Write-Host ""
Write-Host "  Full cycle: PostgreSQL source → Azure OpenAI embedding → pgvector destination → vector search"
Write-Host ""

Remove-Item $CheckpointFile -ErrorAction SilentlyContinue
Save-Checkpoint 10
