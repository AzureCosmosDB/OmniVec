# OmniVec — preprovision hook (PowerShell)
# Validates prerequisites, checks for existing installations, and collects config choices

$ErrorActionPreference = "Stop"

Write-Host "`n`e[32m+==========================================+`e[0m"
Write-Host "`e[32m|     OmniVec - Pre-provision Checks       |`e[0m"
Write-Host "`e[32m+==========================================+`e[0m"

# -- Deployment lock: prevent concurrent azd up/down for the same env --
$lockDir = Join-Path $HOME ".omnivec" "locks"
if (-not (Test-Path $lockDir)) { New-Item -ItemType Directory -Path $lockDir -Force | Out-Null }
$lockFile = Join-Path $lockDir "$env:AZURE_ENV_NAME.lock"

function Acquire-Lock {
    if (Test-Path $lockFile) {
        $lockContent = Get-Content $lockFile -ErrorAction SilentlyContinue
        $lockPid = $lockContent | Select-Object -First 1
        $lockHost = $lockContent | Select-Object -Last 1

        # Check if the locking process is still alive
        $alive = $false
        if ($lockPid) {
            try {
                $proc = Get-Process -Id ([int]$lockPid) -ErrorAction Stop
                $alive = $true
            } catch {
                $alive = $false
            }
        }

        if ($alive) {
            Write-Host "`n`e[31mERROR: Another deployment for '$env:AZURE_ENV_NAME' is already running (PID $lockPid).`e[0m"
            Write-Host "  If that process is stuck, you can force-take the lock."
            $forceLock = (Read-Host "  Take over lock and continue? [y/N]").Trim()
            if ($forceLock -match "^[yY]") {
                Write-Host "  `e[33mKilling PID $lockPid and taking lock...`e[0m"
                try { Stop-Process -Id ([int]$lockPid) -Force -ErrorAction SilentlyContinue } catch {}
                Start-Sleep -Seconds 2
            } else {
                Write-Host "  `e[31mAborting. Wait for the other deployment to finish or take over the lock.`e[0m"
                exit 1
            }
        } else {
            Write-Host "  `e[33mStale lock found (PID $lockPid is dead). Cleaning up.`e[0m"
        }
    }

    # Write lock: PID on line 1, hostname on line 2
    @($PID, (hostname)) | Set-Content $lockFile
}

function Release-Lock {
    if (Test-Path $lockFile) { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue }
}

Acquire-Lock

# Release lock on exit (success or failure) via try/finally wrapper
try {

# -- Validate required tools --
Write-Host "`n`e[33mChecking prerequisites...`e[0m"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Host "`e[31mMissing required tool: az (Azure CLI). Install from https://aka.ms/install-azure-cli`e[0m"
    exit 1
}
Write-Host "  `e[32maz CLI found.`e[0m"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    Write-Host "  `e[33mkubectl not found - installing via az aks install-cli...`e[0m"
    az aks install-cli 2>$null
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        Write-Host "  `e[31mFailed to install kubectl. Install manually: https://aka.ms/install-kubectl`e[0m"
        exit 1
    }
    Write-Host "  `e[32mkubectl installed.`e[0m"
} else {
    Write-Host "  `e[32mkubectl found.`e[0m"
}

if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
    Write-Host "  `e[33mhelm not found - installing via winget...`e[0m"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install Helm.Helm --silent --accept-package-agreements --accept-source-agreements 2>$null
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
        Write-Host "  `e[31mFailed to install helm. Install manually: https://helm.sh/docs/intro/install/`e[0m"
        exit 1
    }
    Write-Host "  `e[32mhelm installed.`e[0m"
} else {
    Write-Host "  `e[32mhelm found.`e[0m"
}

Write-Host "`e[32mAll prerequisites met.`e[0m"

# Init submodules if needed
if (-not (Test-Path "$PSScriptRoot/../docgrok/Dockerfile")) {
    Write-Host "  `e[33mInitializing git submodules...`e[0m"
    Push-Location "$PSScriptRoot/.."
    git submodule update --init --recursive 2>$null
    Pop-Location
}

