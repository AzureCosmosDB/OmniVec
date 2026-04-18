# tests/run.ps1 - Windows / PowerShell test runner.
#
# Mirrors tests/run.sh for the Windows side: runs every tests/**/*.ps1
# suite under both Windows PowerShell 5.1 (powershell.exe) and PowerShell
# Core 7+ (pwsh) when available.
#
# Usage:
#   pwsh -File tests/run.ps1              # run all suites under all shells
#   pwsh -File tests/run.ps1 -Shell pwsh  # force a single shell
[CmdletBinding()]
param(
    [string]$Shell = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

$shellsToTry = @()
if ($Shell) {
    $shellsToTry = @($Shell)
} else {
    foreach ($s in 'pwsh','powershell') {
        if (Get-Command $s -ErrorAction SilentlyContinue) { $shellsToTry += $s }
    }
}
if ($shellsToTry.Count -eq 0) {
    Write-Host "No PowerShell hosts available." -ForegroundColor Red
    exit 1
}

$testFiles = @()
$testFiles += Get-ChildItem -Path (Join-Path $RepoRoot 'tests\hooks')   -Filter 'test-*.ps1' -ErrorAction SilentlyContinue
$testFiles += Get-ChildItem -Path (Join-Path $RepoRoot 'tests\scripts') -Filter 'test-*.ps1' -ErrorAction SilentlyContinue
$testFiles += Get-ChildItem -Path (Join-Path $RepoRoot 'tests\infra')   -Filter 'test-*.ps1' -ErrorAction SilentlyContinue

if ($testFiles.Count -eq 0) {
    Write-Host "No .ps1 test suites found under tests/." -ForegroundColor Yellow
    exit 0
}

$totalFail = 0
foreach ($sh in $shellsToTry) {
    $shBin = (Get-Command $sh).Source
    Write-Host ""
    Write-Host ("========== running under {0} ({1}) ==========" -f $sh, $shBin) -ForegroundColor Cyan
    foreach ($t in $testFiles) {
        Write-Host ""
        Write-Host ("--- {0} ---" -f $t.Name) -ForegroundColor Cyan
        & $shBin -NoProfile -NonInteractive -File $t.FullName
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            $totalFail++
            Write-Host ("*** FAILED under {0} (exit {1})" -f $sh, $rc) -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host ("========== done: {0} failing suites ==========" -f $totalFail) -ForegroundColor Cyan
exit $totalFail
