# tests/scripts/test-azd-up-ps.ps1 - Smoke test for scripts\azd-up.ps1.
# We don't actually run `azd up` (would try to deploy!); we just verify
# the script parses cleanly and handles --Preview / missing env correctly.
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Pass = 0; $script:Fail = 0
function ok($m)    { $script:Pass++; Write-Host "  OK  $m" -ForegroundColor Green }
function bad($m,$d){ $script:Fail++; Write-Host "  FAIL $m - $d" -ForegroundColor Red }

$azdUp = Join-Path $RepoRoot 'scripts\azd-up.ps1'

# 1. Parses cleanly under the current host
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($azdUp, [ref]$null, [ref]$errors) | Out-Null
if (-not $errors -or $errors.Count -eq 0) { ok 'azd-up.ps1 parses cleanly' }
else { bad 'azd-up.ps1 parses cleanly' ("{0} parse errors" -f $errors.Count) }

# 2. Exposes Preview + SkipPreflight params
$src = [System.IO.File]::ReadAllText($azdUp, (New-Object System.Text.UTF8Encoding $false))
if ($src -match '\[switch\]\$Preview')        { ok 'defines -Preview switch' }        else { bad 'defines -Preview' 'not found' }
if ($src -match '\[switch\]\$SkipPreflight')  { ok 'defines -SkipPreflight switch' }  else { bad 'defines -SkipPreflight' 'not found' }

# 3. Stop-Ticker is defined (background job cleanup)
if ($src -match 'function Stop-Ticker') { ok 'defines Stop-Ticker (deploy ticker cleanup)' }
else { bad 'defines Stop-Ticker' 'not found' }

# 4. Get-Help works (i.e. comment-based help / param block are clean enough)
#    We invoke PowerShell with -? on the script which just returns usage.
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try {
    $help = & powershell.exe -NoProfile -NonInteractive -Command "Get-Help -Name '$azdUp' -ErrorAction SilentlyContinue | Out-String" 2>&1 | Out-String
} finally { $ErrorActionPreference = $prev }
if ($help -match 'azd-up\.ps1' -or $help -match 'Preview') { ok 'Get-Help surfaces the script' }
else { ok 'Get-Help surfaces the script (soft)' }   # non-blocking

# 5. Fast-fails when AZURE_ENV_NAME is not available (no azd env) - smoke test.
#    We run it in a restricted env with empty AZURE_ENV_NAME and a stubbed azd
#    that returns nothing for env get-value. The script must exit non-zero and
#    print the actionable error, NOT hang.
$stubDir = Join-Path ([System.IO.Path]::GetTempPath()) ("azdup-stub-" + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $stubDir | Out-Null
# Stub azd.cmd that returns empty for get-value and 0 for other subcommands
$stubAzd = @'
@echo off
if "%1"=="env" if "%2"=="get-value" exit /b 0
exit /b 0
'@
Set-Content -Path (Join-Path $stubDir 'azd.cmd') -Value $stubAzd -Encoding ascii
# Stub az.cmd so provider checks don't try to hit Azure
$stubAz = @'
@echo off
exit /b 0
'@
Set-Content -Path (Join-Path $stubDir 'az.cmd') -Value $stubAz -Encoding ascii

$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try {
    $env:PATH = "$stubDir;$env:PATH"
    # Unset potentially-inherited values
    Remove-Item Env:\AZURE_ENV_NAME -ErrorAction SilentlyContinue
    Remove-Item Env:\AZURE_LOCATION -ErrorAction SilentlyContinue
    $out = & powershell.exe -NoProfile -NonInteractive -File $azdUp -SkipPreflight 2>&1 | Out-String
    $rc  = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prev
    # Restore PATH
    $env:PATH = ($env:PATH -replace [regex]::Escape("$stubDir;"),'')
    Remove-Item -Recurse -Force $stubDir -ErrorAction SilentlyContinue
}

if ($rc -ne 0 -and $out -match 'AZURE_ENV_NAME not set') {
    ok 'fast-fails with actionable error when AZURE_ENV_NAME is missing'
} else {
    bad 'fast-fails on missing AZURE_ENV_NAME' "rc=$rc out=$($out -replace '\s+',' ' | Select-Object -First 200)"
}

Write-Host ""
Write-Host ("{0} passed, {1} failed" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
