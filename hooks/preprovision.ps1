# OmniVec - preprovision hook (PowerShell)
# Validates prerequisites, checks for existing installations, and collects config choices

$ErrorActionPreference = "Stop"

Write-Host "`n`e[32m+==========================================+`e[0m"
Write-Host "`e[32m|     OmniVec - Pre-provision Checks       |`e[0m"
Write-Host "`e[32m+==========================================+`e[0m"

# -- Validate AZURE_ENV_NAME (Bicep @maxLength=20) -- fail fast, clear message
$envName = "$env:AZURE_ENV_NAME"
if ([string]::IsNullOrWhiteSpace($envName)) {
    Write-Host "`n`e[31mERROR: AZURE_ENV_NAME is not set.`e[0m"
    Write-Host "  Run: `e[36mazd env new <name>`e[0m (1-20 chars, lowercase alnum+dash)"
    exit 1
}
if ($envName.Length -gt 20) {
    Write-Host "`n`e[31mERROR: AZURE_ENV_NAME='$envName' is $($envName.Length) chars (max 20).`e[0m"
    Write-Host "  Azure resource naming requires environmentName <= 20 chars."
    Write-Host "  Fix with:  `e[36mazd env set AZURE_ENV_NAME <shorter-name>`e[0m"
    Write-Host "  Or create a fresh env:  `e[36mazd env new <shorter-name>`e[0m"
    exit 1
}
if ($envName -notmatch '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$') {
    Write-Host "`n`e[33mWARNING: AZURE_ENV_NAME='$envName' may contain invalid characters.`e[0m"
    Write-Host "  Recommended: lowercase letters, digits, and dashes (no leading/trailing dash)."
}

# -- Block Azure-trademark reserved words. Azure rejects PublicIP DNS labels
# containing these (DomainNameLabelReserved 400), which makes the web LB
# unable to acquire an IP and causes helm --wait to time out. Fail fast
# here so the user picks a clean name instead of debugging a stuck deploy.
$lcEnv = $envName.ToLowerInvariant()
foreach ($_w in @('microsoft','windows','azure','xbox','login','bing','apple')) {
    if ($lcEnv -like "*$_w*") {
        Write-Host "`n`e[31mERROR: AZURE_ENV_NAME='$envName' contains the reserved word '$_w'.`e[0m"
        Write-Host "  Azure rejects PublicIP DNS labels containing trademarks"
        Write-Host "  (microsoft, windows, azure, xbox, login, bing, apple) with"
        Write-Host "  DomainNameLabelReserved (400). Pick a name that doesn't contain any of these."
        Write-Host "  Fix with:  `e[36mazd env new <new-name>`e[0m"
        exit 1
    }
}

