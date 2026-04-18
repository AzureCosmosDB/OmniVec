# tests/scripts/test-doctor.ps1 - smoke-test scripts/doctor.ps1
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Pass = 0; $script:Fail = 0
function ok($m)  { $script:Pass++; Write-Host "  OK  $m" -ForegroundColor Green }
function bad($m,$d){ $script:Fail++; Write-Host "  FAIL $m - $d" -ForegroundColor Red }

# 1. parse cleanly
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $RepoRoot 'scripts\doctor.ps1'), [ref]$null, [ref]$errors) | Out-Null
if (-not $errors -or $errors.Count -eq 0) { ok 'doctor.ps1 parses cleanly' }
else { bad 'doctor.ps1 parses cleanly' ("{0} parse errors" -f $errors.Count) }

# 2. runs to completion in non-interactive mode and produces banner + summary
$env:OMNIVEC_NONINTERACTIVE = '1'
# 'Continue' so that stderr output from the subprocess (e.g. "azd not found"
# on CI agents without the tools installed) doesn't abort the test: we only
# care that the script printed its banner + summary.
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try {
    $out = & powershell.exe -NoProfile -NonInteractive -File (Join-Path $RepoRoot 'scripts\doctor.ps1') 2>&1 | Out-String
} finally {
    $ErrorActionPreference = $prev
}
Remove-Item Env:\OMNIVEC_NONINTERACTIVE -ErrorAction SilentlyContinue

if ($out -match 'OmniVec Doctor') { ok 'doctor.ps1 emits banner' }
else { bad 'doctor.ps1 emits banner' 'no banner in output' }

if ($out -match 'Summary') { ok 'doctor.ps1 emits summary' }
else { bad 'doctor.ps1 emits summary' 'no summary in output' }

Write-Host ""
Write-Host ("{0} passed, {1} failed" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
