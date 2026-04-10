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
            $forceLock = Read-Host "  Take over lock and continue? [y/N]"
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

# -- Check for existing healthy deployment --
$ErrorActionPreference = "SilentlyContinue"
$existingAks = azd env get-value AZURE_AKS_CLUSTER_NAME 2>$null
$existingRg = azd env get-value AZURE_RESOURCE_GROUP 2>$null
$ErrorActionPreference = "Stop"

if ($existingAks -and $existingRg -and $existingAks -notmatch "^ERROR" -and $existingRg -notmatch "^ERROR") {
    $kubeCtx = $existingAks.Trim()
    az aks get-credentials --resource-group $existingRg.Trim() --name $kubeCtx --context $kubeCtx --overwrite-existing 2>$null | Out-Null

    $healthyPods = 0
    try {
        $podLines = kubectl --context $kubeCtx get pods -n omnivec --field-selector=status.phase=Running --no-headers 2>$null
        if ($podLines) { $healthyPods = @($podLines | Where-Object { $_ }).Count }
    } catch {}

    if ($healthyPods -gt 0) {
        Write-Host "`n`e[33mExisting healthy deployment detected ($healthyPods running pods in omnivec).`e[0m"
        Write-Host "  AKS:  `e[36m$kubeCtx`e[0m"
        Write-Host "  RG:   `e[36m$($existingRg.Trim())`e[0m"
        Write-Host "  `e[32mReusing existing resources and proceeding with in-place update.`e[0m"
    }
}

# -- Resume detection --
$existingConfig = $null
$ErrorActionPreference = "SilentlyContinue"
$existingConfig = azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>$null
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -eq 0 -and $existingConfig -and $existingConfig -notmatch "^ERROR") {
    Write-Host "`n`e[36mFound previous configuration for environment '$env:AZURE_ENV_NAME':`e[0m"
    Write-Host "  System SKU:      $(azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>$null)"
    Write-Host "  System nodes:    $(azd env get-value OMNIVEC_SYSTEM_NODE_COUNT 2>$null)"
    Write-Host "  GPU SKU:         $(azd env get-value OMNIVEC_GPU_NODE_VM_SIZE 2>$null)"
    Write-Host "  GPU nodes:       $(azd env get-value OMNIVEC_GPU_NODE_COUNT 2>$null)"
    Write-Host "  Blob source:     $(azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>$null)"
    Write-Host "  Metadata store:  $(azd env get-value OMNIVEC_METADATA_STORE 2>$null)"
    Write-Host ""
    $reuse = Read-Host "  Keep these settings? [Y/n] (n = reconfigure from scratch)"
    if (-not $reuse) { $reuse = "Y" }
    if ($reuse -match "^[nN]") {
        Write-Host "  `e[32mReconfiguring...`e[0m"
    } else {
        Write-Host "  `e[32mUsing existing settings, skipping configuration prompts.`e[0m"
        exit 0
    }
}

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

# -- Check for existing OmniVec installations --
Write-Host "`n`e[33mChecking for existing OmniVec installations in subscription...`e[0m"

$existing = az resource list --query "[?tags.""omnivec-instance"" != null].{name:name, type:type, rg:resourceGroup, instance:tags.""omnivec-instance""}" -o json 2>$null | ConvertFrom-Json
if ($existing -and $existing.Count -gt 0) {
    $instances = $existing | Group-Object -Property instance
    Write-Host "`e[36mFound $($instances.Count) existing OmniVec installation(s):`e[0m"
    Write-Host ""
    foreach ($inst in $instances) {
        $rg = $inst.Group[0].rg
        $types = ($inst.Group | ForEach-Object { $_.type.Split('/')[-1] } | Sort-Object -Unique) -join ", "
        Write-Host "  `e[36m$($inst.Name)`e[0m  (rg: $rg, $($inst.Count) resources ($types))"
    }
    Write-Host ""
    Write-Host "`e[33mWhat would you like to do?`e[0m"
    Write-Host "  1) Launch a NEW OmniVec installation (unique resources alongside existing)"
    Write-Host "  2) Cancel deployment"
    Write-Host ""
    $choice = Read-Host "Choice [1/2]"
    if ($choice -eq "2") {
        Write-Host "`e[31mDeployment cancelled.`e[0m"
        exit 1
    }
    Write-Host "`e[32mCreating new installation with environment '$env:AZURE_ENV_NAME'.`e[0m"
} else {
    Write-Host "`e[32mNo existing OmniVec installations found. This will be a fresh deployment.`e[0m"
}

