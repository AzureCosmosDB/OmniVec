param(
    [switch]$CodeOnly,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AzdArguments
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not (Get-Command azd -ErrorAction SilentlyContinue)) {
    throw 'Azure Developer CLI (azd) is required. See https://aka.ms/azd-install'
}
if ((Test-Path "$root\terraform\terraform.tfstate") -or
    (Test-Path "$root\terraform\terraform.tfstate.d")) {
    throw 'Terraform state detected. Review migration to azd before deploying; refusing to provision a second stack.'
}

Push-Location $root
try {
    if ($CodeOnly) {
        Write-Host 'Deploying application images and Helm release only (skipping Bicep provisioning).'
        & azd hooks run postprovision @AzdArguments
    } else {
        Write-Host 'Deploying with azd up (azure.yaml + Bicep + platform hooks).'
        & azd up @AzdArguments
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
