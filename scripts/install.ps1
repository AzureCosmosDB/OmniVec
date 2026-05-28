<#
.SYNOPSIS
  OmniVec CLI installer for Windows.

.DESCRIPTION
  Downloads the OmniVec CLI for Windows (amd64 or arm64) from the GitHub
  release and installs it to a user-writable directory. No admin required.

.EXAMPLE
  irm https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.ps1 | iex

.EXAMPLE
  $env:OMNIVEC_VERSION = 'v1.1.3'; irm https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.ps1 | iex

.NOTES
  Env vars:
    OMNIVEC_VERSION       Pin specific version (default: latest)
    OMNIVEC_INSTALL_DIR   Install location (default: $env:USERPROFILE\.omnivec\bin)
#>

$ErrorActionPreference = 'Stop'

$repo        = 'AzureCosmosDB/OmniVec'
$version     = if ($env:OMNIVEC_VERSION) { $env:OMNIVEC_VERSION } else { 'latest' }
$installDir  = if ($env:OMNIVEC_INSTALL_DIR) { $env:OMNIVEC_INSTALL_DIR } else { Join-Path $env:USERPROFILE '.omnivec\bin' }

# ---------- detect arch ----------
$arch = switch -Wildcard ($env:PROCESSOR_ARCHITECTURE) {
  'ARM64' { 'arm64'; break }
  default { 'amd64' }
}

# ---------- resolve version ----------
if ($version -eq 'latest') {
  try {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -UseBasicParsing
    $version = $latest.tag_name
  } catch {
    Write-Error "Could not resolve latest release tag. Set `$env:OMNIVEC_VERSION explicitly."
  }
}

$asset = "omnivec-$version-windows-$arch.exe"
$url   = "https://github.com/$repo/releases/download/$version/$asset"

Write-Host "Installing OmniVec CLI $version for windows/$arch" -ForegroundColor Cyan
Write-Host "  source : $url"
Write-Host "  target : $installDir\omnivec.exe"
Write-Host ""

# ---------- download ----------
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$dest = Join-Path $installDir 'omnivec.exe'
try {
  Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
} catch {
  Write-Error "Download failed. Asset may not exist for this platform: $asset"
}

# ---------- unblock (removes Zone.Identifier from MOTW) ----------
try { Unblock-File -Path $dest -ErrorAction SilentlyContinue } catch {}

Write-Host "Installed: $dest" -ForegroundColor Green

# ---------- PATH guidance ----------
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath -split ';' | Where-Object { $_ -eq $installDir })) {
  Write-Host ""
  Write-Host "$installDir is not on your User PATH." -ForegroundColor Yellow
  Write-Host "Adding it now (new shells only — restart your terminal after this)."
  [Environment]::SetEnvironmentVariable('Path', "$userPath;$installDir", 'User')
}

Write-Host ""
& $dest --help 2>$null | Out-Null
if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 2) {
  Write-Host "Verified - try: omnivec --help" -ForegroundColor Green
} else {
  Write-Warning "omnivec installed but did not run cleanly. Inspect: $dest"
}