# -- Metadata storage selection --
Write-Host ""
Write-Host "`e[33mSelect metadata storage backend:`e[0m"
Write-Host "  1) Azure CosmosDB (Serverless NoSQL) - recommended"
Write-Host "  2) Azure CosmosDB (Provisioned throughput)"
Write-Host ""
$metaChoice = Read-Host "Choice [1]"
if (-not $metaChoice) { $metaChoice = "1" }

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
Write-Host ""
Write-Host "`e[33mWill you use Azure Blob Storage as a document source?`e[0m"
Write-Host "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
Write-Host "  will be created alongside the Storage Account."
Write-Host ""
Write-Host "  1) Yes - enable blob source ingestion (recommended)"
Write-Host "  2) No  - CosmosDB sources only (skip Service Bus + Event Grid)"
Write-Host ""
$blobChoice = Read-Host "Choice [1]"
if (-not $blobChoice) { $blobChoice = "1" }

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

# Query available VM SKUs in the selected location (parallel queries)
$location = $env:AZURE_LOCATION
if (-not $location) { $location = "centralus" }
Write-Host "`e[33mChecking VM SKU availability in $location...`e[0m"

# Run both SKU queries in parallel
$sysJob = Start-Job -ScriptBlock {
    az vm list-skus --location $args[0] --size Standard_D --resource-type virtualMachines --query "[?(restrictions==null || restrictions[0]==null)].name" -o json 2>$null
} -ArgumentList $location
$gpuJob = Start-Job -ScriptBlock {
    az vm list-skus --location $args[0] --size Standard_NC --resource-type virtualMachines --query "[?(restrictions==null || restrictions[0]==null)].name" -o json 2>$null
} -ArgumentList $location

# Wait for both to finish
$null = Wait-Job $sysJob, $gpuJob
$sysJson = Receive-Job $sysJob
$gpuJson = Receive-Job $gpuJob
Remove-Job $sysJob, $gpuJob

$availableRaw = @()
if ($sysJson) { $availableRaw += ($sysJson | ConvertFrom-Json) }
if ($gpuJson) { $availableRaw += ($gpuJson | ConvertFrom-Json) }
if (-not $availableRaw) { $availableRaw = @() }

# System node candidates
$sysCandidates = @(
    @{ name = "Standard_D4s_v3";  desc = "4 vCPU, 16 GB RAM" },
    @{ name = "Standard_D4ds_v5"; desc = "4 vCPU, 16 GB RAM (v5)" },
    @{ name = "Standard_D8s_v3";  desc = "8 vCPU, 32 GB RAM" },
    @{ name = "Standard_D8ds_v5"; desc = "8 vCPU, 32 GB RAM (v5)" },
    @{ name = "Standard_D2s_v3";  desc = "2 vCPU, 8 GB RAM (dev)" },
    @{ name = "Standard_D2ds_v5"; desc = "2 vCPU, 8 GB RAM (dev, v5)" }
)
$availableSys = @()
foreach ($c in $sysCandidates) {
    if ($availableRaw -contains $c.name) {
        $availableSys += $c
    }
}

Write-Host "`e[36mSystem node pool (API, controller, worker, changefeed):`e[0m"
if ($availableSys.Count -eq 0) {
    Write-Host "  `e[31mNo suitable system VM SKUs found in $location!`e[0m"
    $SYS_SKU = Read-Host "  Enter a VM SKU manually"
} else {
    Write-Host "  Available VM SKUs:"
    for ($i = 0; $i -lt $availableSys.Count; $i++) {
        $rec = if ($i -eq 0) { " (recommended)" } else { "" }
        Write-Host "    $($i+1)) $($availableSys[$i].name) - $($availableSys[$i].desc)$rec"
    }
    Write-Host ""
    $sysSku = Read-Host "  System VM SKU [1]"
    if (-not $sysSku) { $sysSku = "1" }
    $idx = [int]$sysSku - 1
    if ($idx -lt 0 -or $idx -ge $availableSys.Count) { $idx = 0 }
    $SYS_SKU = $availableSys[$idx].name
}
Write-Host "  `e[32mSystem VM SKU: $SYS_SKU`e[0m"