# -- Repair .env if prior run left embedded newlines / stray quotes --
# Symptom: `loading .env: unexpected character "\"" in variable name near "...\n"`
# Cause: a previous azd env set wrote a multi-line value; subsequent runs can't parse it.
$envFile = Join-Path (Get-Location) ".azure/$envName/.env"
if (Test-Path $envFile) {
    try {
        $raw = [System.IO.File]::ReadAllText($envFile)
        # Collapse CR+LF variants inside quoted values. A well-formed entry is:
        #   KEY="value"\n  — value itself contains no raw newline.
        # Match KEY="...(possibly with newlines)..." and strip internal CR/LF/TAB.
        $repaired = [regex]::Replace($raw, '(?ms)^([A-Z_][A-Z0-9_]*)="([^"]*)"', {
            param($m)
            $k = $m.Groups[1].Value
            $v = ($m.Groups[2].Value -replace '[\r\n\t]+', '').Trim()
            "${k}=`"${v}`""
        })
        if ($repaired -ne $raw) {
            [System.IO.File]::WriteAllText($envFile, $repaired)
            Write-Host "`e[33mRepaired corrupt .env (stripped embedded whitespace from values).`e[0m"
        }
    } catch {
        Write-Host "`e[33mNote: could not pre-scan .env ($_). Continuing.`e[0m"
    }
}

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
            $forceLock = Read-InputSafely -Prompt "  Take over lock and continue? [y/N]" -Default 'n'
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

# -- a1/a3: Interactive-safety helpers (mirror of hooks/preprovision.sh) ----
function Test-CanPrompt {
    if ($env:OMNIVEC_FORCE_NO_TTY) { return $false }
    try {
        if ([Console]::IsInputRedirected) { return $false }
    } catch {}
    try {
        return [Environment]::UserInteractive
    } catch { return $false }
}

function Test-IsNonInteractive {
    foreach ($v in 'OMNIVEC_NONINTERACTIVE','AZD_NONINTERACTIVE','CI','GITHUB_ACTIONS') {
        $val = [Environment]::GetEnvironmentVariable($v)
        if ($val) { return $true }
    }
    return $false
}

function Read-InputSafely {
    param([string]$Prompt, [string]$Default = '', [int]$TimeoutSec = 0)
    if (-not (Test-CanPrompt)) { return $Default }
    # Windows PowerShell has no built-in Read-Host timeout; the 30s variant
    # below is only used when TimeoutSec>0 AND host supports RawUI.
    if ($TimeoutSec -gt 0 -and $Host.UI.RawUI) {
        try {
            [Console]::Write($Prompt)
            $sb = New-Object System.Text.StringBuilder
            $deadline = (Get-Date).AddSeconds($TimeoutSec)
            while ((Get-Date) -lt $deadline) {
                if ([Console]::KeyAvailable) {
                    $k = [Console]::ReadKey($true)
                    if ($k.Key -eq [ConsoleKey]::Enter) { [Console]::WriteLine(); break }
                    if ($k.Key -eq [ConsoleKey]::Backspace) {
                        if ($sb.Length -gt 0) { $sb.Length -= 1; [Console]::Write("`b `b") }
                        continue
                    }
                    [void]$sb.Append($k.KeyChar)
                    [Console]::Write($k.KeyChar)
                }
                Start-Sleep -Milliseconds 50
            }
            if ((Get-Date) -ge $deadline) {
                [Console]::WriteLine()
                Write-Host "  [timeout after ${TimeoutSec}s - using default: $Default]" -ForegroundColor Yellow
                return $Default
            }
            $val = $sb.ToString().Trim()
            if (-not $val) { return $Default }
            return $val
        } catch {
            # Fall back to plain Read-Host
        }
    }
    try {
        $val = (Read-Host $Prompt).Trim()
        if (-not $val) { return $Default }
        return $val
    } catch {
        return $Default
    }
}

function Use-QuickstartDefaults {
    Write-Host "  `e[32mApplying Quick-start defaults (non-interactive mode).`e[0m"
    $defaults = @{
        'OMNIVEC_SYSTEM_NODE_VM_SIZE' = 'Standard_B4ms'
        'OMNIVEC_SYSTEM_NODE_COUNT'   = '2'
        'OMNIVEC_GPU_NODE_VM_SIZE'    = ''
        'OMNIVEC_GPU_NODE_COUNT'      = '0'
        'OMNIVEC_METADATA_STORE'      = 'cosmosdb-serverless'
        'OMNIVEC_ENABLE_BLOB_SOURCE'  = 'true'
    }
    foreach ($kv in $defaults.GetEnumerator()) {
        azd env set $kv.Key $kv.Value 2>$null
    }
}

function Require-InteractiveOrPreset {
    if (Test-CanPrompt) { return }
    if (Test-IsNonInteractive) {
        Use-QuickstartDefaults
        Write-Host "`n`e[32mPre-provision checks passed (non-interactive). Proceeding with Bicep deployment...`e[0m"
        Release-Lock
        exit 0
    }
    Write-Host "`n`e[31mERROR: No interactive console and no configuration found.`e[0m"
    Write-Host "  azd hooks are running without an interactive terminal (common in CI,"
    Write-Host "  piped shells, or redirected stdin), and no config has been pre-set."
    Write-Host ""
    Write-Host "  Fix with ONE of:"
    Write-Host "    1) Run from a real terminal:  azd up"
    Write-Host "    2) Accept defaults:           `$env:OMNIVEC_NONINTERACTIVE=1; azd up"
    Write-Host "    3) Pre-set config, e.g.:"
    Write-Host "         azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE Standard_B4ms"
    Write-Host "         azd env set OMNIVEC_SYSTEM_NODE_COUNT 2"
    Write-Host "         azd env set OMNIVEC_GPU_NODE_COUNT 0"
    Write-Host "         azd env set OMNIVEC_ENABLE_BLOB_SOURCE true"
    Write-Host "         azd env set OMNIVEC_METADATA_STORE cosmosdb-serverless"
    Release-Lock
    exit 1
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

    # Snapshot user's blob-source override BEFORE tag import overwrites azd env.
    $_userBlobOverride = (& azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>$null)
    if (-not $_userBlobOverride -or "$_userBlobOverride" -match "ERROR") {
        $_userBlobOverride = (& azd env get-value AZURE_ENABLE_BLOB_SOURCE 2>$null)
    }
    if ("$_userBlobOverride" -match "ERROR") { $_userBlobOverride = "" }
    $_userBlobOverride = "$_userBlobOverride".Trim().ToLower()

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
            # Honor user-intended blob-source flip over stale tag.
            if ($tag.Value -eq "OMNIVEC_ENABLE_BLOB_SOURCE" -and $_userBlobOverride -and $_userBlobOverride -ne ("$val".Trim().ToLower())) {
                if ($_userBlobOverride -eq "false" -and ("$val".Trim().ToLower()) -eq "true") {
                    Write-Host "`n  `e[31m✗ Cannot disable OMNIVEC_ENABLE_BLOB_SOURCE (true -> false) on existing deployment.`e[0m"
                    Write-Host "  `e[33mThis would orphan Storage/ServiceBus/EventGrid resources.`e[0m"
                    Write-Host "  `e[33mRun 'azd down' first, or pick a different AZURE_ENV_NAME.`e[0m"
                    exit 1
                }
                if ($_userBlobOverride -eq "true" -and ("$val".Trim().ToLower()) -eq "false") {
                    Write-Host "  `e[33m! Enabling blob source on existing deployment (false -> true). Storage/ServiceBus/EventGrid will be created.`e[0m"
                }
                azd env set OMNIVEC_ENABLE_BLOB_SOURCE $_userBlobOverride 2>$null
                Write-Host "  OMNIVEC_ENABLE_BLOB_SOURCE = $_userBlobOverride `e[33m(user override; tag was '$val')`e[0m"
                continue
            }
            if ($val) {
                azd env set $tag.Value "$val" 2>$null
                Write-Host "  $($tag.Value) = $val"
            }
        }
    }
    Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
    exit 0
}

# -- Config already set (e.g. via azd env set before azd up) - skip prompts --
$ErrorActionPreference = "SilentlyContinue"
$_existingVm = azd env get-value OMNIVEC_SYSTEM_NODE_VM_SIZE 2>$null
$_vmExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($_vmExit -eq 0 -and $_existingVm -and "$_existingVm" -notmatch "ERROR") {
    Write-Host "`n`e[32mConfig already set. Skipping prompts.`e[0m"
    Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
    exit 0
}

# -- Fresh deploy: offer auto-defaults or interactive --
# a1/a3: fast-fail (or apply defaults) if no interactive console.
Require-InteractiveOrPreset

Write-Host ""
Write-Host "`e[33mNo configuration found. Choose setup mode:`e[0m"
Write-Host "  1) Quick start - use recommended defaults (fastest, no GPU)"
Write-Host "  2) Custom     - choose VM sizes, GPU, metadata store"
Write-Host ""
$setupMode = Read-InputSafely -Prompt "Choice [1]" -Default "1"

if ($setupMode -eq "1") {
    Write-Host "`n`e[32mApplying recommended defaults:`e[0m"
    # Honor a pre-set blob-source preference (either var name)
    $qsBlob = (& azd env get-value OMNIVEC_ENABLE_BLOB_SOURCE 2>$null)
    if (-not $qsBlob -or "$qsBlob" -match "ERROR") {
        $qsBlob = (& azd env get-value AZURE_ENABLE_BLOB_SOURCE 2>$null)
    }
    if (-not $qsBlob -or "$qsBlob" -match "ERROR") { $qsBlob = "true" }
    $qsBlob = "$qsBlob".Trim()
    $defaults = [ordered]@{
        "OMNIVEC_SYSTEM_NODE_VM_SIZE" = "Standard_B4ms"
        "OMNIVEC_SYSTEM_NODE_COUNT"   = "2"
        "OMNIVEC_GPU_NODE_VM_SIZE"    = ""
        "OMNIVEC_GPU_NODE_COUNT"      = "0"
        "OMNIVEC_METADATA_STORE"      = "cosmosdb-serverless"
        "OMNIVEC_ENABLE_BLOB_SOURCE"  = $qsBlob
    }
    foreach ($kv in $defaults.GetEnumerator()) {
        azd env set $kv.Key $kv.Value
        Write-Host "  $($kv.Key) = $($kv.Value)"
    }
    Write-Host "`n  System pool: 2x Standard_B4ms (4 vCPU, 16 GB each)"
    Write-Host "  GPU pool: none (use Azure OpenAI for embeddings)"
    Write-Host "  Metadata: CosmosDB Serverless"
    if ($qsBlob -eq "true") {
        Write-Host "  Blob source: enabled"
    } else {
        Write-Host "  Blob source: disabled (CosmosDB sources only)"
    }
    Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
    exit 0
}

# -- Helper: get azd env value safely --
function Get-EnvValue {
    param([string]$Key)
    $ErrorActionPreference = "SilentlyContinue"
    $val = azd env get-value $Key 2>$null
    $ec = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($ec -eq 0 -and $val -and "$val" -notmatch "ERROR") { return "$val".Trim() }
    return $null
}

# -- Metadata storage selection --
$curMeta = Get-EnvValue "OMNIVEC_METADATA_STORE"
if ($curMeta) {
    Write-Host "`n`e[32mMetadata store: $curMeta (already set)`e[0m"
} else {
    $defMetaNum = "1"
    Write-Host ""
    Write-Host "`e[33mSelect metadata storage backend:`e[0m"
    Write-Host "  1) Azure CosmosDB (Serverless NoSQL)"
    Write-Host "  2) Azure CosmosDB (Provisioned throughput)"
    Write-Host ""
    $metaChoice = Read-InputSafely -Prompt "Choice [$defMetaNum]" -Default $defMetaNum
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
}

# -- Blob storage source --
$curBlob = Get-EnvValue "OMNIVEC_ENABLE_BLOB_SOURCE"
if ($curBlob) {
    Write-Host "`e[32mBlob source: $curBlob (already set)`e[0m"
} else {
    $defBlobNum = "1"
    Write-Host ""
    Write-Host "`e[33mWill you use Azure Blob Storage as a document source?`e[0m"
    Write-Host "  If yes, Service Bus (jobs queue) and Event Grid (blob event routing)"
    Write-Host "  will be created alongside the Storage Account."
    Write-Host ""
    Write-Host "  1) Yes - enable blob source ingestion"
    Write-Host "  2) No  - CosmosDB sources only (skip Service Bus + Event Grid)"
    Write-Host ""
    $blobChoice = Read-InputSafely -Prompt "Choice [$defBlobNum]" -Default $defBlobNum
    if ($blobChoice -eq "1") {
        Write-Host "`e[32mBlob source enabled.`e[0m"
        azd env set OMNIVEC_ENABLE_BLOB_SOURCE "true"
    } else {
        Write-Host "`e[32mBlob source disabled.`e[0m"
        azd env set OMNIVEC_ENABLE_BLOB_SOURCE "false"
    }
}

# -- Node provisioning --
Write-Host ""
Write-Host "`e[33mConfigure AKS node pools:`e[0m"
Write-Host ""

$location = $env:AZURE_LOCATION
if (-not $location) { $location = "eastus2" }

# Helper: validate a single SKU in the location
function Test-SkuAvailable {
    param($Sku, $Location)
    $result = az vm list-skus --location $Location --size $Sku --resource-type virtualMachines --query "[?name=='$Sku' && (restrictions==null || restrictions[0]==null)].name" -o tsv 2>$null
    return ($result -and $result.Trim() -eq $Sku)
}

# -- System node pool --
$curSysSku = Get-EnvValue "OMNIVEC_SYSTEM_NODE_VM_SIZE"
$curSysCount = Get-EnvValue "OMNIVEC_SYSTEM_NODE_COUNT"

if ($curSysSku) {
    Write-Host "`e[32mSystem VM SKU: $curSysSku (already set)`e[0m"
    $SYS_SKU = $curSysSku
} else {
    Write-Host "`e[36mSystem node pool (API, controller, worker, changefeed):`e[0m"

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
        if ($failedSkus.ContainsKey($sysCandidates[$i].name)) { $mark = " `e[31m[x unavailable]`e[0m" }
        elseif (-not $nextDefault) { $nextDefault = $($i+1) }
        Write-Host "    $($i+1)) $($sysCandidates[$i].name) - $($sysCandidates[$i].desc)$mark"
    }
    Write-Host "    $($sysCandidates.Count+1)) Enter custom SKU"
    Write-Host ""
    if (-not $nextDefault) { $nextDefault = $($defIdx+1) }

    $sysPick = Read-InputSafely -Prompt "  System VM SKU [$nextDefault]" -Default "$nextDefault"

    if ([int]$sysPick -eq ($sysCandidates.Count + 1)) {
        $defManual = if ($curSysSku) { $curSysSku } else { "Standard_D4s_v3" }
        $candidate = Read-InputSafely -Prompt "  Enter SKU name [$defManual]" -Default $defManual
    } else {
        $idx = [int]$sysPick - 1
        if ($idx -ge 0 -and $idx -lt $sysCandidates.Count) {
            $candidate = $sysCandidates[$idx].name
        } else {
            $candidate = $sysCandidates[[int]$nextDefault - 1].name
        }
    }

    if ($failedSkus.ContainsKey($candidate)) {
        Write-Host "  `e[31m$candidate already checked - not available. Pick another.`e[0m"
        continue
    }

    Write-Host "  `e[36mValidating $candidate in $location...`e[0m" -NoNewline
    if (Test-SkuAvailable -Sku $candidate -Location $location) {
        Write-Host " `e[32m* available`e[0m"
        $SYS_SKU = $candidate
    } else {
        Write-Host " `e[31mx not available in $location`e[0m"
        $failedSkus[$candidate] = $true
    }
}
Write-Host "  `e[32mSystem VM SKU: $SYS_SKU`e[0m"
} # end else (SKU not pre-set)

if ($curSysCount) {
    Write-Host "`e[32mSystem nodes: $curSysCount (already set)`e[0m"
    $sysCount = $curSysCount
} else {
    $defSysCount = "2"
    $sysCount = Read-InputSafely -Prompt "  System node count [$defSysCount]" -Default $defSysCount
    Write-Host "  `e[32mSystem nodes: $sysCount`e[0m"
}

Write-Host ""

# -- GPU node pool --
$curGpuSku = Get-EnvValue "OMNIVEC_GPU_NODE_VM_SIZE"
$curGpuCount = Get-EnvValue "OMNIVEC_GPU_NODE_COUNT"

if ($curGpuCount) {
    Write-Host "`e[32mGPU nodes: $curGpuCount (already set)`e[0m"
    $gpuCount = $curGpuCount
    $GPU_SKU = if ($curGpuSku) { $curGpuSku } else { "" }
} else {
    Write-Host "`e[36mGPU node pool (ML models - dse-qwen2, clip, bge, bge-small):`e[0m"
    Write-Host "  Enter 0 nodes to skip GPU pool (use external models only)."

    $defGpuCount = "0"
    $gpuCount = Read-InputSafely -Prompt "  GPU node count (0 = no GPU pool) [$defGpuCount]" -Default $defGpuCount

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
            if ($failedGpuSkus.ContainsKey($gpuCandidates[$i].name)) { $mark = " `e[31m[x unavailable]`e[0m" }
            elseif (-not $nextGpuDefault) { $nextGpuDefault = $($i+1) }
            Write-Host "    $($i+1)) $($gpuCandidates[$i].name) - $($gpuCandidates[$i].desc)$mark"
        }
        Write-Host "    $($gpuCandidates.Count+1)) Enter custom SKU"
        Write-Host ""
        if (-not $nextGpuDefault) { $nextGpuDefault = $($defGpuIdx+1) }

        $gpuPick = Read-InputSafely -Prompt "  GPU VM SKU [$nextGpuDefault]" -Default "$nextGpuDefault"

        if ([int]$gpuPick -eq ($gpuCandidates.Count + 1)) {
            $defGpuManual = if ($curGpuSku) { $curGpuSku } else { "Standard_NC4as_T4_v3" }
            $candidate = Read-InputSafely -Prompt "  Enter SKU name [$defGpuManual]" -Default $defGpuManual
        } else {
            $idx = [int]$gpuPick - 1
            if ($idx -ge 0 -and $idx -lt $gpuCandidates.Count) {
                $candidate = $gpuCandidates[$idx].name
            } else {
                $candidate = $gpuCandidates[[int]$nextGpuDefault - 1].name
            }
        }

        if ($failedGpuSkus.ContainsKey($candidate)) {
            Write-Host "  `e[31m$candidate already checked - not available. Pick another.`e[0m"
            continue
        }

        Write-Host "  `e[36mValidating $candidate in $location...`e[0m" -NoNewline
        if (Test-SkuAvailable -Sku $candidate -Location $location) {
            Write-Host " `e[32m* available`e[0m"
            $GPU_SKU = $candidate
        } else {
            Write-Host " `e[31mx not available in $location`e[0m"
            $failedGpuSkus[$candidate] = $true
        }
    }
    Write-Host "  `e[32mGPU VM: $GPU_SKU, nodes: $gpuCount`e[0m"
} else {
    Write-Host "  `e[33mGPU pool disabled - using external embedding models only.`e[0m"
    $GPU_SKU = if ($curGpuSku) { $curGpuSku } else { "" }
}
} # end else (GPU not pre-set)

