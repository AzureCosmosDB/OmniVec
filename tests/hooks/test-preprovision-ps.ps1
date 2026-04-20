# tests/hooks/test-preprovision-ps.ps1
# Exercises the a1/a3 helpers added to hooks/preprovision.ps1 without
# actually running the hook (which would try to acquire an azd env lock).
#
# Strategy: extract each helper function's source block via regex and
# dot-source it into this script, then call it directly.

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Pass = 0; $script:Fail = 0
function ok($m)   { $script:Pass++; Write-Host "  OK  $m" -ForegroundColor Green }
function bad($m,$d){ $script:Fail++; Write-Host "  FAIL $m - $d" -ForegroundColor Red }

$hookPath = Join-Path $RepoRoot 'hooks\preprovision.ps1'
# Read as explicit UTF-8 so Windows PowerShell 5.1 (default: ANSI) doesn't
# mis-decode characters like em-dash in the hook source.
$hookSrc  = [System.IO.File]::ReadAllText($hookPath, (New-Object System.Text.UTF8Encoding $false))

# 1. parse cleanly
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($hookPath, [ref]$null, [ref]$errors) | Out-Null
if (-not $errors -or $errors.Count -eq 0) { ok 'preprovision.ps1 parses cleanly' }
else { bad 'preprovision.ps1 parses' ("{0} errors" -f $errors.Count) }

# 2. All required helpers are defined
foreach ($fn in 'Test-CanPrompt','Test-IsNonInteractive','Read-InputSafely','Use-QuickstartDefaults','Require-InteractiveOrPreset') {
    if ($hookSrc -match [regex]::Escape("function $fn")) { ok "defines $fn" }
    else { bad "defines $fn" 'function not found' }
}

# 3. No bare Read-Host calls outside the Read-InputSafely helper (a1 regression guard)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($hookPath, [ref]$null, [ref]$null)
$cmds = $ast.FindAll({
    param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
              $n.GetCommandName() -eq 'Read-Host'
}, $true)
$bare = @()
foreach ($c in $cmds) {
    $parentFn = $c
    while ($parentFn -and -not ($parentFn -is [System.Management.Automation.Language.FunctionDefinitionAst])) {
        $parentFn = $parentFn.Parent
    }
    if (-not $parentFn -or $parentFn.Name -ne 'Read-InputSafely') { $bare += $c }
}
if ($bare.Count -eq 0) { ok 'no bare Read-Host outside Read-InputSafely helper' }
else { bad 'no bare Read-Host outside helper' ("{0} bare calls at lines: {1}" -f $bare.Count, (($bare | ForEach-Object { $_.Extent.StartLineNumber }) -join ',')) }

# 4. Extract + dot-source the helper block so we can actually invoke them
$helperPattern = '(?s)function Release-Lock \{.*?\nAcquire-Lock'
$m = [regex]::Match($hookSrc, $helperPattern)
if (-not $m.Success) {
    bad 'extract helper block' 'helper block pattern not matched'
} else {
    # Strip the final "Acquire-Lock" line (we don't want to run it).
    $helpers = $m.Value -replace 'Acquire-Lock\s*$',''
    # Sanitize: remove any "exit" calls in Require-InteractiveOrPreset so
    # sourcing it into pwsh doesn't terminate our test process.
    $helpers = $helpers -replace '\bexit 1\b','throw "EXIT_1"'
    $helpers = $helpers -replace '\bexit 0\b','return'
    Invoke-Expression $helpers
    ok 'helper block dot-sources without error'

    # 5. Test-CanPrompt honors OMNIVEC_FORCE_NO_TTY
    $env:OMNIVEC_FORCE_NO_TTY = '1'
    if (-not (Test-CanPrompt)) { ok 'Test-CanPrompt honors OMNIVEC_FORCE_NO_TTY=1' }
    else { bad 'Test-CanPrompt honors OMNIVEC_FORCE_NO_TTY' 'returned true' }
    Remove-Item Env:\OMNIVEC_FORCE_NO_TTY

    # 6. Test-IsNonInteractive honors common env vars
    $env:OMNIVEC_NONINTERACTIVE = '1'
    if (Test-IsNonInteractive) { ok 'Test-IsNonInteractive honors OMNIVEC_NONINTERACTIVE' }
    else { bad 'Test-IsNonInteractive honors OMNIVEC_NONINTERACTIVE' 'returned false' }
    Remove-Item Env:\OMNIVEC_NONINTERACTIVE

    $env:CI = 'true'
    if (Test-IsNonInteractive) { ok 'Test-IsNonInteractive honors CI=true' }
    else { bad 'Test-IsNonInteractive honors CI' 'returned false' }
    Remove-Item Env:\CI

    if (Test-IsNonInteractive) { bad 'Test-IsNonInteractive false when unset' 'returned true unexpectedly' }
    else { ok 'Test-IsNonInteractive false when no flags set' }

    # 7. Read-InputSafely returns default when we can't prompt (no hang, no throw)
    $env:OMNIVEC_FORCE_NO_TTY = '1'
    try {
        $v = Read-InputSafely -Prompt 'ignored' -Default 'mydefault'
        if ($v -eq 'mydefault') { ok 'Read-InputSafely returns default in no-TTY mode' }
        else { bad 'Read-InputSafely returns default' "got '$v'" }
    } catch {
        bad 'Read-InputSafely no-TTY' "threw: $_"
    }
    Remove-Item Env:\OMNIVEC_FORCE_NO_TTY
}

Write-Host ""
Write-Host ("{0} passed, {1} failed" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
