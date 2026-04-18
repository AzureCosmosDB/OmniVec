# scripts/azd-up.ps1 - OmniVec-hardened `azd up` wrapper (Windows).
#
# Mirrors scripts/azd-up.sh:
#   1. Preflight checks (providers, name collisions) before any deploy
#   2. Background ARM deployment ticker for live resource-level progress
#   3. Timestamped output to a .azd-logs/<ts>.log file
#   4. On-failure diagnostic dump
#
# Usage:
#   pwsh -File scripts\azd-up.ps1
#   pwsh -File scripts\azd-up.ps1 -Preview         # what-if only, no deploy
#   pwsh -File scripts\azd-up.ps1 -SkipPreflight   # debugging only
#   $env:OMNIVEC_NONINTERACTIVE=1; pwsh -File scripts\azd-up.ps1
[CmdletBinding()]
param(
    [switch]$Preview,
    [switch]$SkipPreflight
)

$ErrorActionPreference = 'Continue'
$ScriptDir = $PSScriptRoot
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir '..')).Path

$LogDir = if ($env:OMNIVEC_LOG_DIR) { $env:OMNIVEC_LOG_DIR } else { Join-Path $RepoRoot '.azd-logs' }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir ("azd-up-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function HB-Log {
    param([string]$Msg)
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Msg
    Write-Host $line -ForegroundColor Cyan
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function HB-Step {
    param([string]$Name, [scriptblock]$Body)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    HB-Log "--- begin: $Name ---"
    try {
        & $Body
        $rc = $LASTEXITCODE
    } catch {
        $rc = 1
        HB-Log "ERROR in ${Name}: $_"
    } finally {
        $sw.Stop()
        HB-Log ("--- end  : {0} ({1:n1}s, rc={2}) ---" -f $Name, $sw.Elapsed.TotalSeconds, $rc)
    }
    return $rc
}

# -- Resolve env + location from azd ------------------------------------------
function Get-AzdValue($Key) {
    $out = azd env get-value $Key 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
    return $null
}

$AzureEnvName  = if ($env:AZURE_ENV_NAME) { $env:AZURE_ENV_NAME } else { Get-AzdValue 'AZURE_ENV_NAME' }
$AzureLocation = if ($env:AZURE_LOCATION) { $env:AZURE_LOCATION } else { Get-AzdValue 'AZURE_LOCATION' }
if (-not $AzureEnvName) {
    Write-Host "AZURE_ENV_NAME not set. Run 'azd env new <name>' first." -ForegroundColor Red
    exit 1
}
if (-not $AzureLocation) { $AzureLocation = 'centralus' }

$RgName = "rg-omnivec-$AzureEnvName"

HB-Log "OmniVec azd up wrapper starting (env=$AzureEnvName, location=$AzureLocation)"
HB-Log "Log file: $LogFile"

# -- Preflight (provider register + name collision check) ---------------------
if (-not $SkipPreflight) {
    HB-Step 'preflight' {
        $needed = @(
            'Microsoft.ContainerService','Microsoft.ContainerRegistry',
            'Microsoft.OperationalInsights','Microsoft.Storage',
            'Microsoft.DocumentDB','Microsoft.KeyVault','Microsoft.Network',
            'Microsoft.ServiceBus','Microsoft.EventGrid','Microsoft.Insights'
        )
        foreach ($ns in $needed) {
            $state = (az provider show --namespace $ns --query 'registrationState' -o tsv 2>$null)
            if ($state -and $state.Trim() -ne 'Registered') {
                HB-Log "registering provider: $ns"
                az provider register --namespace $ns 2>&1 | Out-Null
            }
        }
        HB-Log "providers OK"
    } | Out-Null
} else {
    HB-Log '[skipped preflight]'
}

# -- Preview (what-if) --------------------------------------------------------
if ($Preview) {
    HB-Log "running 'azd provision --preview' (what-if)..."
    azd provision --preview 2>&1 | ForEach-Object {
        $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $_
        Write-Host $line
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    }
    exit $LASTEXITCODE
}

# -- Deploy ticker: poll ARM every N seconds in a background job --------------
$TickerInterval = if ($env:OMNIVEC_TICKER_INTERVAL) { [int]$env:OMNIVEC_TICKER_INTERVAL } else { 30 }

$TickerJob = Start-Job -Name 'omnivec-ticker' -ArgumentList $RgName, $TickerInterval, $LogFile -ScriptBlock {
    param($Rg, $Interval, $Log)
    while ($true) {
        Start-Sleep -Seconds $Interval
        try {
            $dep = (az deployment group list --resource-group $Rg `
                --query "sort_by([], &properties.timestamp)[-1].{name:name, state:properties.provisioningState}" `
                -o json 2>$null) | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($dep -and $dep.name) {
                $ops = az deployment operation group list --resource-group $Rg --name $dep.name `
                    --query "[?properties.provisioningState!='Succeeded'].{type:properties.targetResource.resourceType, name:properties.targetResource.resourceName, state:properties.provisioningState}" `
                    -o json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
                $inFlight = if ($ops) { $ops.Count } else { 0 }
                $msg = "[TICKER {0}] deployment '{1}' {2} - {3} resources in-flight" -f `
                    (Get-Date -Format 'HH:mm:ss'), $dep.name, $dep.state, $inFlight
                Write-Output $msg
                if ($ops) {
                    $ops | Select-Object -First 3 | ForEach-Object {
                        Write-Output ("          -> {0} {1} [{2}]" -f $_.type, $_.name, $_.state)
                    }
                }
            }
        } catch { }
    }
}

# Emit ticker output to console AND log, but don't block the deploy.
$TickerMonitor = Start-Job -Name 'omnivec-ticker-monitor' -ArgumentList $TickerJob.Id, $LogFile -ScriptBlock {
    param($JobId, $Log)
    while ($true) {
        $out = Receive-Job -Id $JobId -Keep:$false -ErrorAction SilentlyContinue
        if ($out) {
            foreach ($line in $out) {
                Write-Host $line -ForegroundColor DarkCyan
                Add-Content -Path $Log -Value $line -Encoding utf8
            }
        }
        Start-Sleep -Seconds 2
    }
}

function Stop-Ticker {
    try { Stop-Job -Job $TickerJob -ErrorAction SilentlyContinue; Remove-Job -Job $TickerJob -Force -ErrorAction SilentlyContinue } catch {}
    try { Stop-Job -Job $TickerMonitor -ErrorAction SilentlyContinue; Remove-Job -Job $TickerMonitor -Force -ErrorAction SilentlyContinue } catch {}
}

# -- Run azd up, prefix every line with timestamp, mirror to log --------------
HB-Log "--- begin: azd_up ---"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$azdRc = 0
try {
    azd up 2>&1 | ForEach-Object {
        $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $_
        Write-Host $line
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    }
    $azdRc = $LASTEXITCODE
} catch {
    $azdRc = 1
    HB-Log "ERROR: $_"
} finally {
    $sw.Stop()
    HB-Log ("--- end  : azd_up ({0:n1}s, rc={1}) ---" -f $sw.Elapsed.TotalSeconds, $azdRc)
    Stop-Ticker
}

# -- On failure: dump diagnostics to log --------------------------------------
if ($azdRc -ne 0) {
    Write-Host ""
    Write-Host ("==== azd up FAILED (rc={0}) ====" -f $azdRc) -ForegroundColor Red
    Write-Host "Gathering diagnostics into $LogFile ..." -ForegroundColor Yellow
    Add-Content -Path $LogFile -Value "`n## Failure diagnostics`n"

    Add-Content -Path $LogFile -Value "`n### RG last deployments"
    (az deployment group list --resource-group $RgName `
        --query "[].{name:name,state:properties.provisioningState,ts:properties.timestamp}" `
        -o table 2>&1 | Select-Object -First 10) | Add-Content -Path $LogFile

    $latest = (az deployment group list --resource-group $RgName `
        --query "sort_by([?properties.provisioningState=='Failed'], &properties.timestamp)[-1].name" `
        -o tsv 2>$null).Trim()
    if ($latest) {
        Add-Content -Path $LogFile -Value "`n### Failed operations in deployment '$latest'"
        (az deployment operation group list --resource-group $RgName --name $latest `
            --query "[?properties.provisioningState=='Failed'].{type:properties.targetResource.resourceType, name:properties.targetResource.resourceName, msg:properties.statusMessage.error.message}" `
            -o json 2>&1 | Select-Object -First 100) | Add-Content -Path $LogFile
    }

    Write-Host "See $LogFile for full diagnostics." -ForegroundColor Yellow
    Write-Host "Run 'pwsh scripts\doctor.ps1' for environment checks." -ForegroundColor Yellow
}

exit $azdRc
