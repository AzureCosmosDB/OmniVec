#!/usr/bin/env pwsh
# OmniVec — Hook Test Harness
# Tests preprovision.ps1 and postprovision.ps1 by mocking external commands.
# Usage: pwsh scripts/test-hooks.ps1

$ErrorActionPreference = "Stop"
$VerbosePreference = "SilentlyContinue"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = (Resolve-Path "$ScriptDir/..").Path
$HooksDir    = Join-Path $RepoRoot "hooks"
$Preprovision  = Join-Path $HooksDir "preprovision.ps1"
$Postprovision = Join-Path $HooksDir "postprovision.ps1"

$PassCount = 0
$FailCount = 0
$Results   = @()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

function New-TestDir {
    $dir = Join-Path ([System.IO.Path]::GetTempPath()) "omnivec-test-$([guid]::NewGuid().ToString('N').Substring(0,8))"
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    return $dir
}

function Remove-TestDir {
    param($Path)
    if ($Path -and (Test-Path $Path)) {
        Remove-Item -Recurse -Force $Path -ErrorAction SilentlyContinue
    }
}

function Write-MockScript {
    <#
        Writes a mock script into $Dir.
        The implementation goes in _<name>_mock.ps1 (underscore-prefixed to
        prevent PowerShell from resolving it directly via PATH).
        A .cmd shim at <name>.cmd invokes it via a fresh pwsh process, which
        ensures proper stderr, exit codes, and isolation from the caller's
        $ErrorActionPreference.
    #>
    param(
        [string]$Dir,
        [string]$Name,      # e.g. "az"
        [string]$Body        # PowerShell script body
    )
    $implPath = Join-Path $Dir "_${Name}_mock.ps1"
    Set-Content -Path $implPath -Value $Body -Encoding UTF8

    $cmdPath = Join-Path $Dir "$Name.cmd"
    $shimBody = @"
@echo off
pwsh -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0_${Name}_mock.ps1" %*
exit /b %errorlevel%
"@
    Set-Content -Path $cmdPath -Value $shimBody -Encoding UTF8
}

function Write-BatchMock {
    <#
        Writes a pure .cmd batch mock (no pwsh subprocess).
        Much faster for scenarios with many mock invocations.
    #>
    param(
        [string]$Dir,
        [string]$Name,
        [string]$BatchBody
    )
    $cmdPath = Join-Path $Dir "$Name.cmd"
    Set-Content -Path $cmdPath -Value $BatchBody -Encoding UTF8
}

function Initialize-AzdEnvDir {
    <#
        Creates a minimal .azure/<env>/.env file so the mocked azd can read/write
        key=value pairs just like the real one.
    #>
    param(
        [string]$Root,
        [string]$EnvName,
        [hashtable]$InitialValues = @{}
    )
    $envDir = Join-Path $Root ".azure" $EnvName
    New-Item -ItemType Directory -Path $envDir -Force | Out-Null
    $lines = @()
    foreach ($kv in $InitialValues.GetEnumerator()) {
        $lines += "$($kv.Key)=`"$($kv.Value)`""
    }
    Set-Content -Path (Join-Path $envDir ".env") -Value ($lines -join "`n") -Encoding UTF8
}

function Invoke-HookTest {
    <#
        Runs a hook script in an isolated process with mocked PATH.
        Returns: @{ ExitCode; Output (string) }
    #>
    param(
        [string]$HookPath,
        [string]$MockBinDir,
        [string]$WorkDir,
        [hashtable]$EnvVars = @{},
        [string]$StdinText = ""
    )

    # Build a wrapper script that sets env, adjusts PATH, and invokes the hook
    $wrapper = Join-Path $WorkDir "_run.ps1"

    $envLines = @()
    foreach ($kv in $EnvVars.GetEnumerator()) {
        $safeVal = $kv.Value -replace "'", "''"
        $envLines += "`$env:$($kv.Key) = '$safeVal'"
    }
    $envBlock = $envLines -join "`n"

    # Wrapper: set env vars, prepend mock dir to PATH, invoke the hook
    $safeMockBin = $MockBinDir -replace "'", "''"
    $safeHookPath = $HookPath -replace "'", "''"
    $wrapperBody = @"
`$ErrorActionPreference = 'Continue'
$envBlock
`$env:Path = '$safeMockBin' + ';' + `$env:Path
try {
    & '$safeHookPath'
    exit `$LASTEXITCODE
} catch {
    Write-Host "HOOK_EXCEPTION: `$(`$_.Exception.Message)"
    Write-Host "HOOK_STACK: `$(`$_.ScriptStackTrace)"
    exit 99
}
"@
    Set-Content -Path $wrapper -Value $wrapperBody -Encoding UTF8

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = "pwsh"
    $psi.Arguments = "-NoProfile -NoLogo -ExecutionPolicy Bypass -File `"$wrapper`""
    $psi.WorkingDirectory = $WorkDir
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.RedirectStandardInput  = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    $proc.Start() | Out-Null

    if ($StdinText) {
        $proc.StandardInput.Write($StdinText)
    }
    $proc.StandardInput.Close()

    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit(120000) # 2-minute timeout

    return @{
        ExitCode = $proc.ExitCode
        Output   = $stdout + $stderr
    }
}

function Assert-Test {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail = ""
    )
    $script:Results += @{ Name = $Name; Passed = $Passed; Detail = $Detail }
    if ($Passed) {
        $script:PassCount++
        Write-Host "[PASS] $Name" -ForegroundColor Green
    } else {
        $script:FailCount++
        Write-Host "[FAIL] $Name" -ForegroundColor Red
        if ($Detail) { Write-Host "       $Detail" -ForegroundColor Yellow }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Mock generators
# ─────────────────────────────────────────────────────────────────────────────

function Get-AzMockBody {
    <#
        Builds az.ps1 mock.  $Behaviours maps argument-pattern strings to
        @{ Output; ExitCode } responses.
    #>
    param([hashtable]$Behaviours)

    $cases = ""
    foreach ($kv in $Behaviours.GetEnumerator()) {
        $pattern = $kv.Key
        $resp    = $kv.Value
        $safeOut = ($resp.Output -replace "'", "''")
        $cases += @"

    if (`$joined -match '$pattern') {
        Write-Output '$safeOut'
        exit $($resp.ExitCode)
    }