# -- Validate Azure login --
Write-Host "`n`e[33mChecking Azure login...`e[0m"
$acct = az account show 2>$null | ConvertFrom-Json
if (-not $acct) {
    Write-Host "`e[31mNot logged into Azure. Run 'az login' first.`e[0m"
    exit 1
}
Write-Host "`e[32mLogged in to subscription: $($acct.name) ($($acct.id))`e[0m"

# -- Check for existing deployment (RG exists = update in-place) --
$rgName = "rg-omnivec-$env:AZURE_ENV_NAME"
$rgExists = az group exists --name $rgName 2>$null
if ("$rgExists".Trim() -eq "true") {
    Write-Host "`n`e[32mExisting deployment detected (RG: $rgName). Importing config...`e[0m"
    $tags = az group show --name $rgName --query "tags" -o json 2>$null | ConvertFrom-Json
    if ($tags) {
        $tagMap = @{
            "omnivec-sys-sku"    = "OMNIVEC_SYSTEM_NODE_VM_SIZE"
            "omnivec-sys-count"  = "OMNIVEC_SYSTEM_NODE_COUNT"
            "omnivec-gpu-sku"    = "OMNIVEC_GPU_NODE_VM_SIZE"
            "omnivec-gpu-count"  = "OMNIVEC_GPU_NODE_COUNT"
            "omnivec-metadata"   = "OMNIVEC_METADATA_STORE"
            "omnivec-blob"       = "OMNIVEC_ENABLE_BLOB_SOURCE"
            "omnivec-build"      = "OMNIVEC_BUILD_MODE"
        }
        foreach ($tag in $tagMap.GetEnumerator()) {
            $val = $tags.PSObject.Properties[$tag.Key].Value
            if ($val) {
                azd env set $tag.Value "$val" 2>$null
                Write-Host "  $($tag.Value) = $val"
            }
        }
    }
    Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
    exit 0
}

# -- Metadata storage selection --
$ErrorActionPreference = "SilentlyContinue"
$curMeta = azd env get-value OMNIVEC_METADATA_STORE 2>$null
$metaExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$defMeta = if ($metaExitCode -eq 0 -and $curMeta -and "$curMeta" -notmatch "ERROR") { "$curMeta".Trim() } else { "cosmosdb-serverless" }
$defMetaNum = if ($defMeta -eq "cosmosdb-provisioned") { "2" } else { "1" }
$mark1 = if ($defMetaNum -eq "1") { " (current)" } else { "" }
$mark2 = if ($defMetaNum -eq "2") { " (current)" } else { "" }
Write-Host ""
Write-Host "`e[33mSelect metadata storage backend:`e[0m"
Write-Host "  1) Azure CosmosDB (Serverless NoSQL)$mark1"
Write-Host "  2) Azure CosmosDB (Provisioned throughput)$mark2"
Write-Host ""
$metaChoice = (Read-Host "Choice [$defMetaNum]").Trim()
if (-not $metaChoice) { $metaChoice = $defMetaNum }

switch ($metaChoice) {
    "2" {
        Write-Host "`e[32mUsing CosmosDB Provisioned for metadata storage.`e[0m"
        azd env set OMNIVEC_METADATA_STORE "cosmosdb-provisioned"
    }
    default {
        Write-Host "`e[32mUsing CosmosDB Serverless for metadata storage.`e[0m"
        azd env set OMNIVEC_METADATA_STORE "cosmosdb-serverless"
    }
}

# -- Blob storage source --
$ErrorActionPreference = "SilentlyContinue"
$curBlob = azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>$null
$blobExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$defBlob = if ($blobExitCode -eq 0 -and $curBlob -and "$curBlob" -notmatch "ERROR") { "$curBlob".Trim() } else { "true" }
$defBlobNum = if ($defBlob -eq "false") { "2" } else { "1" }
$bmark1 = if ($defBlobNum -eq "1") { " (current)" } else { "" }
$bmark2 = if ($defBlobNum -eq "2") { " (current)" } else { "" }
Write-Host ""
Write-Host "`e[33mWill you use Azure Blob Storage as a document source?`e[0m"
Write-Host "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
Write-Host "  will be created alongside the Storage Account."
Write-Host ""
Write-Host "  1) Yes - enable blob source ingestion$bmark1"
Write-Host "  2) No  - CosmosDB sources only (skip Service Bus + Event Grid)$bmark2"
Write-Host ""
$blobChoice = (Read-Host "Choice [$defBlobNum]").Trim()
if (-not $blobChoice) { $blobChoice = $defBlobNum }

