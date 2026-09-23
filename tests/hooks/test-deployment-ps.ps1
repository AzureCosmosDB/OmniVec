$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$root\hooks\lib\deployment.ps1"
$passed = 0
function Check($condition, $message) {
    if (-not $condition) { throw $message }
    $script:passed++
    Write-Host "OK $message"
}
function Must-Throw([scriptblock]$action, $message) {
    $threw = $false
    try { & $action } catch { $threw = $true }
    Check $threw $message
}

foreach ($path in @('hooks\preprovision.ps1', 'hooks\postprovision.ps1', 'hooks\lib\deployment.ps1')) {
    $errors = $null
    # azure.yaml runs the hooks with pwsh (UTF-8). Match that decoding even
    # when this helper test is launched by the Windows PowerShell 5 runner.
    $text = [IO.File]::ReadAllText("$root\$path", [Text.Encoding]::UTF8)
    [Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errors) | Out-Null
    Check (-not $errors) "$path parses"
}

$lockPath = Join-Path $PSScriptRoot ("lock-test-" + [guid]::NewGuid().ToString('N'))
try {
    $handle = Open-DeploymentLock $lockPath
    Must-Throw { Open-DeploymentLock $lockPath } 'a second writer cannot take an active lock'
    $handle.Dispose()
    $handle = Open-DeploymentLock $lockPath
    Check ($null -ne $handle) 'a released lock can be acquired without deleting its file'
} finally {
    if ($handle) { $handle.Dispose() }
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}

Check (-not (Test-DeploymentsReady '{"items":[]}')) 'empty deployment list is not healthy'
Check (-not (Test-DeploymentsReady '{bad json')) 'invalid deployment response is not healthy'
Must-Throw { Assert-SystemPoolConfiguration Standard_B4ms 2 } 'B-series system pool is rejected before provisioning'
Must-Throw { Assert-SystemPoolConfiguration Standard_D2s_v3 2 } 'undersized system pool VM is rejected'
Must-Throw { Assert-SystemPoolConfiguration Standard_D4s_v5 1 } 'single-node system pool is rejected'
Must-Throw { Assert-SystemPoolConfiguration Standard_D4s_v5 invalid } 'invalid node count is rejected'
Assert-SystemPoolConfiguration Standard_D4s_v5 2
Check $true 'supported quickstart system pool is accepted'
Check (-not (Test-DeploymentsReady '{"items":[{"metadata":{"generation":2},"spec":{"replicas":2},"status":{}}]}')) 'missing availableReplicas is not healthy'
$ready = @{
    metadata = @{ generation = 2 }
    spec = @{ replicas = 2 }
    status = @{ observedGeneration = 2; updatedReplicas = 2; availableReplicas = 2; replicas = 2 }
}
Check (Test-DeploymentsReady (@{items=@($ready)} | ConvertTo-Json -Depth 5)) 'all desired replicas at the current generation are healthy'
$ready.status.updatedReplicas = 1
Check (-not (Test-DeploymentsReady (@{items=@($ready)} | ConvertTo-Json -Depth 5))) 'old available pods cannot mask an incomplete rollout'
$ready.status.updatedReplicas = 2
$ready.status.observedGeneration = 1
Check (-not (Test-DeploymentsReady (@{items=@($ready)} | ConvertTo-Json -Depth 5))) 'unobserved generation cannot be skipped'
$ready.status.observedGeneration = 2

$script:deploymentReads = 0
function kubectl {
    $script:deploymentReads++
    $global:LASTEXITCODE = 0
    if ($script:deploymentReads -eq 1) {
        $pending = $ready.Clone()
        $pending.status = $ready.status.Clone()
        $pending.status.availableReplicas = 1
        @{items=@($pending)} | ConvertTo-Json -Depth 5
    } else {
        @{items=@($ready)} | ConvertTo-Json -Depth 5
    }
}
function Start-Sleep { }
Check (Wait-DeploymentsReady -Context mock -KubeConfig isolated -TimeoutSeconds 1 -PollSeconds 1) 'rollout recovery waits for actual convergence'
Check ($script:deploymentReads -eq 2) 'rollout recovery rechecks deployments after a transient incomplete state'
Remove-Item Function:\Start-Sleep
Remove-Item Function:\kubectl

$capacityHints = Get-DeploymentRemediation 'FailedScheduling: Too many pods'
Check (($capacityHints -join ' ') -match 'Scale the AKS node pool') 'capacity failures include a concrete recovery action'
$imageHints = Get-DeploymentRemediation 'ImagePullBackOff: manifest unknown'
Check (($imageHints -join ' ') -match 'environment ACR') 'image pull failures include registry and identity guidance'
$unknownHints = Get-DeploymentRemediation 'unclassified failure'
Check (($unknownHints -join ' ') -match 'No safe automatic repair matched') 'unknown failures fail closed with manual diagnostic guidance'

& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -Command 'exit 17'
Must-Throw { Assert-NativeSuccess 'native failure' } 'native nonzero exit is fatal'
Assert-NativeSuccess 'success' 0

