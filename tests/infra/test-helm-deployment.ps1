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
    if ($source -notmatch 'OMNIVEC_SHAREPOINT_ENABLED' -or $source -notmatch 'sharepointWatcher' -or $source -notmatch 'dependency build.*--skip-refresh') {
        throw "$hook must wire the persisted watcher setting and package local charts without refreshing unrelated repositories"
    }
    Write-Host "OK $hook wires SharePoint and packages charts offline"
}
Write-Host '8 Helm deployment checks passed'
exit 0