if ($blobChoice -eq "1") {
    Write-Host "`e[32mBlob source enabled - will create Storage Account, Service Bus, and Event Grid.`e[0m"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
} else {
    Write-Host "`e[32mBlob source disabled - skipping Service Bus and Event Grid.`e[0m"
    azd env set OMNIVEC_ENABLE_BLOB_SOURCE "false"
}

# -- Node provisioning --
Write-Host ""
Write-Host "`e[33mConfigure AKS node pools:`e[0m"
Write-Host ""

$location = $env:AZURE_LOCATION
if (-not $location) { $location = "centralus" }

# Helper: validate a single SKU in the location
function Test-SkuAvailable {
    param($Sku, $Location)
    $result = az vm list-skus --location $Location --size $Sku --resource-type virtualMachines --query "[?name=='$Sku' && (restrictions==null || restrictions[0]==null)].name" -o tsv 2>$null
    return ($result -and $result.Trim() -eq $Sku)
}

# -- System node pool --
Write-Host "`e[36mSystem node pool (API, controller, worker, changefeed):`e[0m"
$ErrorActionPreference = "SilentlyContinue"
$curSysSku = $null
try { $curSysSku = azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>$null } catch {}
$sysSkuExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($curSysSku -and $sysSkuExitCode -eq 0 -and "$curSysSku" -notmatch "ERROR") {
    $curSysSku = $curSysSku.Trim()
} else {
    $curSysSku = ""
}

$sysCandidates = @(
    @{ name = "Standard_D4s_v3";  desc = "4 vCPU, 16 GB" },
    @{ name = "Standard_D4ds_v5"; desc = "4 vCPU, 16 GB (v5)" },
    @{ name = "Standard_D8s_v3";  desc = "8 vCPU, 32 GB" },
    @{ name = "Standard_B4ms";    desc = "4 vCPU, 16 GB (burstable)" },
    @{ name = "Standard_D2s_v3";  desc = "2 vCPU, 8 GB (dev)" }
)
$defIdx = 0
for ($i = 0; $i -lt $sysCandidates.Count; $i++) {
    if ($curSysSku -and $sysCandidates[$i].name -eq $curSysSku) { $defIdx = $i }
}

$SYS_SKU = $null
$failedSkus = @{}
while (-not $SYS_SKU) {
    # Re-display list with failed SKUs marked
    Write-Host "  Common options:"
    $nextDefault = $null
    for ($i = 0; $i -lt $sysCandidates.Count; $i++) {
        $mark = ""
        if ($curSysSku -and $sysCandidates[$i].name -eq $curSysSku) { $mark = " (current)" }
        if ($failedSkus.ContainsKey($sysCandidates[$i].name)) { $mark = " `e[31m[✗ unavailable]`e[0m" }
        elseif (-not $nextDefault) { $nextDefault = $($i+1) }
        Write-Host "    $($i+1)) $($sysCandidates[$i].name) - $($sysCandidates[$i].desc)$mark"
    }
    Write-Host "    $($sysCandidates.Count+1)) Enter custom SKU"
    Write-Host ""
    if (-not $nextDefault) { $nextDefault = $($defIdx+1) }

    $sysPick = (Read-Host "  System VM SKU [$nextDefault]").Trim()
    if (-not $sysPick) { $sysPick = "$nextDefault" }

    if ([int]$sysPick -eq ($sysCandidates.Count + 1)) {
        $defManual = if ($curSysSku) { $curSysSku } else { "Standard_D4s_v3" }
        $candidate = (Read-Host "  Enter SKU name [$defManual]").Trim()
        if (-not $candidate) { $candidate = $defManual }
    } else {
        $idx = [int]$sysPick - 1
        if ($idx -ge 0 -and $idx -lt $sysCandidates.Count) {
            $candidate = $sysCandidates[$idx].name
        } else {
            $candidate = $sysCandidates[[int]$nextDefault - 1].name
        }
    }

    if ($failedSkus.ContainsKey($candidate)) {
        Write-Host "  `e[31m$candidate already checked — not available. Pick another.`e[0m"
        continue
    }

    Write-Host "  `e[36mValidating $candidate in $location...`e[0m" -NoNewline
    if (Test-SkuAvailable -Sku $candidate -Location $location) {
        Write-Host " `e[32m✓ available`e[0m"
        $SYS_SKU = $candidate
    } else {
        Write-Host " `e[31m✗ not available in $location`e[0m"
        $failedSkus[$candidate] = $true
    }
}
Write-Host "  `e[32mSystem VM SKU: $SYS_SKU`e[0m"