# Validate before storing
if (-not $SYS_SKU) {
    Write-Host "`e[31mNo system VM SKU selected. Cannot proceed.`e[0m"
    exit 1
}

# Strip any whitespace/quotes from values before writing — azd env set writes
# values verbatim, and embedded newlines corrupt the .env file irrecoverably.
function Clean-EnvValue($v) {
    if ($null -eq $v) { return "" }
    return (("$v") -replace '[\r\n\t]+', '' -replace '^"|"$', '').Trim()
}
$SYS_SKU  = Clean-EnvValue $SYS_SKU
$GPU_SKU  = Clean-EnvValue $GPU_SKU
$sysCount = Clean-EnvValue $sysCount
$gpuCount = Clean-EnvValue $gpuCount

# Store in azd env
azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE $SYS_SKU
azd env set OMNIVEC_SYSTEM_NODE_COUNT $sysCount
azd env set OMNIVEC_GPU_NODE_VM_SIZE $GPU_SKU
azd env set OMNIVEC_GPU_NODE_COUNT $gpuCount

# -- Sanitize env values: strip BOM, tabs, carriage returns --
Write-Host "`n`e[36mSanitizing environment values...`e[0m"
$envKeys = @("OMNIVEC_SYSTEM_NODE_VM_SIZE", "OMNIVEC_SYSTEM_NODE_COUNT", "OMNIVEC_GPU_NODE_VM_SIZE", "OMNIVEC_GPU_NODE_COUNT", "OMNIVEC_ENABLE_BLOB_SOURCE", "OMNIVEC_METADATA_STORE")
foreach ($key in $envKeys) {
    $ErrorActionPreference = "SilentlyContinue"
    $raw = azd env get-value $key 2>$null
    $ec = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    # Skip missing keys or azd error-text responses
    if ($ec -ne 0 -or -not $raw -or "$raw" -match "^\s*ERROR" -or "$raw" -match "not found") { continue }
    $clean = $raw -replace '[\t\r\n]','' -replace '^\xEF\xBB\xBF','' -replace '^"|"$','' -replace '^\s+|\s+$',''
    if ($clean -ne $raw) {
        azd env set $key $clean
        Write-Host "  `e[33mCleaned ${key}: removed hidden characters`e[0m"
    }
}

Write-Host "`n`e[32mPre-provision checks passed. Proceeding with Bicep deployment...`e[0m"
Write-Host "`e[36mEnvironment: $env:AZURE_ENV_NAME`e[0m"
Write-Host "`e[36mEach installation gets a unique resource token derived from (subscription + resource group + env name).`e[0m"

} finally {
    Release-Lock
}
