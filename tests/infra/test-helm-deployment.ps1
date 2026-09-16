$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$chart = "$root\helm\omnivec"
# helm template is entirely local; never connect to a cluster.
$rendered = & helm template omnivec $chart --set sharepointWatcher.enabled=true --set azure.serviceBus.namespace=mock.servicebus.windows.net 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw $rendered }
foreach ($needle in 'name: omnivec-sharepoint-watcher', 'name: omnivec-dotnet-worker', 'Worker__ServiceBusNamespace', 'ChangeFeed__ServiceBusEnabled') {
    if (-not $rendered.Contains($needle)) { throw "Missing deployment wiring: $needle" }
    Write-Host "OK $needle"
}
$result = & helm template omnivec $chart --set sharepointWatcher.enabled=true --set dotnetWorker.enabled=false --set azure.serviceBus.namespace=mock.servicebus.windows.net 2>&1 | Out-String
if ($LASTEXITCODE -eq 0 -or $result -notmatch 'requires dotnetWorker.enabled') {
    throw 'Watcher without worker must fail before provisioning'
}
Write-Host 'OK watcher without worker is rejected'
$result = & helm template omnivec $chart --set sharepointWatcher.enabled=true 2>&1 | Out-String
if ($LASTEXITCODE -eq 0 -or $result -notmatch 'requires azure.serviceBus.namespace') {
    throw 'Watcher without Service Bus must fail before provisioning'
}
Write-Host 'OK watcher without Service Bus is rejected'
foreach ($hook in 'postprovision.ps1','postprovision.sh') {
    $source = Get-Content "$root\hooks\$hook" -Raw
    if ($source -notmatch 'OMNIVEC_SHAREPOINT_ENABLED' -or
        $source -notmatch 'sharepointWatcher' -or
        $source -notmatch 'dependency build.*--skip-refresh' -or
        $source -notmatch '\-\-repository-config' -or
        $source -notmatch '\-\-repository-cache' -or
        $source -notmatch 'pending-install' -or
        $source -notmatch 'rollback omnivec 0' -or
        $source -notmatch 'uninstall omnivec' -or
        $source -notmatch 'public health endpoint' -or
        $source -notmatch 'after 3 attempts' -or
        $source -notmatch 'not printed') {
        throw "$hook must support isolated chart packaging, interrupted-release recovery, resilient ACR probes, health verification, and secret-safe output"
    }
    if ($source -match 'Admin Token:\s+.*\$ADMIN_TOKEN' -or $source -match 'Admin Token:\s+.*\$\{ADMIN_TOKEN\}') {
        throw "$hook must not print the admin token"
    }
    Write-Host "OK $hook hardens retries, recovery, health checks, and secret output"
}
foreach ($hook in 'preprovision.sh','postprovision.sh') {
    $source = Get-Content "$root\hooks\$hook" -Raw
    if ($source -notmatch 'Removing stale' -or $source -notmatch 'kill -0') {
        throw "$hook must automatically recover same-host stale lock directories"
    }
    Write-Host "OK $hook recovers stale same-host locks"
}
foreach ($hook in 'preprovision.ps1','preprovision.sh','postprovision.ps1','postprovision.sh') {
    $source = Get-Content "$root\hooks\$hook" -Raw
    foreach ($setting in 'OMNIVEC_BUILD','OMNIVEC_IMAGE_TAG','OMNIVEC_SHAREPOINT_ENABLED','OMNIVEC_ONELAKE_ICEBERG_ENABLED') {
        if ($source -notmatch $setting) {
            throw "$hook must preserve $setting across recovery"
        }
    }
    Write-Host "OK $hook preserves recovery configuration"
}
Write-Host 'Helm deployment and recovery checks passed'
exit 0