$ErrorActionPreference = "SilentlyContinue"
$curSysCount = azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>$null
$sysExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$defSysCount = if ($sysExitCode -eq 0 -and $curSysCount -and "$curSysCount".Trim()) { "$curSysCount".Trim() } else { "2" }
$sysCount = (Read-Host "  System node count [$defSysCount]").Trim()
if (-not $sysCount) { $sysCount = $defSysCount }
Write-Host "  `e[32mSystem nodes: $sysCount`e[0m"

Write-Host ""

# -- GPU node pool --
Write-Host "`e[36mGPU node pool (ML models - dse-qwen2, clip, bge, bge-small):`e[0m"
Write-Host "  Enter 0 nodes to skip GPU pool (use external models only)."
$ErrorActionPreference = "SilentlyContinue"
$curGpuSku = $null
try { $curGpuSku = azd env get-value OMNIVEC_GPU_NODE_VM_SIZE 2>$null } catch {}
$gpuSkuExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($curGpuSku -and $gpuSkuExitCode -eq 0 -and "$curGpuSku" -notmatch "ERROR") {
    $curGpuSku = $curGpuSku.Trim()
} else {
    $curGpuSku = ""
}

$ErrorActionPreference = "SilentlyContinue"
$curGpuCount = azd env get-value OMNIVEC_GPU_NODE_COUNT 2>$null
$gpuCountExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$defGpuCount = if ($gpuCountExitCode -eq 0 -and $curGpuCount -and "$curGpuCount".Trim()) { "$curGpuCount".Trim() } else { "0" }

$gpuCount = (Read-Host "  GPU node count (0 = no GPU pool) [$defGpuCount]").Trim()
if (-not $gpuCount) { $gpuCount = $defGpuCount }

if ($gpuCount -ne "0") {
    $gpuCandidates = @(
        @{ name = "Standard_NC4as_T4_v3";     desc = "4 vCPU, 28 GB, 1x T4 16GB" },
        @{ name = "Standard_NC6s_v3";          desc = "6 vCPU, 112 GB, 1x V100 16GB" },
        @{ name = "Standard_NC8as_T4_v3";      desc = "8 vCPU, 56 GB, 1x T4 16GB" },
        @{ name = "Standard_NC12s_v3";         desc = "12 vCPU, 224 GB, 2x V100" },
        @{ name = "Standard_NC24ads_A100_v4";  desc = "24 vCPU, 220 GB, 1x A100 80GB" }
    )
    $defGpuIdx = 0
    for ($i = 0; $i -lt $gpuCandidates.Count; $i++) {
        if ($curGpuSku -and $gpuCandidates[$i].name -eq $curGpuSku) { $defGpuIdx = $i }
    }

    $GPU_SKU = $null
    $failedGpuSkus = @{}
    while (-not $GPU_SKU) {
        Write-Host "  Common GPU options:"
        $nextGpuDefault = $null
        for ($i = 0; $i -lt $gpuCandidates.Count; $i++) {
            $mark = ""
            if ($curGpuSku -and $gpuCandidates[$i].name -eq $curGpuSku) { $mark = " (current)" }
            if ($failedGpuSkus.ContainsKey($gpuCandidates[$i].name)) { $mark = " `e[31m[✗ unavailable]`e[0m" }
            elseif (-not $nextGpuDefault) { $nextGpuDefault = $($i+1) }
            Write-Host "    $($i+1)) $($gpuCandidates[$i].name) - $($gpuCandidates[$i].desc)$mark"
        }
        Write-Host "    $($gpuCandidates.Count+1)) Enter custom SKU"
        Write-Host ""
        if (-not $nextGpuDefault) { $nextGpuDefault = $($defGpuIdx+1) }

        $gpuPick = (Read-Host "  GPU VM SKU [$nextGpuDefault]").Trim()
        if (-not $gpuPick) { $gpuPick = "$nextGpuDefault" }

        if ([int]$gpuPick -eq ($gpuCandidates.Count + 1)) {
            $defGpuManual = if ($curGpuSku) { $curGpuSku } else { "Standard_NC4as_T4_v3" }
            $candidate = (Read-Host "  Enter SKU name [$defGpuManual]").Trim()
            if (-not $candidate) { $candidate = $defGpuManual }
        } else {
            $idx = [int]$gpuPick - 1
            if ($idx -ge 0 -and $idx -lt $gpuCandidates.Count) {
                $candidate = $gpuCandidates[$idx].name
            } else {
                $candidate = $gpuCandidates[[int]$nextGpuDefault - 1].name
            }
        }

        if ($failedGpuSkus.ContainsKey($candidate)) {
            Write-Host "  `e[31m$candidate already checked — not available. Pick another.`e[0m"
            continue
        }

        Write-Host "  `e[36mValidating $candidate in $location...`e[0m" -NoNewline
        if (Test-SkuAvailable -Sku $candidate -Location $location) {
            Write-Host " `e[32m✓ available`e[0m"
            $GPU_SKU = $candidate
        } else {
            Write-Host " `e[31m✗ not available in $location`e[0m"
            $failedGpuSkus[$candidate] = $true
        }
    }
    Write-Host "  `e[32mGPU VM: $GPU_SKU, nodes: $gpuCount`e[0m"
} else {
    Write-Host "  `e[33mGPU pool disabled - using external embedding models only.`e[0m"
    $GPU_SKU = if ($curGpuSku) { $curGpuSku } else { "" }
}