$sysCount = Read-Host "  System node count [2]"
if (-not $sysCount) { $sysCount = "2" }
Write-Host "  `e[32mSystem nodes: $sysCount`e[0m"

Write-Host ""

# GPU node candidates
$gpuCandidates = @(
    @{ name = "Standard_NC6s_v3";     desc = "6 vCPU, 112 GB, 1x V100 16GB" },
    @{ name = "Standard_NC12s_v3";    desc = "12 vCPU, 224 GB, 2x V100" },
    @{ name = "Standard_NC4as_T4_v3"; desc = "4 vCPU, 28 GB, 1x T4 16GB" },
    @{ name = "Standard_NC8as_T4_v3"; desc = "8 vCPU, 56 GB, 1x T4 16GB" },
    @{ name = "Standard_NC24ads_A100_v4"; desc = "24 vCPU, 220 GB, 1x A100 80GB" }
)
$availableGpu = @()
foreach ($c in $gpuCandidates) {
    if ($availableRaw -contains $c.name) {
        $availableGpu += $c
    }
}

Write-Host "`e[36mGPU node pool (ML models - dse-qwen2, clip, bge, bge-small):`e[0m"
Write-Host "  Enter 0 nodes to skip GPU pool (use external models only)."
if ($availableGpu.Count -eq 0) {
    Write-Host "  `e[33mNo GPU VM SKUs available in $location. GPU pool will be skipped.`e[0m"
    $GPU_SKU = "Standard_NC6s_v3"
    $gpuCount = "0"
} else {
    Write-Host "  Available GPU SKUs:"
    for ($i = 0; $i -lt $availableGpu.Count; $i++) {
        $rec = if ($i -eq 0) { " (recommended)" } else { "" }
        Write-Host "    $($i+1)) $($availableGpu[$i].name) - $($availableGpu[$i].desc)$rec"
    }
    Write-Host ""
    $gpuSku = Read-Host "  GPU VM SKU [1]"
    if (-not $gpuSku) { $gpuSku = "1" }
    $idx = [int]$gpuSku - 1
    if ($idx -lt 0 -or $idx -ge $availableGpu.Count) { $idx = 0 }
    $GPU_SKU = $availableGpu[$idx].name

    $gpuCount = Read-Host "  GPU node count (0 = no GPU pool) [4]"
    if (-not $gpuCount) { $gpuCount = "4" }
}
if ($gpuCount -eq "0") {
    Write-Host "  `e[33mGPU pool disabled - using external embedding models only.`e[0m"
} else {
    Write-Host "  `e[32mGPU VM: $GPU_SKU, nodes: $gpuCount`e[0m"
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

# -- Check for soft-deleted Key Vault matching THIS environment's resource group --
Write-Host "`n`e[33mChecking for soft-deleted Key Vaults...`e[0m"
$rgName = "rg-omnivec-$($env:AZURE_ENV_NAME)"
$softDeletedVault = $null
$ErrorActionPreference = "SilentlyContinue"
$softDeletedVault = az keyvault list-deleted --query "[?contains(properties.vaultId,'$rgName')].name" -o tsv 2>$null
$ErrorActionPreference = "Stop"
if ($softDeletedVault) {
    $softDeletedVault = "$softDeletedVault".Trim()
    Write-Host "  `e[33mFound soft-deleted vault for this env: $softDeletedVault`e[0m"
    Write-Host "  `e[36mBicep will recover the vault automatically (no purge needed).`e[0m"
    azd env set OMNIVEC_RECOVER_KEYVAULT "true"
} else {
    Write-Host "  `e[32mNo soft-deleted Key Vault for env '$($env:AZURE_ENV_NAME)'.`e[0m"
    azd env set OMNIVEC_RECOVER_KEYVAULT "false"
}

Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
Write-Host "`e[36mEnvironment: $env:AZURE_ENV_NAME`e[0m"
Write-Host "`e[36mEach installation gets a unique resource token derived from (subscription + resource group + env name).`e[0m"

} finally {
    Release-Lock
}