"@
    }

    return @"
`$joined = (`$args -join ' ')
$cases
# Default: succeed silently
exit 0
"@
}

function Get-AzdMockBody {
    <#
        Builds azd.ps1 mock using single-quoted template to avoid escaping issues.
        Reads/writes a .env file and supports forced overrides for specific keys.
    #>
    param(
        [string]$EnvFilePath,
        [hashtable]$Overrides = @{}
    )

    # Build override switch cases
    $overrideCases = ""
    foreach ($kv in $Overrides.GetEnumerator()) {
        $k = $kv.Key
        $safeOut = ($kv.Value.Output -replace "'", "''")
        $ec = $kv.Value.ExitCode
        $overrideCases += "        '$k' { Write-Output '$safeOut'; exit $ec }`n"
    }

    # Use a single-quoted here-string for the template (no expansion at all)
    $template = @'
$joined = ($args -join ' ')

if ($joined -match '^env get-value (.+)$') {
    $key = $Matches[1].Trim()

    # Forced overrides
    switch ($key) {
__OVERRIDES__
        default { <# no override for this key, fall through to .env #> }
    }

    # Read from .env file
    $envFile = '__ENVFILE__'
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile)) {
            $parts = $line -split '=', 2
            if ($parts.Count -ge 2) {
                $k = $parts[0].Trim()
                $v = $parts[1].Trim().Trim('"')
                if ($k -eq $key -and $v) {
                    Write-Output $v
                    exit 0
                }
            }
        }
    }
    Write-Error "ERROR: key not found: $key"
    exit 1
}

if ($joined -match '^env set (\S+) (.+)$') {
    $setKey = $Matches[1]
    $setVal = $Matches[2]
    $envFile = '__ENVFILE__'
    $newLines = @()
    $found = $false
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile)) {
            $parts = $line -split '=', 2
            if ($parts.Count -ge 2 -and $parts[0].Trim() -eq $setKey) {
                $newLines += "${setKey}=`"${setVal}`""
                $found = $true
            } else {
                $newLines += $line
            }
        }
    }
    if (-not $found) { $newLines += "${setKey}=`"${setVal}`"" }
    Set-Content -Path $envFile -Value ($newLines -join "`n") -Encoding UTF8
    exit 0
}

if ($joined -match '^env get-values') {
    $envFile = '__ENVFILE__'
    if (Test-Path $envFile) { Get-Content $envFile }
    exit 0
}

# Default
exit 0
'@

    # Replace placeholders (simple string replacement, no regex pitfalls)
    $body = $template.Replace('__ENVFILE__', $EnvFilePath).Replace('__OVERRIDES__', $overrideCases)
    return $body
}

