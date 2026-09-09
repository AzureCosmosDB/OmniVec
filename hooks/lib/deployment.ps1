# Shared by the Windows azd hooks; no commands run while sourcing this file.
function Open-DeploymentLock {
    param([string]$Path)
    try {
        # OS ownership is released even if the hook crashes. Keep the file on
        # disk: deleting it after closing would race the next lock owner.
        return [IO.File]::Open($Path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw "Cannot acquire deployment lock '$Path'. Another hook may be running. $($_.Exception.Message)"
    }
}

function Assert-NativeSuccess {
    param([string]$Operation, [int]$ExitCode = $LASTEXITCODE)
    if ($ExitCode -ne 0) { throw "$Operation failed (exit $ExitCode)." }
}

function Assert-SystemPoolConfiguration {
    param([string]$VmSize, [string]$NodeCount)
    if ($VmSize -match '^Standard_B' -or
        ($VmSize -match '^Standard_[A-Za-z]+(\d+)' -and [int]$Matches[1] -lt 4)) {
        throw "AKS system pools require a non-B-series SKU with at least 4 vCPUs. Set OMNIVEC_SYSTEM_NODE_VM_SIZE (for example Standard_D4s_v5); existing pools may require migration."
    }
    if ($NodeCount) {
        $count = 0
        if (-not [int]::TryParse($NodeCount.Trim(), [ref]$count) -or $count -lt 2) {
            throw 'AKS system pools require at least 2 nodes. Set OMNIVEC_SYSTEM_NODE_COUNT to an integer >= 2.'
        }
    }
}

function Apply-KubernetesResource {
    param([string[]]$Arguments)
    $manifest = & kubectl --context $KUBE_CONTEXT --request-timeout=30s @Arguments --dry-run=client -o yaml
    Assert-NativeSuccess 'Generating Kubernetes resource'
    if (-not $manifest) { throw 'kubectl produced an empty resource manifest.' }
    $manifest | & kubectl --context $KUBE_CONTEXT --request-timeout=30s apply -f -
    Assert-NativeSuccess 'Applying Kubernetes resource'
}

function Test-DeploymentsReady {
    param([string]$Json)
    try {
        $deployments = $Json | ConvertFrom-Json -ErrorAction Stop
        if (@($deployments.items).Count -eq 0) { return $false }
        foreach ($deployment in $deployments.items) {
            $desired = if ($null -eq $deployment.spec.replicas) { 1 } else { [int]$deployment.spec.replicas }
            if ($deployment.status.observedGeneration -lt $deployment.metadata.generation) { return $false }
            if ([int]$deployment.status.updatedReplicas -ne $desired -or
                [int]$deployment.status.availableReplicas -ne $desired -or
                [int]$deployment.status.replicas -ne $desired) { return $false }
        }
        return $true
    } catch { return $false }
}

function Wait-ImageImports {
    param([object[]]$Entries, [int]$TimeoutSeconds = 900)
    if ($Entries.Count -eq 0) { return }
    $jobs = @($Entries | ForEach-Object { $_.Job })
    $jobs | Wait-Job -Timeout $TimeoutSeconds | Out-Null
    $unfinished = @($jobs | Where-Object { $_.State -ne 'Completed' })
    if ($unfinished.Count -gt 0) {
        $jobs | Stop-Job
        $jobs | Remove-Job -Force
        throw "Image imports failed or exceeded ${TimeoutSeconds}s. Check ACR task status before retrying."
    }

}

function Invoke-AcrImport {
    $job = Start-Job -ArgumentList (,@($args)) -ScriptBlock {
        param($Arguments)
        $output = & az acr import @Arguments 2>&1
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    }
    Wait-ImageImports -Entries @(@{ Job = $job })
    try {
        $result = Receive-Job $job -ErrorAction Stop
        Set-Variable -Name LASTEXITCODE -Scope 1 -Value $result.ExitCode
        $result.Output
    } finally {
        Remove-Job $job -Force
    }
}
