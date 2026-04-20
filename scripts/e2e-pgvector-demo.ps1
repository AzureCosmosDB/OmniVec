# OmniVec E2E Demo — PostgreSQL + pgvector
# Provisions Azure PostgreSQL Flexible Server, creates source/destination tables,
# registers an embedding model, creates a pipeline, and verifies vector search.
#
# Usage:
#   pwsh scripts/e2e-pgvector-demo.ps1                                    # Full run
#   pwsh scripts/e2e-pgvector-demo.ps1 -Existing -EnvName my-omnivec     # Against existing deployment
#   pwsh scripts/e2e-pgvector-demo.ps1 -Cleanup -EnvName my-omnivec      # Delete test resources
#
# Requires: az, azd, kubectl (SQL runs via `az postgres flexible-server execute`, no local psql needed)

param(
    [int]$FromStep = 1,
    [switch]$Quiet,
    [switch]$Existing,
    [switch]$Cleanup,
    [Alias('h','?')][switch]$Help,
    [string]$EnvName,
    [string]$AdminToken = $env:OMNIVEC_ADMIN_TOKEN,
    [string]$AoaiEndpoint = $env:AOAI_ENDPOINT,
    [string]$AoaiKey = $env:AOAI_KEY,
    [string]$AoaiDeployment = $(if ($env:AOAI_DEPLOYMENT) { $env:AOAI_DEPLOYMENT } else { "text-embedding-3-small" }),
    [int]$AoaiDims = $(if ($env:AOAI_DIMS) { [int]$env:AOAI_DIMS } else { 1536 }),
    [string]$PgAdminPassword
)

if ($Help) {
@'
OmniVec E2E Demo — PostgreSQL + pgvector (Windows / pwsh 7+)

Provisions an Azure PostgreSQL Flexible Server with the pgvector extension,
registers an Azure OpenAI embedding model, creates source/destination tables,
runs a pipeline, and verifies vector similarity search.

USAGE:
  pwsh scripts/e2e-pgvector-demo.ps1 [OPTIONS]

MODES (mutually exclusive; default = run against an existing OmniVec deployment):
  -Existing               Run against an already-provisioned azd env
                          (this is also the default). Requires -EnvName.
  -Cleanup                Delete PG server + OmniVec test entities.
                          Requires -EnvName.

OPTIONS:
  -h, -Help               Show this help and exit.
  -FromStep N             Start at step N (1-10). Auto-resumes from last
                          successful step via .e2e-pgvector-checkpoint.
  -EnvName NAME           azd environment name.
  -AdminToken TOKEN       OmniVec admin token (skips auto-discovery).
  -PgAdminPassword PW     PostgreSQL admin password
                          (default: auto-generated OmniVec-Demo-NNNN!).
  -AoaiEndpoint URL       Azure OpenAI endpoint.
  -AoaiKey KEY            Azure OpenAI API key.
  -AoaiDeployment NAME    Embedding deployment (default: text-embedding-3-small).
  -AoaiDims N             Embedding dimensions (default: 1536).
  -Quiet                  Minimal output.

ENVIRONMENT VARIABLES (used when flag is not passed):
  OMNIVEC_ADMIN_TOKEN, AOAI_ENDPOINT, AOAI_KEY, AOAI_DEPLOYMENT, AOAI_DIMS

REQUIREMENTS:
  az, azd, kubectl  (SQL runs via `az postgres flexible-server execute` —
  no local psql client needed.)

EXAMPLES:
  # Default: run against an existing OmniVec deployment
  pwsh scripts/e2e-pgvector-demo.ps1 -EnvName my-omnivec `
      -AoaiEndpoint https://my-aoai.openai.azure.com -AoaiKey $env:AOAI_KEY

  # Resume after a failure (auto-detects last successful step)
  pwsh scripts/e2e-pgvector-demo.ps1 -EnvName my-omnivec

  # Skip ahead manually
  pwsh scripts/e2e-pgvector-demo.ps1 -EnvName my-omnivec -FromStep 6

  # Clean up (delete PG server + demo entities)
  pwsh scripts/e2e-pgvector-demo.ps1 -Cleanup -EnvName my-omnivec

STEPS (10 total):
   1. Parse config, collect AOAI credentials
   2. Resolve AKS cluster + RG from azd env (fallback to discovery)
   3. Provision Azure PostgreSQL Flexible Server + enable pgvector
   4. Create database, source/destination tables, sample rows
   5. Register Azure OpenAI embedding model in OmniVec
   6. Create PG source + PG destination in OmniVec
   7. Create pipeline
   8. Verify embeddings populated in destination table
   9. Vector similarity search smoke test
  10. Summary + cleanup hints

Linux/macOS/WSL: use the shell variant instead:
  ./scripts/e2e-pgvector-demo.sh --help
'@ | Write-Host
    exit 0
}

# Require PowerShell 7+ (uses `e ANSI escape + relies on pwsh native-stderr handling).
if ($PSVersionTable.PSVersion.Major -lt 7) {
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) {
        $forwarded = @('-NoLogo', '-NoProfile', '-File', $PSCommandPath)
        foreach ($kv in $PSBoundParameters.GetEnumerator()) {
            if ($kv.Value -is [System.Management.Automation.SwitchParameter]) {
                if ($kv.Value.IsPresent) { $forwarded += "-$($kv.Key)" }
            } else {
                $forwarded += "-$($kv.Key)"; $forwarded += "$($kv.Value)"
            }
        }
        Write-Host "Relaunching under PowerShell 7 (pwsh)..." -ForegroundColor DarkGray
        & $pwshCmd.Source @forwarded
        exit $LASTEXITCODE
    }
    Write-Host "ERROR: This script requires PowerShell 7+ (pwsh)." -ForegroundColor Red
    Write-Host "  Install pwsh: winget install --id Microsoft.PowerShell --source winget" -ForegroundColor Yellow
    Write-Host "  Then run:     pwsh -File $PSCommandPath" -ForegroundColor Yellow
    exit 1
}

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