$script:applyCalled = $false
$script:createExit = 1
$script:applyExit = 0
$KUBE_CONTEXT = 'mock'
function kubectl {
    if ($args -contains 'apply') {
        $script:applyCalled = $true
        $global:LASTEXITCODE = $script:applyExit
    } else {
        $global:LASTEXITCODE = $script:createExit
        'kind: Secret'
    }
}
Must-Throw { Apply-KubernetesResource @('create','secret') } 'failed generator cannot be masked by successful apply'
Check (-not $script:applyCalled) 'failed generated resource is never applied'
$script:createExit = 0
$script:applyExit = 1
Must-Throw { Apply-KubernetesResource @('create','secret') } 'failed secret apply is fatal'
Remove-Item Function:\kubectl

$job = Start-Job { Start-Sleep -Seconds 60 }
$watch = [Diagnostics.Stopwatch]::StartNew()
Must-Throw { Wait-ImageImports -Entries @(@{Job=$job}) -TimeoutSeconds 1 } 'stalled imports time out'
Check ($watch.Elapsed.TotalSeconds -lt 20) 'import timeout is bounded'
Check (-not (Get-Job -Id $job.Id -ErrorAction SilentlyContinue)) 'timed-out import jobs are removed'

# Exercise the actual bounded import wrapper, but inject a fake az into its
# child process so there is no possibility of contacting Azure.
$startJobCommand = Get-Command Start-Job -CommandType Cmdlet
function Start-Job {
    param($ArgumentList, $ScriptBlock)
    & $startJobCommand -ArgumentList $ArgumentList -ScriptBlock $ScriptBlock -InitializationScript {
        function global:az {
            $global:LASTEXITCODE = 23
            $args -join '|'
        }
    }
}
$importOutput = Invoke-AcrImport --name 'registry with spaces' --force
Check ($LASTEXITCODE -eq 23) 'bounded import propagates the native failure code'
Check ($importOutput -eq 'acr|import|--name|registry with spaces|--force') 'bounded import preserves argument boundaries and output'
Remove-Item Function:\Start-Job

# Extract only the build function, never execute a provisioning hook.
$text = [IO.File]::ReadAllText("$root\hooks\postprovision.ps1", [Text.Encoding]::UTF8)
$ast = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$null)
$build = $ast.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Build-Image' }, $true)
Invoke-Expression $build.Extent.Text
function Test-ImageExists { return $true }
function Mark-ImageUpdate { $script:imagesChanged = $true }
function Add-ChangedImage { param($Name) Mark-ImageUpdate }
function az { $global:LASTEXITCODE = 0 }
$script:dockerCalls = @()
function docker { $script:dockerCalls += $args[0]; $global:LASTEXITCODE = 0 }
$DO_BUILD = $true
$FORCE_IMPORT = $false
$BUILD_MODE = 'docker'
$ACR_NAME = 'mock'
$ACR_LOGIN_SERVER = 'mock.invalid'
Build-Image 'test' 'Dockerfile' '.'
Check (($script:dockerCalls -join ',') -eq 'build,push') 'explicit source build replaces an existing tag'
$DO_BUILD = $false
$script:dockerCalls = @()
Build-Image 'test' 'Dockerfile' '.'
Check ($script:dockerCalls.Count -eq 0) 'ordinary deployment preserves an existing image'

try {
    $DO_BUILD = $true
    $BUILD_MODE = 'acr'
    $script:acrExit = 0
    function az {
        $script:acrArguments = @($args)
        $global:LASTEXITCODE = $script:acrExit
    }
    Build-Image 'test' 'Dockerfile' '.'
    Check ($script:acrArguments -contains '--no-logs') 'ACR builds avoid Windows Unicode log-stream failures'
    Check ($script:acrArguments -notcontains '--no-wait') 'ACR builds still wait for the remote build result'
    $script:acrExit = 9
    Must-Throw { Build-Image 'test' 'Dockerfile' '.' } 'ACR build failure remains fatal without streamed logs'
} finally {
    $DO_BUILD = $false
    function az { $global:LASTEXITCODE = 0 }
}

$imageBranch = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq '$DO_BUILD'
}, $true)
$SKIP_IMPORT = 'true'
$IMAGES = @('present','missing')
function Test-ImageExists { param($Name, $Tag) return $Name -eq 'present' }
function Start-Job { throw 'Unexpected import attempted' }
$failedAsExpected = $false
try { Invoke-Expression $imageBranch.Extent.Text } catch {
    $failedAsExpected = $_.Exception.Message -match 'OMNIVEC_SKIP_IMPORT is set'
}
Check $failedAsExpected 'skip-import fails on a missing image without starting imports'
Remove-Item Function:\Start-Job

$source = Get-Content "$root\hooks\postprovision.ps1" -Raw
Check ($source -match 'OMNIVEC_RECOVER_PENDING_HELM' -and
       $source -match 'pending-install' -and $source -match 'helm uninstall omnivec' -and
       $source -match 'helm rollback omnivec 0') 'interrupted Helm recovery is explicit, guarded, and state-specific'