# Validate before storing
if (-not $SYS_SKU) {
    Write-Host "`e[31mNo system VM SKU selected. Cannot proceed.`e[0m"
    exit 1
}

# Store in azd env
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE $SYS_SKU
azd env set OMNIVEC_SYSTEM_NODE_COUNT $sysCount
azd env set OMNIVEC_GPU_NODE_VM_SIZE $GPU_SKU
azd env set OMNIVEC_GPU_NODE_COUNT $gpuCount

# -- Check image build capability --
Write-Host "`n`e[33mChecking image build capability...`e[0m"
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $dockerInfo = docker info 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`e[32mDocker daemon available - will use local builds.`e[0m"
        azd env set OMNIVEC_BUILD_MODE "docker"
    } else {
        Write-Host "`e[33mNo Docker daemon - will use 'az acr build' for remote builds.`e[0m"
        azd env set OMNIVEC_BUILD_MODE "acr"
    }
} else {
    Write-Host "`e[33mNo Docker daemon - will use 'az acr build' for remote builds.`e[0m"
    azd env set OMNIVEC_BUILD_MODE "acr"
}

# -- Sanitize env values: strip BOM, tabs, carriage returns --
Write-Host "`n`e[36mSanitizing environment values...`e[0m"
$envKeys = @("OMNIVEC_SYSTEM_NODE_VM_SIZE", "OMNIVEC_SYSTEM_NODE_COUNT", "OMNIVEC_GPU_NODE_VM_SIZE", "OMNIVEC_GPU_NODE_COUNT", "OMNIVEC_ENABLE_BLOB_SOURCE", "OMNIVEC_METADATA_STORE", "OMNIVEC_BUILD_MODE")
foreach ($key in $envKeys) {
    $ErrorActionPreference = "SilentlyContinue"
    $raw = azd env get-value $key 2>$null
    $ErrorActionPreference = "Stop"
    if ($raw) {
        $clean = $raw -replace '[\t\r]','' -replace '^\xEF\xBB\xBF','' -replace '^\s+|\s+$',''
        if ($clean -ne $raw) {
            azd env set $key $clean
            Write-Host "  `e[33mCleaned ${key}: removed hidden characters`e[0m"
        }
    }
}

Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
Write-Host "`e[36mEnvironment: $env:AZURE_ENV_NAME`e[0m"
Write-Host "`e[36mEach installation gets a unique resource token derived from (subscription + resource group + env name).`e[0m"

} finally {
    Release-Lock
}