# ─── Resolve native CLIs (avoid PS profile shadowing of `az`/`azd`) ──────────
function Resolve-NativeExe {
    param([string]$Name)
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) {
        LogErr "Required CLI not found on PATH: $Name"
        exit 1
    }
    return $cmd.Source
}
$AzExe  = Resolve-NativeExe 'az'
$AzdExe = Resolve-NativeExe 'azd'

# ─── PG SQL helper via throwaway kubectl pod (no local psql, no az extensions) ─
# Runs psql inside a one-shot postgres:16-alpine pod in the AKS cluster. The
# pod has network access to the Azure PostgreSQL Flexible Server (both in
# Azure) and avoids any dependency on local psql or the fragile
# `rdbms-connect` az extension. Requires only kubectl (already required).
#
# To dodge DNS-propagation delays after creating a fresh Azure PG flexible
# server, we resolve the host's IP from the local box (with retry) and
# inject it as a hostAliases entry on the pod so the container bypasses
# CoreDNS entirely.
function Resolve-PgHostIp {
    param([Parameter(Mandatory)][string]$PgHost, [int]$TimeoutSec = 120)
    $waited = 0
    while ($waited -lt $TimeoutSec) {
        try {
            $rec = Resolve-DnsName -Name $PgHost -Type A -ErrorAction Stop 2>$null |
                   Where-Object { $_.IPAddress -and $_.IPAddress -match '^\d+\.\d+\.\d+\.\d+$' } |
                   Select-Object -First 1
            if ($rec -and $rec.IPAddress) { return $rec.IPAddress }
        } catch {}
        Start-Sleep -Seconds 5; $waited += 5
    }
    return $null
}