Check ($source -notmatch 'adopt_orphaned_resources|cp -f .*HOME/.kube/config') 'recovery never takes ownership or overwrites the default kubeconfig'
Check ($source -notmatch 'Remove-Item \$lockFile') 'hook cleanup cannot delete Chart.lock'
Check ($source -match 'rollout status deployment -n omnivec') 'all deployments are verified rather than only the API'
Check ($source -match 'Wait-DeploymentsReady') 'a timed-out rollout gets a bounded convergence recovery window'
Check ($source -match 'forcibly closed' -and $source -match 'wsarecv') 'AKS transport resets are classified as transient'
Check ($source -match 'Get-DeploymentRemediation') 'terminal deployment failures print actionable recovery guidance'
Check ($source -match 'src-\$\(\$fingerprint\.Substring\(0,16\)\)') 'source builds use deterministic immutable fingerprint tags'
Check ($source -match 'OMNIVEC_BUILD_CONCURRENCY' -and $source -match '\$concurrency -gt 5') 'parallel source builds have a bounded concurrency limit'
Check ($source -match 'Copy-MinimalBuildTree' -and $source -match 'omnivec-build-') 'source builds use staged minimal contexts'
Check ($source -match 'docgrok\.pipelineWorker\.image\.tag' -and
       $source -match 'dotnetWorker\.image\.tag' -and
       $source -match 'onelakeIcebergWatcher\.image\.tag') 'Helm receives per-component immutable image tags'
Check ($source -match '\$script:immutableSourceBuild' -and
       $source -match '\$script:changedImages') 'immutable source builds avoid blanket restarts and track changed images'

$copyBuildTree = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Copy-MinimalBuildTree'
}, $true)
$getBuildSpec = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-SourceBuildSpec'
}, $true)
Invoke-Expression $copyBuildTree.Extent.Text
Invoke-Expression $getBuildSpec.Extent.Text
$RootDir = $root
$script:buildContextRoot = Join-Path $env:TEMP ("omnivec-context-test-" + [guid]::NewGuid().ToString('N'))
try {
    $apiSpec = Get-SourceBuildSpec -Name 'omnivec-api'
    $apiBytes = (Get-ChildItem $apiSpec.Context -File -Recurse | Measure-Object Length -Sum).Sum
    Check (Test-Path $apiSpec.Dockerfile) 'staged API context contains its Dockerfile'
    Check ($apiSpec.Tag -match '^src-[a-f0-9]{16}$') 'staged API context produces a valid immutable tag'
    Check (-not (Test-Path (Join-Path $apiSpec.Context 'docgrok'))) 'staged API context excludes unrelated repository trees'
    Check ($apiBytes -lt 100MB) 'staged API context remains below 100 MiB'
} finally {
    Remove-Item $script:buildContextRoot -Recurse -Force -ErrorAction SilentlyContinue
    $script:buildContextRoot = $null
}

# Native timeout responses stand in for stalls longer than a minute. Exercise
# the hook's real command/guard and retry loop without sleeping or networking.
$rollout = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.CommandAst] -and
    $n.Extent.Text -match '^kubectl .*rollout status deployment -n omnivec'
}, $true)
$rolloutGuard = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.IfStatementAst] -and
    $n.Extent.StartLineNumber -gt $rollout.Extent.EndLineNumber -and
    $n.Clauses[0].Item1.Extent.Text -eq '$LASTEXITCODE -ne 0'
}, $true)
$script:rolloutArguments = @()
function kubectl {
    if ($args -contains 'rollout') {
        $script:rolloutArguments = @($args)
    }
    $global:LASTEXITCODE = 1
    'error: timed out waiting for the condition (simulated 301s elapsed)'
}
$env:OMNIVEC_ROLLOUT_RECOVERY_SEC = '0'
$rolloutCheck = $rollout.Extent.Text + "`n" + ($rolloutGuard.Extent.Text -replace '\bexit 1\b', 'throw "rollout deadline reached"')
Must-Throw { Invoke-Expression $rolloutCheck } 'a rollout stalled over one minute fails rather than reporting success'
Check (($script:rolloutArguments -contains '--timeout=5m') -and
       ($script:rolloutArguments -contains '--request-timeout=5m')) 'rollout watch and Kubernetes requests both have explicit deadlines'
Remove-Item Env:\OMNIVEC_ROLLOUT_RECOVERY_SEC
Remove-Item Function:\kubectl

$helmLoop = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.ForStatementAst] -and
    $n.Condition.Extent.Text -eq '$attempt -le $maxAttempts'
}, $true)
$maxAttempts = 4
$baseSec = 0
$transientPatterns = @('context deadline exceeded')
$helmArgs = @('upgrade','--install','omnivec','chart','--wait','--timeout','10m')
$script:helmCalls = 0
function helm {
    $script:helmCalls++
    $global:LASTEXITCODE = 1
    'Error: context deadline exceeded (simulated 601s elapsed)'
}
Invoke-Expression $helmLoop.Extent.Text
Check ($script:helmCalls -eq 1 -and $helmRc -ne 0) 'a Helm readiness stall stops after its deadline without repeated upgrades'
Remove-Item Function:\helm
Write-Host "$passed deployment checks passed"
