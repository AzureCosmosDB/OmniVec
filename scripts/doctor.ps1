# scripts/doctor.ps1 — OmniVec environment diagnostic tool (Windows).
# Mirror of scripts/doctor.sh. Exits 0 if no fails.

$ErrorActionPreference = 'Continue'
$script:Fails = 0; $script:Warns = 0; $script:Passes = 0

function Write-Pass($m)    { $script:Passes++; Write-Host ("  [{0}] {1}" -f 'OK', $m) -ForegroundColor Green }
function Write-Warn($m,$d) { $script:Warns++;  Write-Host ("  [{0}] {1}" -f '!',  $m) -ForegroundColor Yellow; if ($d) { Write-Host "     $d" } }
function Write-Fail($m,$d) { $script:Fails++;  Write-Host ("  [{0}] {1}" -f 'X',  $m) -ForegroundColor Red;    if ($d) { Write-Host "     $d" } }

Write-Host "`n=== OmniVec Doctor ===" -ForegroundColor Cyan

Write-Host "`nTools:" -ForegroundColor Cyan
foreach ($t in 'az','azd','kubectl','helm','git','curl') {
    $cmd = Get-Command $t -ErrorAction SilentlyContinue
    if ($cmd) {
        try { $ver = (& $t --version 2>$null | Select-Object -First 1) } catch { $ver = 'installed' }
        Write-Pass "${t}: $ver"
    } else {
        if ($t -in 'kubectl','helm') { Write-Warn "$t not on PATH" "will be auto-installed by hooks" }
        else                         { Write-Fail "$t missing"     "install from vendor docs and rerun" }
    }
}

Write-Host "`nAzure:" -ForegroundColor Cyan
try {
    $acct = az account show 2>$null | ConvertFrom-Json -ErrorAction Stop
    Write-Pass "logged in: $($acct.name) ($($acct.id))"
} catch { Write-Fail "not logged in" "run: az login" }

Write-Host "`nazd environment:" -ForegroundColor Cyan
try {
    $envs = azd env list -o json 2>$null | ConvertFrom-Json -ErrorAction Stop
    if (-not $envs -or $envs.Count -eq 0) {
        Write-Warn "no azd env found" "run: azd env new <name>"
    } else {
        $cur = $envs | Where-Object { $_.IsDefault } | Select-Object -First 1
        if ($cur) { Write-Pass "current env: $($cur.Name)" } else { Write-Warn "no default env selected" }
    }
} catch { Write-Warn "azd env list failed" "is azd installed and initialized?" }

Write-Host "`nOmniVec config:" -ForegroundColor Cyan
$missing = @()
foreach ($k in 'AZURE_LOCATION','AZURE_ENV_NAME') {
    $v = azd env get-value $k 2>$null
    if (-not $v) { $missing += $k }
}
if ($missing) { Write-Warn ("not set: " + ($missing -join ' ')) "azd will prompt or use defaults" }

$niActive = @()
foreach ($v in 'OMNIVEC_NONINTERACTIVE','AZD_NONINTERACTIVE','CI','GITHUB_ACTIONS') {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ($val) { $niActive += "$v=$val" }
}
if ($niActive) { Write-Pass ("non-interactive mode: " + ($niActive -join ' ')) }

Write-Host "`nTerminal:" -ForegroundColor Cyan
$hasRawUI = $false
try { $null = [System.Console]::KeyAvailable; $hasRawUI = $true } catch { $hasRawUI = $false }
if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    Write-Pass "stdin is interactive (prompts will work)"
} elseif ($niActive) {
    Write-Pass "no TTY but non-interactive mode is set"
} else {
    Write-Warn "no interactive stdin" 'set $env:OMNIVEC_NONINTERACTIVE=1 or pre-set config via azd env set'
}

Write-Host "`nBicep:" -ForegroundColor Cyan
$bicepCmd = Get-Command bicep -ErrorAction SilentlyContinue
if ($bicepCmd) {
    Write-Pass ("bicep on PATH: " + ((& bicep --version 2>$null) -split "`n" | Select-Object -First 1))
} elseif (Test-Path (Join-Path $HOME ".azure\bin\bicep.exe")) {
    Write-Pass "bicep installed via az (~/.azure/bin/bicep.exe)"
} else {
    Write-Warn "bicep not installed" "run: az bicep install"
}

$envName = azd env get-value AZURE_ENV_NAME 2>$null
if ($envName) {
    $rg = "rg-omnivec-$envName"
    Write-Host ("`nResource group ({0}):" -f $rg) -ForegroundColor Cyan
    $exists = (az group exists --name $rg 2>$null).Trim()
    if ($exists -eq 'true') {
        Write-Pass "$rg exists (will update in-place)"
        try {
            $failedCount = (az deployment group list --resource-group $rg --query "[?properties.provisioningState=='Failed'] | length(@)" -o tsv 2>$null).Trim()
            if ($failedCount -and [int]$failedCount -gt 0) {
                Write-Warn "$failedCount prior failed deployment(s) on this RG"
            }
        } catch {}
    } else {
        Write-Pass "$rg does not exist yet (fresh deploy)"
    }
}

Write-Host "`nHost:" -ForegroundColor Cyan
try {
    $free = (Get-PSDrive -Name (Split-Path -Qualifier (Get-Location)).TrimEnd(':') -ErrorAction Stop).Free
    if ($free -lt 1GB) { Write-Warn "less than 1 GiB free" } else { Write-Pass "disk space OK" }
} catch {}
Write-Pass "shell: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"

Write-Host ("`n=== Summary: {0} passed, {1} warnings, {2} failures ===" -f $script:Passes, $script:Warns, $script:Fails) -ForegroundColor Cyan
if ($script:Fails -gt 0) { exit 1 } else { exit 0 }