function Invoke-PgSql {
    param(
        [Parameter(Mandatory)][string]$PgHost,
        [Parameter(Mandatory)][string]$AdminUser,
        [Parameter(Mandatory)][string]$AdminPassword,
        [string]$Database = 'postgres',
        [int]$Port = 5432,
        [Parameter(Mandatory)][string]$Query,
        [string]$Namespace = 'default'
    )
    # Cache the resolved IP per-run.
    if (-not $script:_pgHostIp -or $script:_pgHostIpFor -ne $PgHost) {
        Log "  Resolving $PgHost ..."
        $ip = Resolve-PgHostIp -PgHost $PgHost
        if (-not $ip) {
            LogErr "Could not resolve $PgHost within 120s (DNS propagation lag?)."
            throw "DNS resolution failed for $PgHost"
        }
        Log "  -> $ip"
        $script:_pgHostIp = $ip
        $script:_pgHostIpFor = $PgHost
    }
    $ip = $script:_pgHostIp

    $suffix = [Guid]::NewGuid().ToString('N').Substring(0,8)
    $podName = "ov-pgclient-$suffix"

    $overrides = @{
        apiVersion = 'v1'
        spec = @{
            restartPolicy = 'Never'
            hostAliases = @(@{ ip = $ip; hostnames = @($PgHost) })
            containers = @(@{
                name  = 'psql'
                image = 'postgres:16-alpine'
                env = @(
                    @{ name = 'PGPASSWORD'; value = $AdminPassword }
                    @{ name = 'PGSSLMODE';  value = 'require' }
                )
                command = @('psql','-h',$PgHost,'-p',"$Port",'-U',$AdminUser,
                            '-d',$Database,'-v','ON_ERROR_STOP=1','-t','-A','-c',$Query)
            })
        }
    } | ConvertTo-Json -Depth 10 -Compress

    $createOut = & kubectl run $podName `
        --namespace $Namespace `
        --image=postgres:16-alpine `
        --restart=Never `
        --overrides=$overrides 2>&1
    if ($LASTEXITCODE -ne 0) {
        LogErr "kubectl run failed: $createOut"
        throw "Failed to launch psql pod"
    }

    $timeoutSec = 120
    $waited = 0
    $phase = ''
    while ($waited -lt $timeoutSec) {
        $phase = (& kubectl get pod $podName -n $Namespace -o jsonpath='{.status.phase}' 2>$null)
        if ($phase -eq 'Succeeded' -or $phase -eq 'Failed') { break }
        Start-Sleep -Seconds 2; $waited += 2
    }
    $logs = (& kubectl logs $podName -n $Namespace 2>&1) -join "`n"
    & kubectl delete pod $podName -n $Namespace --grace-period=0 --force 2>&1 | Out-Null

    if ($phase -ne 'Succeeded') {
        LogErr "PG SQL failed (pod phase=$phase, db=$Database):`n$logs"
        throw "PG SQL execution failed"
    }
    return $logs
}

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

# ─── Default to Existing mode when no flag given (provisioning steps 1-2 are stubbed) ──
if (-not $Existing -and -not $Cleanup) {
    Log "`n  No mode flag given; defaulting to -Existing (against an already-provisioned azd env)."
    $Existing = $true
}