function Get-NoopMockBody {
    return @'
# noop mock
exit 0
'@
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-provision: create docgrok stub and git mock so the hook doesn't fail
# ─────────────────────────────────────────────────────────────────────────────

function Initialize-RepoStubs {
    param([string]$WorkDir)
    # The hook checks for docgrok/Dockerfile to decide whether to init submodules
    $docgrokDir = Join-Path $WorkDir "docgrok"
    New-Item -ItemType Directory -Path $docgrokDir -Force | Out-Null
    Set-Content -Path (Join-Path $docgrokDir "Dockerfile") -Value "# stub" -Encoding UTF8

    # Create a hooks dir symlink / copy so $PSScriptRoot/.. resolves
    $hooksTarget = Join-Path $WorkDir "hooks"
    if (-not (Test-Path $hooksTarget)) {
        New-Item -ItemType Directory -Path $hooksTarget -Force | Out-Null
        # Copy actual hook scripts into the work dir tree
        Copy-Item $Preprovision  (Join-Path $hooksTarget "preprovision.ps1")  -Force
        Copy-Item $Postprovision (Join-Path $hooksTarget "postprovision.ps1") -Force
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Lock-file helper: pre-create the lock directory so the hook doesn't fail
# ─────────────────────────────────────────────────────────────────────────────

function Initialize-LockDir {
    param([string]$EnvName)
    $lockDir = Join-Path $HOME ".omnivec" "locks"
    if (-not (Test-Path $lockDir)) { New-Item -ItemType Directory -Path $lockDir -Force | Out-Null }
    # Remove any stale lock for this env
    $lockFile = Join-Path $lockDir "$EnvName.lock"
    if (Test-Path $lockFile) { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue }
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Preprovision: Fresh deploy, no RG, no config
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario1 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-RepoStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    $envFile = Join-Path $testDir ".azure" $envName ".env"

    # az mock: group does not exist, account show returns valid JSON, vm list-skus returns a match
    Write-MockScript -Dir $mockBin -Name "az" -Body (Get-AzMockBody @{
        "group exists"       = @{ Output = "false";  ExitCode = 0 }
        "account show"       = @{ Output = '{"name":"TestSub","id":"00000000-0000-0000-0000-000000000000"}'; ExitCode = 0 }
        "vm list-skus"       = @{ Output = "Standard_D4s_v3"; ExitCode = 0 }
    })

    # azd mock: all get-value calls fail (nothing configured)
    Write-MockScript -Dir $mockBin -Name "azd" -Body (Get-AzdMockBody -EnvFilePath $envFile -Overrides @{
        "OMNIVEC_SYSTEM_NODE_VM_SIZE" = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_METADATA_STORE"      = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_ENABLE_BLOB_SOURCE"  = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_SYSTEM_NODE_COUNT"   = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_GPU_NODE_VM_SIZE"    = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_GPU_NODE_COUNT"      = @{ Output = "ERROR: key not found"; ExitCode = 1 }
    })

    Write-MockScript -Dir $mockBin -Name "kubectl" -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "helm"    -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "git"     -Body (Get-NoopMockBody)

    $hookPath = Join-Path $testDir "hooks" "preprovision.ps1"
    # Provide stdin: "1" for quick start mode
    $stdin = "1`n"

    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir -StdinText $stdin -EnvVars @{
        AZURE_ENV_NAME  = $envName
        AZURE_LOCATION  = "eastus2"
        HOME            = $HOME
    }

    $showsPrompts = ($r.Output -match "Quick start" -or $r.Output -match "recommended defaults" -or $r.Output -match "Choose setup mode")
    Assert-Test "Scenario 1: Fresh deploy — quick start applies defaults" $showsPrompts `
        "Expected quick start text in output. Exit=$($r.ExitCode). Output tail: $($r.Output.Substring([Math]::Max(0,$r.Output.Length-300)))"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Preprovision: Existing RG with tags → imports config, exits 0
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario2 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-RepoStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    $envFile = Join-Path $testDir ".azure" $envName ".env"

    $tagsJson = '{"omnivec-sys-sku":"Standard_D4s_v3","omnivec-sys-count":"2","omnivec-gpu-sku":"Standard_NC4as_T4_v3","omnivec-gpu-count":"1","omnivec-metadata":"cosmosdb-serverless","omnivec-blob":"true","omnivec-build":"acr"}'

    Write-MockScript -Dir $mockBin -Name "az" -Body (Get-AzMockBody @{
        "group exists"  = @{ Output = "true";     ExitCode = 0 }
        "group show"    = @{ Output = $tagsJson;  ExitCode = 0 }
        "account show"  = @{ Output = '{"name":"TestSub","id":"00000000-0000-0000-0000-000000000000"}'; ExitCode = 0 }
    })

    Write-MockScript -Dir $mockBin -Name "azd" -Body (Get-AzdMockBody -EnvFilePath $envFile)
    Write-MockScript -Dir $mockBin -Name "kubectl" -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "helm"    -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "git"     -Body (Get-NoopMockBody)

    $hookPath = Join-Path $testDir "hooks" "preprovision.ps1"
    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir -EnvVars @{
        AZURE_ENV_NAME = $envName
        AZURE_LOCATION = "eastus2"
        HOME           = $HOME
    }

    $importsConfig = $r.Output -match "Importing config"
    $noPrompts     = $r.Output -notmatch "Select metadata storage backend"
    $exitOk        = $r.ExitCode -eq 0

    Assert-Test "Scenario 2: Existing RG with tags — imports config, no prompts" `
        ($importsConfig -and $noPrompts -and $exitOk) `
        "import=$importsConfig noPrompts=$noPrompts exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Preprovision: Config already set via azd env set (no RG yet)
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario3 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-RepoStubs $testDir

    # Pre-populate .env with config values
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName -InitialValues @{
        OMNIVEC_SYSTEM_NODE_VM_SIZE = "Standard_B4ms"
        OMNIVEC_SYSTEM_NODE_COUNT   = "2"
        OMNIVEC_GPU_NODE_VM_SIZE    = ""
        OMNIVEC_GPU_NODE_COUNT      = "0"
        OMNIVEC_METADATA_STORE      = "cosmosdb-serverless"
        OMNIVEC_ENABLE_BLOB_SOURCE  = "true"
    }

    $envFile = Join-Path $testDir ".azure" $envName ".env"

    Write-MockScript -Dir $mockBin -Name "az" -Body (Get-AzMockBody @{
        "group exists"  = @{ Output = "false"; ExitCode = 0 }
        "account show"  = @{ Output = '{"name":"TestSub","id":"00000000-0000-0000-0000-000000000000"}'; ExitCode = 0 }
    })

    Write-MockScript -Dir $mockBin -Name "azd" -Body (Get-AzdMockBody -EnvFilePath $envFile)
    Write-MockScript -Dir $mockBin -Name "kubectl" -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "helm"    -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "git"     -Body (Get-NoopMockBody)

    $hookPath = Join-Path $testDir "hooks" "preprovision.ps1"
    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir -EnvVars @{
        AZURE_ENV_NAME = $envName
        AZURE_LOCATION = "eastus2"
        HOME           = $HOME
    }

    $skipsPrompts = $r.Output -match "Config already set"
    $exitOk       = $r.ExitCode -eq 0

    Assert-Test "Scenario 3: Config pre-set — skips prompts, exits 0" `
        ($skipsPrompts -and $exitOk) `
        "skipsPrompts=$skipsPrompts exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Preprovision: Existing RG but no tags (legacy)
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario4 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-RepoStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    $envFile = Join-Path $testDir ".azure" $envName ".env"

    Write-MockScript -Dir $mockBin -Name "az" -Body (Get-AzMockBody @{
        "group exists"  = @{ Output = "true";  ExitCode = 0 }
        "group show"    = @{ Output = "null";  ExitCode = 0 }   # no tags
        "account show"  = @{ Output = '{"name":"TestSub","id":"00000000-0000-0000-0000-000000000000"}'; ExitCode = 0 }
        "vm list-skus"  = @{ Output = "Standard_D4s_v3"; ExitCode = 0 }
    })

    # azd: all keys missing
    Write-MockScript -Dir $mockBin -Name "azd" -Body (Get-AzdMockBody -EnvFilePath $envFile -Overrides @{
        "OMNIVEC_SYSTEM_NODE_VM_SIZE" = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_METADATA_STORE"      = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_ENABLE_BLOB_SOURCE"  = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_SYSTEM_NODE_COUNT"   = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_GPU_NODE_VM_SIZE"    = @{ Output = "ERROR: key not found"; ExitCode = 1 }
        "OMNIVEC_GPU_NODE_COUNT"      = @{ Output = "ERROR: key not found"; ExitCode = 1 }
    })

    Write-MockScript -Dir $mockBin -Name "kubectl" -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "helm"    -Body (Get-NoopMockBody)
    Write-MockScript -Dir $mockBin -Name "git"     -Body (Get-NoopMockBody)

    $hookPath = Join-Path $testDir "hooks" "preprovision.ps1"

    # The hook sees RG exists → tries to import tags → $tags is null →
    # falls through past `exit 0` to the config-already-set check → that also
    # fails → falls through to prompts.
    #
    # BUT: in the actual script the `if ($tags)` block is inside the
    # `if ("$rgExists".Trim() -eq "true")` block which ALWAYS ends with
    # `exit 0`.  So even with null tags, the script exits 0 after printing
    # "Importing config...".  The test validates that:
    #   - It detects the existing RG
    #   - It does NOT show the metadata-selection prompt
    #   - It exits 0
    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir -EnvVars @{
        AZURE_ENV_NAME = $envName
        AZURE_LOCATION = "eastus2"
        HOME           = $HOME
    }

    $detectsRg    = $r.Output -match "Existing deployment detected"
    $exitOk       = $r.ExitCode -eq 0

    Assert-Test "Scenario 4: Existing RG, no tags — detects RG, exits 0 (no tag import)" `
        ($detectsRg -and $exitOk) `
        "detectsRg=$detectsRg exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════════
# Postprovision helpers — shared env vars every postprovision test needs
# ═════════════════════════════════════════════════════════════════════════════

function Get-PostprovisionEnvVars {
    param([string]$EnvName = "test-env")
    return @{
        AZURE_ENV_NAME              = $EnvName
        AZURE_LOCATION              = "eastus2"
        AZURE_OMNIVEC_INSTANCE_ID   = "inst-test-1234"
        AZURE_AKS_CLUSTER_NAME      = "aks-omnivec-test"
        AZURE_ACR_LOGIN_SERVER      = "testacr.azurecr.io"
        AZURE_ACR_NAME              = "testacr"
        AZURE_COSMOS_ENDPOINT       = "https://cosmos-test.documents.azure.com:443/"
        AZURE_IDENTITY_CLIENT_ID    = "00000000-1111-2222-3333-444444444444"
        AZURE_RESOURCE_GROUP        = "rg-omnivec-test-env"
        AZURE_ENABLE_BLOB_SOURCE    = "false"
        AZURE_KEYVAULT_URI          = "https://kv-test.vault.azure.net/"
        OMNIVEC_BUILD_MODE          = "acr"
        HOME                        = $HOME
    }
}

function Initialize-PostprovisionStubs {
    <# Create minimal directory stubs the postprovision hook expects. #>
    param([string]$WorkDir)
    Initialize-RepoStubs $WorkDir

    # helm chart stubs
    $helmDir = Join-Path $WorkDir "helm" "omnivec"
    New-Item -ItemType Directory -Path $helmDir -Force | Out-Null
    Set-Content -Path (Join-Path $helmDir "Chart.yaml") -Value "name: omnivec" -Encoding UTF8

    # Dockerfile stubs for build fallback
    foreach ($sub in @("api","web","connectors/ingestion/dotnet","connectors/worker/dotnet","docgrok/pipeline-worker","docgrok/router")) {
        $d = Join-Path $WorkDir $sub
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Set-Content -Path (Join-Path $d "Dockerfile") -Value "FROM scratch" -Encoding UTF8
    }
}

function Write-PostprovisionNoopMocks {
    <# Write fast batch-based noop mocks for kubectl, helm, docker, git, azd. #>
    param([string]$MockBin)
    foreach ($tool in @("kubectl","helm","docker","git")) {
        Write-BatchMock -Dir $MockBin -Name $tool -BatchBody "@exit /b 0"
    }
    # azd: env set succeeds, env get-value always fails (env vars provide values)
    Write-BatchMock -Dir $MockBin -Name "azd" -BatchBody @"
@echo off
echo %* | findstr /i "env set" >nul && exit /b 0
echo ERROR: not found 1>&2
exit /b 1
"@
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — Postprovision: Anonymous pull works
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario5 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-PostprovisionStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    # az: all commands succeed (batch mock — very fast)
    Write-BatchMock -Dir $mockBin -Name "az" -BatchBody @"
@echo off
set "a=%*"
echo %a% | findstr /i "acr import" >nul && exit /b 0
echo %a% | findstr /i "acr repository" >nul && (echo latest& exit /b 0)
echo %a% | findstr /i "acr manifest" >nul && (echo sha256:abc123& exit /b 0)
echo %a% | findstr /i "aks get-credentials" >nul && exit /b 0
echo %a% | findstr /i "group update" >nul && exit /b 0
echo %a% | findstr /i "account show" >nul && (echo {"name":"TestSub","id":"00000000"}& exit /b 0)
exit /b 0
"@
    Write-PostprovisionNoopMocks $mockBin

    $hookPath = Join-Path $testDir "hooks" "postprovision.ps1"
    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir `
        -EnvVars (Get-PostprovisionEnvVars $envName)

    $anonOk = $r.Output -match "anonymous pull works"
    $noTokenPrompt = $r.Output -notmatch "Enter token for"

    Assert-Test "Scenario 5: Anonymous pull works — no token prompt" `
        ($anonOk -and $noTokenPrompt) `
        "anonOk=$anonOk noPrompt=$noTokenPrompt exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Postprovision: Anonymous fails, stored token works
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario6 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-PostprovisionStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    # az: acr import fails without --password, succeeds with --password (batch mock)
    Write-BatchMock -Dir $mockBin -Name "az" -BatchBody @"
@echo off
set "a=%*"
echo %a% | findstr /i "acr import" >nul || goto :not_import
echo %a% | findstr /i "password" >nul && exit /b 0
echo 401 Unauthorized 1>&2
exit /b 1
:not_import
echo %a% | findstr /i "acr repository" >nul && (echo latest& exit /b 0)
echo %a% | findstr /i "acr manifest" >nul && (echo sha256:abc123& exit /b 0)
echo %a% | findstr /i "aks get-credentials" >nul && exit /b 0
echo %a% | findstr /i "group update" >nul && exit /b 0
echo %a% | findstr /i "account show" >nul && (echo {"name":"TestSub","id":"00000000"}& exit /b 0)
exit /b 0
"@
    Write-PostprovisionNoopMocks $mockBin

    $hookPath = Join-Path $testDir "hooks" "postprovision.ps1"
    $envVars = Get-PostprovisionEnvVars $envName
    $envVars["OMNIVEC_SHARED_REGISTRY_TOKEN"] = "stored-token-value-abc123"

    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir -EnvVars $envVars

    $requiresAuth  = $r.Output -match "requires auth"
    $tokenWorks    = $r.Output -match "token works"
    $noPrompt      = $r.Output -notmatch "Enter token for"

    Assert-Test "Scenario 6: Anonymous fails, stored token works — no prompt" `
        ($requiresAuth -and $tokenWorks -and $noPrompt) `
        "requiresAuth=$requiresAuth tokenWorks=$tokenWorks noPrompt=$noPrompt exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 7 — Postprovision: Anonymous fails, no token set → prompts user
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario7 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-PostprovisionStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    # az: acr import fails unless specific token in --password (batch mock)
    Write-BatchMock -Dir $mockBin -Name "az" -BatchBody @"
@echo off
set "a=%*"
echo %a% | findstr /i "acr import" >nul || goto :not_import
echo %a% | findstr /i "user-provided-token-xyz" >nul && exit /b 0
echo 401 Unauthorized 1>&2
exit /b 1
:not_import
echo %a% | findstr /i "acr repository" >nul && (echo latest& exit /b 0)
echo %a% | findstr /i "acr manifest" >nul && (echo sha256:abc123& exit /b 0)
echo %a% | findstr /i "aks get-credentials" >nul && exit /b 0
echo %a% | findstr /i "group update" >nul && exit /b 0
echo %a% | findstr /i "account show" >nul && (echo {"name":"TestSub","id":"00000000"}& exit /b 0)
exit /b 0
"@
    Write-PostprovisionNoopMocks $mockBin

    $hookPath = Join-Path $testDir "hooks" "postprovision.ps1"
    $envVars = Get-PostprovisionEnvVars $envName
    # No OMNIVEC_SHARED_REGISTRY_TOKEN set

    # stdin: provide a token when prompted
    $stdin = "user-provided-token-xyz`n"

    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir `
        -EnvVars $envVars -StdinText $stdin

    $promptsUser = $r.Output -match "token required|Enter token"

    Assert-Test "Scenario 7: Anonymous fails, no token — prompts user for token" `
        $promptsUser `
        "promptsUser=$promptsUser exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 8 — Postprovision: Anonymous fails, stored token also expired
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario8 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-PostprovisionStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    # az: acr import always fails unless the fresh token is present (batch mock)
    Write-BatchMock -Dir $mockBin -Name "az" -BatchBody @"
@echo off
set "a=%*"
echo %a% | findstr /i "acr import" >nul || goto :not_import
echo %a% | findstr /i "fresh-new-token-999" >nul && exit /b 0
echo 401 Unauthorized 1>&2
exit /b 1
:not_import
echo %a% | findstr /i "acr repository" >nul && (echo latest& exit /b 0)
echo %a% | findstr /i "acr manifest" >nul && (echo sha256:abc123& exit /b 0)
echo %a% | findstr /i "aks get-credentials" >nul && exit /b 0
echo %a% | findstr /i "group update" >nul && exit /b 0
echo %a% | findstr /i "account show" >nul && (echo {"name":"TestSub","id":"00000000"}& exit /b 0)
exit /b 0
"@
    Write-PostprovisionNoopMocks $mockBin

    $hookPath = Join-Path $testDir "hooks" "postprovision.ps1"
    $envVars = Get-PostprovisionEnvVars $envName
    $envVars["OMNIVEC_SHARED_REGISTRY_TOKEN"] = "expired-old-token"

    # stdin: provide a fresh token when prompted
    $stdin = "fresh-new-token-999`n"

    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir `
        -EnvVars $envVars -StdinText $stdin

    $tokenInvalid = $r.Output -match "token invalid|expired"
    # Read-Host prompts may not be captured in stdout; check Write-Host text too
    $promptsUser  = $r.Output -match "token required|Enter token"

    Assert-Test "Scenario 8: Stored token expired — prompts user for new token" `
        ($tokenInvalid -and $promptsUser) `
        "tokenInvalid=$tokenInvalid promptsUser=$promptsUser exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 9 — Postprovision: All auth fails → falls back to source build
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario9 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $envName = "test-env"
    Initialize-LockDir $envName
    Initialize-PostprovisionStubs $testDir
    Initialize-AzdEnvDir -Root $testDir -EnvName $envName

    # az: acr import ALWAYS fails; acr build succeeds; acr repository returns OK
    Write-BatchMock -Dir $mockBin -Name "az" -BatchBody @"
@echo off
set "a=%*"
echo %a% | findstr /i "acr import" >nul && (echo 401 Unauthorized 1>&2 & exit /b 1)
echo %a% | findstr /i "acr build" >nul && exit /b 0
echo %a% | findstr /i "acr repository" >nul && (echo latest& exit /b 0)
echo %a% | findstr /i "acr manifest" >nul && (echo sha256:abc123& exit /b 0)
echo %a% | findstr /i "aks get-credentials" >nul && exit /b 0
echo %a% | findstr /i "group update" >nul && exit /b 0
echo %a% | findstr /i "account show" >nul && (echo {"name":"TestSub","id":"00000000"}& exit /b 0)
exit /b 0
"@
    Write-PostprovisionNoopMocks $mockBin

    $hookPath = Join-Path $testDir "hooks" "postprovision.ps1"
    $envVars = Get-PostprovisionEnvVars $envName
    # No token set

    # stdin: press Enter (blank) at token prompt to skip → build from source
    $stdin = "`n"

    $r = Invoke-HookTest -HookPath $hookPath -MockBinDir $mockBin -WorkDir $testDir `
        -EnvVars $envVars -StdinText $stdin

    $fallsBack = $r.Output -match "Falling back to source build|Building images from source"

    Assert-Test "Scenario 9: All auth fails — falls back to source build" `
        $fallsBack `
        "fallsBack=$fallsBack exit=$($r.ExitCode)"

    Remove-TestDir $testDir
}


# Run all scenarios
# ═════════════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  OmniVec Hook Test Harness"                  -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Verify hook files exist
if (-not (Test-Path $Preprovision)) {
    Write-Host "[ERROR] preprovision.ps1 not found at: $Preprovision" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Postprovision)) {
    Write-Host "[ERROR] postprovision.ps1 not found at: $Postprovision" -ForegroundColor Red
    exit 1
}

Write-Host "--- Preprovision Scenarios ---" -ForegroundColor Yellow
Test-Scenario1
Test-Scenario2
Test-Scenario3
Test-Scenario4

Write-Host ""
Write-Host "--- Postprovision Scenarios ---" -ForegroundColor Yellow
Test-Scenario5
Test-Scenario6
Test-Scenario7
Test-Scenario8
Test-Scenario9

Write-Host ""
Write-Host "--- Additional Scenarios ---" -ForegroundColor Yellow

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 10 — Import mode is default (no docker check in preprovision)
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario10 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    Initialize-LockDir "test-env"

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "" -Encoding UTF8

    $azBody = Get-AzMockBody @{
        "group exists"    = @{ Output = "false"; ExitCode = 0 }
        "account show"    = @{ Output = '{"name":"test","id":"sub-123"}'; ExitCode = 0 }
        "vm list-skus"    = @{ Output = "Standard_B4ms"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "preprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = "test-env"; AZURE_LOCATION = "eastus2"; HOME = $HOME } `
        -StdinText "1`n1`n4`n2`n0`n"

    $noDockerCheck = $result.Output -notmatch "Checking image build capability|Docker daemon"
    Assert-Test "Scenario 10: Import mode default — no docker check in preprovision" $noDockerCheck

    Remove-TestDir $testDir
}
Test-Scenario10

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 11 — OMNIVEC_BUILD=true skips import, goes straight to build
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario11 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "OMNIVEC_BUILD=`"true`"" -Encoding UTF8

    $azBody = Get-AzMockBody @{
        "aks get-credentials" = @{ Output = ""; ExitCode = 0 }
        "acr repository"      = @{ Output = "latest"; ExitCode = 0 }
        "group update"        = @{ Output = "{}"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile -Overrides @{
        "AZURE_OMNIVEC_INSTANCE_ID" = @{ Output = "test-inst"; ExitCode = 0 }
        "AZURE_AKS_CLUSTER_NAME"   = @{ Output = "test-aks"; ExitCode = 0 }
        "AZURE_ACR_LOGIN_SERVER"   = @{ Output = "testacr.azurecr.io"; ExitCode = 0 }
        "AZURE_ACR_NAME"           = @{ Output = "testacr"; ExitCode = 0 }
        "AZURE_COSMOS_ENDPOINT"    = @{ Output = "https://test.documents.azure.com"; ExitCode = 0 }
        "AZURE_IDENTITY_CLIENT_ID" = @{ Output = "client-123"; ExitCode = 0 }
        "AZURE_RESOURCE_GROUP"     = @{ Output = "rg-test"; ExitCode = 0 }
        "AZURE_ENABLE_BLOB_SOURCE" = @{ Output = "false"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "docker.cmd") "@exit 1" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "postprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = "test-env"; AZURE_LOCATION = "eastus2"; HOME = $HOME }

    $buildMode = $result.Output -match "Building images from source"
    $noImport = $result.Output -notmatch "Testing anonymous pull"
    Assert-Test "Scenario 11: OMNIVEC_BUILD=true — skips import, builds from source" ($buildMode -and $noImport)

    Remove-TestDir $testDir
}
Test-Scenario11

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 12 — Partial config: some vars set, others not — only prompts missing
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario12 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    Initialize-LockDir "test-env"

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "OMNIVEC_METADATA_STORE=`"cosmosdb-serverless`"`nOMNIVEC_ENABLE_BLOB_SOURCE=`"true`"" -Encoding UTF8

    $azBody = Get-AzMockBody @{
        "group exists"    = @{ Output = "false"; ExitCode = 0 }
        "account show"    = @{ Output = '{"name":"test","id":"sub-123"}'; ExitCode = 0 }
        "vm list-skus"    = @{ Output = "Standard_B4ms"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "preprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = "test-env"; AZURE_LOCATION = "eastus2"; HOME = $HOME } `
        -StdinText "4`n2`n0`n"

    $skippedMeta = $result.Output -match "cosmosdb-serverless.*already set"
    $skippedBlob = $result.Output -match "true.*already set"
    $showedSku   = $result.Output -match "System node pool|Common options"
    Assert-Test "Scenario 12: Partial config — skips set vars, prompts missing ones" ($skippedMeta -and $skippedBlob -and $showedSku)

    Remove-TestDir $testDir
}
Test-Scenario12

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 13 — Re-run with images already in ACR (digest match → skip)
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario13 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "" -Encoding UTF8

    $azBody = Get-AzMockBody @{
        "acr import"              = @{ Output = ""; ExitCode = 0 }
        "acr manifest show"       = @{ Output = "sha256:abc123"; ExitCode = 0 }
        "acr repository show-tags" = @{ Output = "latest"; ExitCode = 0 }
        "aks get-credentials"     = @{ Output = ""; ExitCode = 0 }
        "group update"            = @{ Output = "{}"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile -Overrides @{
        "AZURE_OMNIVEC_INSTANCE_ID" = @{ Output = "test-inst"; ExitCode = 0 }
        "AZURE_AKS_CLUSTER_NAME"   = @{ Output = "test-aks"; ExitCode = 0 }
        "AZURE_ACR_LOGIN_SERVER"   = @{ Output = "testacr.azurecr.io"; ExitCode = 0 }
        "AZURE_ACR_NAME"           = @{ Output = "testacr"; ExitCode = 0 }
        "AZURE_COSMOS_ENDPOINT"    = @{ Output = "https://test.documents.azure.com"; ExitCode = 0 }
        "AZURE_IDENTITY_CLIENT_ID" = @{ Output = "client-123"; ExitCode = 0 }
        "AZURE_RESOURCE_GROUP"     = @{ Output = "rg-test"; ExitCode = 0 }
        "AZURE_ENABLE_BLOB_SOURCE" = @{ Output = "false"; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "postprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = "test-env"; AZURE_LOCATION = "eastus2"; HOME = $HOME }

    $skipped = $result.Output -match "up to date.*skipping"
    Assert-Test "Scenario 13: Re-run with matching digests — skips import" $skipped

    Remove-TestDir $testDir
}
Test-Scenario13

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 14 — Empty env name — no crash
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario14 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    Initialize-LockDir ""

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "" -Encoding UTF8

    $azBody = Get-AzMockBody @{}
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "preprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = ""; AZURE_LOCATION = "eastus2"; HOME = $HOME }

    $noCrash = $result.Output -notmatch "HOOK_EXCEPTION"
    Assert-Test "Scenario 14: Empty env name — no crash" $noCrash

    Remove-TestDir $testDir
}
Test-Scenario14

# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 15 — RG exists but no tags (partial provision) — proceeds
# ═════════════════════════════════════════════════════════════════════════════

function Test-Scenario15 {
    $testDir = New-TestDir
    $mockBin = Join-Path $testDir "mockbin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    Initialize-LockDir "test-env"

    $envFile = Join-Path $testDir ".env"
    Set-Content $envFile "" -Encoding UTF8

    $azBody = Get-AzMockBody @{
        "group exists"    = @{ Output = "true"; ExitCode = 0 }
        "group show"      = @{ Output = '{"tags":null}'; ExitCode = 0 }
        "account show"    = @{ Output = '{"name":"test","id":"sub-123"}'; ExitCode = 0 }
    }
    Set-Content (Join-Path $mockBin "_az_mock.ps1") $azBody
    Set-Content (Join-Path $mockBin "az.cmd") "@pwsh -NoProfile -File `"%~dp0_az_mock.ps1`" %*" -Encoding ASCII

    $azdBody = Get-AzdMockBody -EnvFilePath $envFile
    Set-Content (Join-Path $mockBin "_azd_mock.ps1") $azdBody
    Set-Content (Join-Path $mockBin "azd.cmd") "@pwsh -NoProfile -File `"%~dp0_azd_mock.ps1`" %*" -Encoding ASCII

    Set-Content (Join-Path $mockBin "kubectl.cmd") "@exit 0" -Encoding ASCII
    Set-Content (Join-Path $mockBin "helm.cmd") "@exit 0" -Encoding ASCII

    $workDir = Join-Path $testDir "repo"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    Initialize-RepoStubs $workDir

    $result = Invoke-HookTest `
        -HookPath (Join-Path $workDir "hooks" "preprovision.ps1") `
        -MockBinDir $mockBin `
        -WorkDir $workDir `
        -EnvVars @{ AZURE_ENV_NAME = "test-env"; AZURE_LOCATION = "eastus2"; HOME = $HOME }

    $passed = $result.ExitCode -eq 0 -and $result.Output -match "Existing deployment detected"
    Assert-Test "Scenario 15: RG exists, no tags — proceeds to Bicep" $passed

    Remove-TestDir $testDir
}
Test-Scenario15

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Results: $PassCount passed, $FailCount failed (of $($PassCount+$FailCount) total)" -ForegroundColor $(if ($FailCount -eq 0) { "Green" } else { "Red" })
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

exit $FailCount