# ─── Existing deployment mode ────────────────────────────────────────────────
if ($Existing) {
    if (-not $EnvName) { $EnvName = Read-Host "  Enter azd environment name"; if (-not $EnvName) { LogErr "EnvName required."; exit 1 } }
    Log "`nUsing existing deployment: $EnvName"
    & $AzdExe env select $EnvName 2>$null

    if (-not $AdminToken) {
        $AdminToken = (& $AzdExe env get-value OMNIVEC_ADMIN_TOKEN 2>$null | Out-String).Trim()
    }
    if (-not $AdminToken) {
        $AdminToken = Read-Host "  Enter admin token"
        if (-not $AdminToken) { LogErr "Admin token required."; exit 1 }
    }

    $AKS_CLUSTER = (& $AzdExe env get-value AZURE_AKS_CLUSTER_NAME 2>$null | Out-String).Trim()
    $RESOURCE_GROUP = (& $AzdExe env get-value AZURE_RESOURCE_GROUP 2>$null | Out-String).Trim()
    if (-not $RESOURCE_GROUP) { $RESOURCE_GROUP = "rg-omnivec-$EnvName" }

    $KUBE_CONTEXT = $AKS_CLUSTER
    & $AzExe aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --context $KUBE_CONTEXT --overwrite-existing 2>$null

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

# Create-or-get: POST, but if the resource already exists, look it up by name.
function ApiCreateOrGet {
    param(
        [Parameter(Mandatory)][string]$CollectionPath,   # e.g. '/api/sources'
        [Parameter(Mandatory)][string]$ListField,        # e.g. 'sources'
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Body
    )
    try {
        return ApiPost $CollectionPath $Body
    } catch {
        $msg = "$_"
        if ($msg -match 'already exists' -or $msg -match '409') {
            $existing = (ApiGet $CollectionPath).$ListField | Where-Object { $_.name -eq $Name } | Select-Object -First 1
            if ($existing) {
                LogOk "Reusing existing $($CollectionPath): $($existing.id) (name=$Name)"
                return $existing
            }
        }
        throw
    }
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
    & $AzExe postgres flexible-server delete --name $PG_SERVER --resource-group $RESOURCE_GROUP --yes 2>$null
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

    # Robust AKS_CLUSTER discovery — azd env var may be empty if the env was
    # created by a different tool; fall back to discovering it from the RG.
    if (-not $AKS_CLUSTER -or $AKS_CLUSTER.Trim() -eq '') {
        if (-not $RESOURCE_GROUP) {
            $RESOURCE_GROUP = "rg-omnivec-$EnvName"
        }
        $aksRaw = & $AzExe aks list --resource-group $RESOURCE_GROUP --query "[?starts_with(name,'omnivec-aks-')].name | [0]" -o tsv 2>$null
        $AKS_CLUSTER = if ($aksRaw) { "$aksRaw".Trim() } else { '' }
        if (-not $AKS_CLUSTER) {
            LogErr "Could not determine AKS cluster name in RG '$RESOURCE_GROUP'. Set AZURE_AKS_CLUSTER_NAME in azd env, or pass -EnvName with -Existing."
            exit 1
        }
        LogOk "Discovered AKS cluster: $AKS_CLUSTER"
    }

    $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
    if (-not $INSTANCE_TOKEN -or $INSTANCE_TOKEN.Trim() -eq '') {
        LogErr "Empty instance token derived from AKS cluster '$AKS_CLUSTER'. Aborting to avoid creating malformed resource."
        exit 1
    }
    $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
    $PG_ADMIN = "omnivecadmin"

    # Check if server already exists
    $showArgs = @('postgres','flexible-server','show','--name',$PG_SERVER,'--resource-group',$RESOURCE_GROUP,'-o','json')
    $showRaw = & $AzExe @showArgs 2>$null
    $showText = if ($null -ne $showRaw) { ($showRaw | Out-String).Trim() } else { '' }
    $existing = $null
    if ($showText -and $showText.StartsWith('{')) {
        try { $existing = $showText | ConvertFrom-Json -ErrorAction Stop } catch { $existing = $null }
    }
    if ($existing) {
        LogOk "PostgreSQL server already exists: $PG_SERVER"
        $PG_LOCATION = $existing.location
    } else {
        # Pick a location for the flex server. Not every region allows flex
        # server provisioning for every subscription, so we try the RG's
        # location first, then fall back through a safe list.
        $rgLoc = (& $AzExe group show --name $RESOURCE_GROUP --query location -o tsv 2>$null)
        if ($rgLoc) { $rgLoc = "$rgLoc".Trim() }
        $candidates = @()
        if ($rgLoc) { $candidates += $rgLoc }
        foreach ($fb in @('eastus','centralus','westus3','westus2','northeurope','westeurope')) {
            if ($candidates -notcontains $fb) { $candidates += $fb }
        }

        $created = $false
        foreach ($loc in $candidates) {
            Log "  Creating server: $PG_SERVER in $loc (this takes ~3-5 minutes)..."
            $out = & $AzExe postgres flexible-server create `
                --name $PG_SERVER `
                --resource-group $RESOURCE_GROUP `
                --location $loc `
                --admin-user $PG_ADMIN `
                --admin-password $PgAdminPassword `
                --sku-name Standard_B1ms `
                --tier Burstable `
                --storage-size 32 `
                --version 16 `
                --public-access 0.0.0.0 `
                --yes 2>&1
            if ($LASTEXITCODE -eq 0) {
                $PG_LOCATION = $loc
                $created = $true
                LogOk "PostgreSQL server created: $PG_SERVER (location=$loc)"
                break
            }
            $outStr = "$out"
            if ($outStr -match 'location is restricted' -or $outStr -match 'not available in the region' -or $outStr -match 'SubscriptionIsRestrictedForLocation') {
                LogWarn "  Location '$loc' restricted for PG flex server. Trying next..."
                continue
            }
            LogErr "PG server create failed (exit $LASTEXITCODE): $outStr"
            throw "PostgreSQL server creation failed"
        }
        if (-not $created) {
            LogErr "Could not find an allowed region for PG flex server. Tried: $($candidates -join ', ')"
            throw "No allowed region for PG flex server"
        }
    }

    # Enable pgvector extension
    Log "  Enabling pgvector extension..."
    $extOut = & $AzExe postgres flexible-server parameter set `
        --server-name $PG_SERVER `
        --resource-group $RESOURCE_GROUP `
        --name azure.extensions `
        --value VECTOR 2>&1
    if ($LASTEXITCODE -ne 0) {
        LogErr "Failed to enable azure.extensions=VECTOR: $extOut"
        throw "pgvector enable failed"
    }
    LogOk "pgvector extension enabled."

    # Get connection info
    $PG_HOST = "$PG_SERVER.postgres.database.azure.com"
    $PG_PORT = 5432
    $PG_DB = "omnivec_demo"

    # Allow Azure services
    & $AzExe postgres flexible-server firewall-rule create `
        --name $PG_SERVER `
        --resource-group $RESOURCE_GROUP `
        --rule-name AllowAzure `
        --start-ip-address 0.0.0.0 `
        --end-ip-address 0.0.0.0 2>$null | Out-Null

    # Allow current IP
    $myIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 5)
    & $AzExe postgres flexible-server firewall-rule create `
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
        if (-not $INSTANCE_TOKEN -or $INSTANCE_TOKEN.Trim() -eq '') {
            LogErr "Empty instance token derived from AKS cluster '$AKS_CLUSTER'. Aborting."
            exit 1
        }
        $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
        $PG_HOST = "$PG_SERVER.postgres.database.azure.com"
        $PG_PORT = 5432
        $PG_DB = "omnivec_demo"
        $PG_ADMIN = "omnivecadmin"
    }

    # Create database (via az; no local psql needed)
    Log "  Creating database: $PG_DB"
    Invoke-PgSql -PgHost $PG_HOST -AdminUser $PG_ADMIN -AdminPassword $PgAdminPassword `
        -Database 'postgres' -Query "CREATE DATABASE $PG_DB;" | Out-Null
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
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Destination table (vector embeddings)
CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    content TEXT,
    embedding vector($AoaiDims),
    metadata JSONB,
    pipeline_id TEXT,
    embedded_at TIMESTAMPTZ,
    source_ref TEXT,
    content_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS embeddings_vector_idx ON embeddings USING hnsw (embedding vector_cosine_ops);
"@
    Invoke-PgSql -PgHost $PG_HOST -AdminUser $PG_ADMIN -AdminPassword $PgAdminPassword `
        -Database $PG_DB -Query $sql | Out-Null
    LogOk "Tables created: documents (source), embeddings (destination)"

    # Insert sample documents
    Log "  Inserting sample documents..."
    $docs = @(
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-001', 'Azure Cosmos DB', 'Azure Cosmos DB is a globally distributed multi-model database service providing turnkey global distribution with elastic scaling.', 'database') ON CONFLICT (id) DO NOTHING;",
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-002', 'Azure Kubernetes Service', 'AKS simplifies deploying managed Kubernetes clusters in Azure by offloading operational overhead.', 'compute') ON CONFLICT (id) DO NOTHING;",
        "INSERT INTO documents (id, title, content, category) VALUES ('doc-003', 'Azure Blob Storage', 'Azure Blob Storage stores massive amounts of unstructured data like documents and images optimized for cloud scale.', 'storage') ON CONFLICT (id) DO NOTHING;"
    )
    foreach ($doc in $docs) {
        Invoke-PgSql -PgHost $PG_HOST -AdminUser $PG_ADMIN -AdminPassword $PgAdminPassword `
            -Database $PG_DB -Query $doc | Out-Null
    }
    LogOk "Inserted 3 sample documents."

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
    $srcResp = ApiCreateOrGet -CollectionPath "/api/sources" -ListField "sources" -Name "pg-demo-source" -Body $srcBody
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
    $dstResp = ApiCreateOrGet -CollectionPath "/api/destinations" -ListField "destinations" -Name "pg-demo-vectors" -Body $dstBody
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
    try {
        $pipResp = ApiPost "/api/pipelines" $pipBody
        $PIP_ID = $pipResp.pipeline.id
        if (-not $PIP_ID) { $PIP_ID = $pipResp.id }
        LogOk "Pipeline created: $PIP_ID"
    } catch {
        if ("$_" -match 'already exists' -or "$_" -match '409') {
            $existingPip = (ApiGet "/api/pipelines").pipelines | Where-Object { $_.name -eq "pgvector-demo-pipeline" } | Select-Object -First 1
            if ($existingPip) {
                $PIP_ID = $existingPip.id
                LogOk "Reusing existing pipeline: $PIP_ID"
            } else { throw }
        } else { throw }
    }

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

    if (-not $PG_SERVER) {
        $INSTANCE_TOKEN = ($AKS_CLUSTER -replace 'omnivec-aks-','')
        if (-not $INSTANCE_TOKEN -or $INSTANCE_TOKEN.Trim() -eq '') {
            LogErr "Empty instance token derived from AKS cluster '$AKS_CLUSTER'. Aborting."
            exit 1
        }
        $PG_SERVER = "omnivec-pgdemo-$INSTANCE_TOKEN"
        $PG_DB = "omnivec_demo"
        $PG_ADMIN = "omnivecadmin"
    }

    $rawCount = Invoke-PgSql -PgHost $PG_HOST -AdminUser $PG_ADMIN -AdminPassword $PgAdminPassword `
        -Database $PG_DB -Query "SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL;"
    $count = if ($rawCount) { ([regex]::Match("$rawCount", '\d+')).Value } else { '0' }
    if (-not $count) { $count = '0' }

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
