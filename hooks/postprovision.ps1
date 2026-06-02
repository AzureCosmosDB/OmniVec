# OmniVec — postprovision hook (PowerShell)
# Pushes images to ACR, configures AKS, creates K8s secrets, deploys via Helm

$ErrorActionPreference = "Stop"

# Refresh PATH (tools installed by preprovision may not be in current PATH)
# Preserve current PATH entries and add any new registry entries
$registryPath = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
$env:Path = $env:Path + ";" + $registryPath

$RootDir = (Resolve-Path "$PSScriptRoot/..").Path

# -- Deployment lock: prevent concurrent postprovision runs --
$lockDir = Join-Path $HOME ".omnivec" "locks"
if (-not (Test-Path $lockDir)) { New-Item -ItemType Directory -Path $lockDir -Force | Out-Null }
$lockFile = Join-Path $lockDir "$env:AZURE_ENV_NAME.post.lock"
@($PID, (hostname)) | Set-Content $lockFile
function Release-PostLock { if (Test-Path $lockFile) { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue } }

try {

Write-Host "`n`e[32m+==========================================+`e[0m"
Write-Host "`e[32m|    OmniVec - Post-provision Setup        |`e[0m"
Write-Host "`e[32m+==========================================+`e[0m"

# -- Load azd environment values (handles both `azd up` and `azd hooks run`) --
function Get-AzdValue {
    param($Key)
    # First check env var (set during azd up flow)
    $val = [System.Environment]::GetEnvironmentVariable($Key)
    if ($val) { return $val }
    # Fallback: read from azd env store
    $ErrorActionPreference = "SilentlyContinue"
    $val = azd env get-value $Key 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($exitCode -eq 0 -and $val -and "$val" -notmatch "ERROR") {
        return "$val".Trim()
    }
    return $null
}

$INSTANCE_ID = Get-AzdValue "AZURE_OMNIVEC_INSTANCE_ID"
$AKS_CLUSTER = Get-AzdValue "AZURE_AKS_CLUSTER_NAME"
$ACR_LOGIN_SERVER = Get-AzdValue "AZURE_ACR_LOGIN_SERVER"
$ACR_NAME = Get-AzdValue "AZURE_ACR_NAME"
$COSMOS_ENDPOINT = Get-AzdValue "AZURE_COSMOS_ENDPOINT"
$IDENTITY_CLIENT_ID = Get-AzdValue "AZURE_IDENTITY_CLIENT_ID"
$RESOURCE_GROUP = Get-AzdValue "AZURE_RESOURCE_GROUP"
$BUILD_MODE = Get-AzdValue "OMNIVEC_BUILD_MODE"
if (-not $BUILD_MODE) {
    # Auto-detect build mode
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $dockerInfo = docker info 2>$null
        if ($LASTEXITCODE -eq 0) { $BUILD_MODE = "docker" } else { $BUILD_MODE = "acr" }
    } else { $BUILD_MODE = "acr" }
}
$ENABLE_BLOB_SOURCE = if (Get-AzdValue "AZURE_ENABLE_BLOB_SOURCE") { Get-AzdValue "AZURE_ENABLE_BLOB_SOURCE" } else { "false" }

$STORAGE_ACCOUNT = Get-AzdValue "AZURE_STORAGE_ACCOUNT_NAME"
$STORAGE_BLOB_ENDPOINT = Get-AzdValue "AZURE_STORAGE_BLOB_ENDPOINT"
$STORAGE_QUEUE_ENDPOINT = Get-AzdValue "AZURE_STORAGE_QUEUE_ENDPOINT"
$SB_ENDPOINT = Get-AzdValue "AZURE_SERVICEBUS_ENDPOINT"
$KEYVAULT_URI = Get-AzdValue "AZURE_KEYVAULT_URI"
$APPINSIGHTS_CS = Get-AzdValue "AZURE_APPINSIGHTS_CONNECTION_STRING"
$LOG_ANALYTICS_WS = Get-AzdValue "AZURE_LOG_ANALYTICS_WORKSPACE_ID"

# Azure rejects PublicIP DNS labels containing reserved trademarks
# (windows, microsoft, azure, xbox, login, bing, apple) with
# DomainNameLabelReserved (400). When the INSTANCE_ID happens to contain
# one of these, fall back to an empty label so the Helm chart skips the
# service.beta.kubernetes.io/azure-dns-label-name annotation and the LB
# is reachable via its public IP instead of an azurewebsites FQDN.
$WEB_DNS_LABEL = $INSTANCE_ID
$DNS_LABEL_RESERVED = ""
$lcId = ([string]$INSTANCE_ID).ToLowerInvariant()
foreach ($_w in @('microsoft','windows','azure','xbox','login','bing','apple')) {
    if ($lcId -like "*$_w*") {
        $WEB_DNS_LABEL = ""
        $DNS_LABEL_RESERVED = $_w
        break
    }
}

# Validate required vars
foreach ($var in @("INSTANCE_ID","AKS_CLUSTER","ACR_LOGIN_SERVER","ACR_NAME","COSMOS_ENDPOINT","IDENTITY_CLIENT_ID","RESOURCE_GROUP")) {
    if (-not (Get-Variable $var -ValueOnly)) {
        Write-Host "`e[31mMissing required output: $var. Run 'azd provision' first.`e[0m"
        exit 1
    }
}

if ($ENABLE_BLOB_SOURCE -eq "true" -and -not $SB_ENDPOINT) {
    Write-Host "`e[31mMissing Service Bus endpoint while blob source is enabled.`e[0m"
    exit 1
}

Write-Host "`n`e[36mConfiguration:`e[0m"
Write-Host "  Instance ID:     $INSTANCE_ID"
Write-Host "  AKS cluster:     $AKS_CLUSTER"
Write-Host "  ACR:             $ACR_LOGIN_SERVER"
Write-Host "  CosmosDB:        $COSMOS_ENDPOINT"
Write-Host "  Blob source:     $ENABLE_BLOB_SOURCE"
if ($ENABLE_BLOB_SOURCE -eq "true") {
    Write-Host "  Storage:         $STORAGE_ACCOUNT"
    Write-Host "  Service Bus:     $SB_ENDPOINT"
}
Write-Host "  Identity:        $IDENTITY_CLIENT_ID"
Write-Host "  Build mode:      $BUILD_MODE"
if ($DNS_LABEL_RESERVED) {
    Write-Host "  `e[33mNote: instance id contains reserved word '$DNS_LABEL_RESERVED'`e[0m"
    Write-Host "  `e[33m      — Azure DNS label disabled; web LB will use IP only.`e[0m"
}

# -- Store config as RG tags (enables cross-machine config sync) --
Write-Host "`n`e[36mSaving config to resource group tags...`e[0m"
$sysVm = Get-AzdValue "OMNIVEC_SYSTEM_NODE_VM_SIZE"
$sysCnt = Get-AzdValue "OMNIVEC_SYSTEM_NODE_COUNT"
$gpuVm = Get-AzdValue "OMNIVEC_GPU_NODE_VM_SIZE"
$gpuCnt = Get-AzdValue "OMNIVEC_GPU_NODE_COUNT"
$meta = Get-AzdValue "OMNIVEC_METADATA_STORE"
$blob = Get-AzdValue "OMNIVEC_ENABLE_BLOB_SOURCE"
$build = Get-AzdValue "OMNIVEC_BUILD_MODE"
az tag update --resource-id (az group show --name $RESOURCE_GROUP --query "id" -o tsv) --operation merge --tags `
    "omnivec-sys-sku=$sysVm" `
    "omnivec-sys-count=$sysCnt" `
    "omnivec-gpu-sku=$gpuVm" `
    "omnivec-gpu-count=$gpuCnt" `
    "omnivec-metadata=$meta" `
    "omnivec-blob=$blob" `
    "omnivec-build=$build" `
    "omnivec-instance=$INSTANCE_ID" 2>$null | Out-Null
Write-Host "  `e[32mConfig saved to RG tags.`e[0m"

# =============================================================================
# PHASE 1: Import or Build images
# =============================================================================

# Shared registry with pre-built images (pull via token)
$SHARED_REGISTRY = "omnivecregistry.azurecr.io"
$SHARED_REGISTRY_USER = "omnivec-pull-token"
$SHARED_REGISTRY_TOKEN = if ($env:OMNIVEC_SHARED_REGISTRY_TOKEN) { $env:OMNIVEC_SHARED_REGISTRY_TOKEN } else { Get-AzdValue "OMNIVEC_SHARED_REGISTRY_TOKEN" }

# Check if we should build or import
$DO_BUILD = if (Get-AzdValue "OMNIVEC_BUILD") { (Get-AzdValue "OMNIVEC_BUILD") -eq "true" } else { $false }
$FORCE_IMPORT = if ($env:OMNIVEC_FORCE_IMPORT) { $env:OMNIVEC_FORCE_IMPORT -eq "true" } else { $false }

# Images to import/build
$IMAGES = @(
    "omnivec-api",
    "omnivec-search",
    "omnivec-web",
    "omnivec-changefeed",
    "omnivec-dotnet-worker",
    "omnivec-agent",
    "docgrok-pipeline-worker",
    "docgrok-router"
)

# Release channel tag — resolved once, used for BOTH import and helm overrides.
# 1. Explicit OMNIVEC_IMAGE_TAG (azd env) wins.
# 2. Auto-detect from current git branch: dev -> dev, main -> stable.
# 3. Fallback -> stable.
$IMG_TAG = Get-AzdValue "OMNIVEC_IMAGE_TAG"
if ([string]::IsNullOrWhiteSpace($IMG_TAG)) {
    $_branch = ""
    try { $_branch = (git -C "$PSScriptRoot\.." rev-parse --abbrev-ref HEAD 2>$null) } catch {}
    switch ($_branch) {
        "dev"  { $IMG_TAG = "dev" }
        "main" { $IMG_TAG = "stable" }
        default { $IMG_TAG = "stable" }
    }
    Write-Host "`e[36mAuto-detected branch '$($_branch -replace '^$','unknown')' -> image tag '$IMG_TAG'`e[0m"
}
# Validate: docker tag = alnum + . _ -
if ($IMG_TAG -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Host "`e[31mERROR: OMNIVEC_IMAGE_TAG='$IMG_TAG' is not a valid image tag.`e[0m"
    Write-Host "`e[31mFix: azd env set OMNIVEC_IMAGE_TAG stable  (or 'dev')`e[0m"
    exit 1
}

function Test-ImageExists {
    param($Name, $Tag)
    try {
        $existing = az acr repository show-tags --name $ACR_NAME --repository $Name --query "[?@ == '$Tag']" -o tsv 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        $result = "$existing".Trim()
        return ($result -eq $Tag)
    } catch {
        return $false
    }
}

function Test-ImageUpToDate {
    param($Name, $Tag)
    try {
        $localDigest = az acr manifest show-metadata --registry $ACR_NAME --name "${Name}:${Tag}" --query "digest" -o tsv 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $localDigest) { return $false }
        $sharedDigest = az acr manifest show-metadata --registry "omnivecregistry" --name "${Name}:${Tag}" --query "digest" -o tsv 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $sharedDigest) { return $false }
        return ($localDigest.Trim() -eq $sharedDigest.Trim())
    } catch {
        return $false
    }
}

# -- Helper: build a single image via docker or ACR --
function Build-Image {
    param($Name, $Dockerfile, $Context, $Tag = "latest")

    if (-not $FORCE_IMPORT -and (Test-ImageExists -Name $Name -Tag $Tag)) {
        Write-Host "  `e[32m${Name}:${Tag} exists, skipping.`e[0m"
        return
    }

    Write-Host "  `e[36mBuilding ${Name}:${Tag}...`e[0m"
    if ($BUILD_MODE -eq "docker") {
        docker build -t "${ACR_LOGIN_SERVER}/${Name}:${Tag}" -f $Dockerfile $Context
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  `e[31mdocker build failed for ${Name}:${Tag}.`e[0m"
            exit 1
        }
        docker push "${ACR_LOGIN_SERVER}/${Name}:${Tag}"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  `e[31mdocker push failed for ${Name}:${Tag}.`e[0m"
            exit 1
        }
    } else {
        az acr build --registry $ACR_NAME --image "${Name}:${Tag}" --file $Dockerfile $Context --no-logs 2>$null
        if ($LASTEXITCODE -ne 0) {
            az acr build --registry $ACR_NAME --image "${Name}:${Tag}" --file $Dockerfile $Context
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  `e[31maz acr build failed for ${Name}:${Tag}.`e[0m"
                exit 1
            }
        }
    }
    Write-Host "  `e[32m${Name}:${Tag} pushed.`e[0m"
}

function Build-AllImages {
    Build-Image -Name "omnivec-api" -Dockerfile "$RootDir/api/Dockerfile" -Context $RootDir -Tag "latest"
    Build-Image -Name "omnivec-search" -Dockerfile "$RootDir/search/Dockerfile" -Context $RootDir -Tag "latest"
    Build-Image -Name "omnivec-web" -Dockerfile "$RootDir/web/Dockerfile" -Context "$RootDir/web/" -Tag "latest"
    Build-Image -Name "omnivec-changefeed" -Dockerfile "$RootDir/connectors/ingestion/dotnet/Dockerfile" -Context "$RootDir/connectors/ingestion/dotnet/" -Tag "latest"
    Build-Image -Name "omnivec-dotnet-worker" -Dockerfile "$RootDir/connectors/worker/dotnet/Dockerfile" -Context "$RootDir/connectors/worker/dotnet/" -Tag "latest"
    Build-Image -Name "omnivec-agent" -Dockerfile "$RootDir/agent/Dockerfile" -Context $RootDir -Tag "latest"

    if (Test-Path "$RootDir/docgrok/pipeline-worker/Dockerfile") {
        Build-Image -Name "docgrok-pipeline-worker" -Dockerfile "$RootDir/docgrok/pipeline-worker/Dockerfile" -Context "$RootDir/docgrok/pipeline-worker/" -Tag "latest"
    }
    if (Test-Path "$RootDir/docgrok/router/Dockerfile") {
        Build-Image -Name "docgrok-router" -Dockerfile "$RootDir/docgrok/router/Dockerfile" -Context "$RootDir/docgrok/router/" -Tag "latest"
    }
}

function Build-MissingImages {
    param([string[]]$Images)

    foreach ($image in $Images) {
        switch ($image) {
            "omnivec-api"             { Build-Image -Name $image -Dockerfile "$RootDir/api/Dockerfile" -Context $RootDir -Tag "latest" }
            "omnivec-search"          { Build-Image -Name $image -Dockerfile "$RootDir/search/Dockerfile" -Context $RootDir -Tag "latest" }
            "omnivec-web"             { Build-Image -Name $image -Dockerfile "$RootDir/web/Dockerfile" -Context "$RootDir/web/" -Tag "latest" }
            "omnivec-changefeed"      { Build-Image -Name $image -Dockerfile "$RootDir/connectors/ingestion/dotnet/Dockerfile" -Context "$RootDir/connectors/ingestion/dotnet/" -Tag "latest" }
            "omnivec-dotnet-worker"   { Build-Image -Name $image -Dockerfile "$RootDir/connectors/worker/dotnet/Dockerfile" -Context "$RootDir/connectors/worker/dotnet/" -Tag "latest" }
            "omnivec-agent"           { Build-Image -Name $image -Dockerfile "$RootDir/agent/Dockerfile" -Context $RootDir -Tag "latest" }
            "docgrok-pipeline-worker" {
                if (Test-Path "$RootDir/docgrok/pipeline-worker/Dockerfile") {
                    Build-Image -Name $image -Dockerfile "$RootDir/docgrok/pipeline-worker/Dockerfile" -Context "$RootDir/docgrok/pipeline-worker/" -Tag "latest"
                } else {
                    Write-Host "  `e[33mSkipping ${image}: source not present in repo.`e[0m"
                }
            }
            "docgrok-router" {
                if (Test-Path "$RootDir/docgrok/router/Dockerfile") {
                    Build-Image -Name $image -Dockerfile "$RootDir/docgrok/router/Dockerfile" -Context "$RootDir/docgrok/router/" -Tag "latest"
                } else {
                    Write-Host "  `e[33mSkipping ${image}: source not present in repo.`e[0m"
                }
            }
        }
    }
}

# Honour OMNIVEC_SKIP_IMPORT — when set, do not overwrite images already
# present in ACR (matches postprovision.sh policy).
$SKIP_IMPORT = Get-AzdValue "OMNIVEC_SKIP_IMPORT"
if (-not $SKIP_IMPORT) { $SKIP_IMPORT = $env:OMNIVEC_SKIP_IMPORT }

$FIRST_IMAGE = $IMAGES[0]
$anonOk = $false
$tokenOk = $false
$authImported = $false

# -- If not explicitly set to build, try import --
if (-not $DO_BUILD -and $SKIP_IMPORT -ne "true" -and $SKIP_IMPORT -ne "1") {
    Write-Host "`n`e[33mPhase 1: Importing pre-built images from shared registry...`e[0m"
    Write-Host "  `e[36mSource: $SHARED_REGISTRY`e[0m"

    # If the first image is already present and not forcing import, skip the
    # auth-test re-import — importing unconditionally here used to clobber
    # locally patched images.
    $authTestCanSkip = (-not $FORCE_IMPORT) -and (Test-ImageExists -Name $FIRST_IMAGE -Tag "latest")

    if ($authTestCanSkip) {
        Write-Host "  `e[32m${FIRST_IMAGE}:latest already present locally, skipping auth test.`e[0m"
        $anonOk = $true
    } else {
    Write-Host "  `e[36mTesting anonymous pull...`e[0m" -NoNewline
    $testResult = az acr import --name $ACR_NAME --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:$IMG_TAG" --image "${FIRST_IMAGE}:latest" --force 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host " `e[32m✓ anonymous pull works`e[0m"
        $anonOk = $true
        $authImported = $true
    } else {
        Write-Host " `e[33m✗ requires auth`e[0m"
        # Try with stored token
        if ($SHARED_REGISTRY_TOKEN) {
            Write-Host "  `e[36mTrying stored token...`e[0m" -NoNewline
            $testResult = az acr import --name $ACR_NAME --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:$IMG_TAG" --image "${FIRST_IMAGE}:latest" --username $SHARED_REGISTRY_USER --password $SHARED_REGISTRY_TOKEN --force 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host " `e[32m✓ token works`e[0m"
                $tokenOk = $true
                $authImported = $true
            } else {
                Write-Host " `e[31m✗ token invalid/expired`e[0m"
            }
        }
        # Prompt for token if nothing worked
        if (-not $tokenOk) {
            Write-Host "  `e[33mRegistry token required for import.`e[0m"
            $newToken = (Read-Host "  Enter token for $SHARED_REGISTRY (or Enter to build from source)")
            # Strip ALL whitespace (paste can inject CR/LF/tabs/spaces). Valid tokens have none.
            $newToken = ("$newToken" -replace '\s', '')
            if ($newToken) {
                $testResult = az acr import --name $ACR_NAME --source "${SHARED_REGISTRY}/${FIRST_IMAGE}:$IMG_TAG" --image "${FIRST_IMAGE}:latest" --username $SHARED_REGISTRY_USER --password $newToken --force 2>&1
                if ($LASTEXITCODE -eq 0) {
                    $SHARED_REGISTRY_TOKEN = $newToken
                    azd env set OMNIVEC_SHARED_REGISTRY_TOKEN $newToken 2>$null
                    Write-Host "  `e[32mToken valid — saved for future use.`e[0m"
                    $tokenOk = $true
                    $authImported = $true
                } else {
                    Write-Host "  `e[31mToken invalid. Will build from source.`e[0m"
                }
            }
        }
    }
    }
} elseif ($SKIP_IMPORT -eq "true" -or $SKIP_IMPORT -eq "1") {
    Write-Host "`n`e[33mPhase 1: Skipping image import (OMNIVEC_SKIP_IMPORT=true).`e[0m"
    Write-Host "  `e[36mUsing images already present in $ACR_NAME.`e[0m"
    # Treat as imported so we do not fall through to build-from-source.
    $anonOk = $true
}

if ($DO_BUILD -or (-not $anonOk -and -not $tokenOk -and $SKIP_IMPORT -ne "true" -and $SKIP_IMPORT -ne "1")) {
    $DO_BUILD = $true
}

if ($DO_BUILD) {
    # BUILD MODE: Build images from source
    Write-Host "`n`e[33mPhase 1: Building images from source...`e[0m"

    Build-AllImages
    $script:imagesChanged = $true

    Write-Host "`e[32mAll images built and pushed.`e[0m"
} else {
    # IMPORT MODE: Token validated, proceed with parallel imports
    Write-Host "  `e[36mTo build from source instead: azd env set OMNIVEC_BUILD true`e[0m"

    $importCount = 0
    $skipCount = 0

    # Import images in parallel for speed (each az acr import takes 30-120s)
    $importJobs = @()
    $imagesToImport = @()

    foreach ($image in $IMAGES) {
        # First image was handled by the auth test above
        if ($image -eq $FIRST_IMAGE) {
            if ($authImported) {
                Write-Host "  `e[32m${image}:latest already imported (auth test).`e[0m"
                $importCount++
            } else {
                Write-Host "  `e[32m${image}:latest already present locally, preserving (auth test).`e[0m"
                $skipCount++
            }
            continue
        }
        if (-not $FORCE_IMPORT -and (Test-ImageExists -Name $image -Tag "latest")) {
            # Default policy: local image wins. Re-import only when the user
            # explicitly sets OMNIVEC_FORCE_IMPORT=true. This prevents the
            # shared-registry :latest (which may lag behind hotfixes) from
            # clobbering locally built / patched images on every azd up.
            Write-Host "  `e[32m${image}:latest already present locally, preserving (set OMNIVEC_FORCE_IMPORT=true to overwrite).`e[0m"
            $skipCount++
            continue
        }

        Write-Host "  `e[36mImporting ${image}:$IMG_TAG as :latest...`e[0m"
        $imagesToImport += $image

        $job = Start-Job -ScriptBlock {
            param($ACR, $SHARED, $IMG, $TAG, $USER, $TOKEN)
            $authArgs = @()
            if ($TOKEN) { $authArgs = @("--username", $USER, "--password", $TOKEN) }
            $importError = az acr import --name $ACR --source "${SHARED}/${IMG}:${TAG}" --image "${IMG}:latest" @authArgs --force 2>&1
            if ($LASTEXITCODE -eq 0) { return "OK" }
            # Retry once on transient errors
            if ($importError -notmatch "unauthorized|authentication|401|not found|does not exist|InvalidHostName|could not be resolved") {
                Start-Sleep -Seconds 2
                az acr import --name $ACR --source "${SHARED}/${IMG}:${TAG}" --image "${IMG}:latest" @authArgs --force 2>&1
                if ($LASTEXITCODE -eq 0) { return "OK" }
            }
            return "FAIL: $importError"
        } -ArgumentList $ACR_NAME, $SHARED_REGISTRY, $image, $IMG_TAG, $SHARED_REGISTRY_USER, $SHARED_REGISTRY_TOKEN

        $importJobs += @{ Image = $image; Job = $job }
    }

    # Wait for all imports to finish
    if ($importJobs.Count -gt 0) {
        $importJobs | ForEach-Object { $_.Job } | Wait-Job | Out-Null
    }

    # Report results
    foreach ($entry in $importJobs) {
        $result = Receive-Job $entry.Job
        Remove-Job $entry.Job
        if ($result -eq "OK") {
            Write-Host "  `e[32m$($entry.Image):latest (from $IMG_TAG) imported.`e[0m"
            $importCount++
        } else {
            Write-Host "  `e[31m$($entry.Image):latest (from $IMG_TAG) import FAILED`e[0m"
            Write-Host "  `e[31m$result`e[0m"
        }
    }

    Write-Host "`e[32mImage import complete: $importCount imported, $skipCount skipped.`e[0m"
    $script:imagesChanged = $importCount -gt 0

    # If import yielded no usable images, auto-fallback to source builds
    $totalAvailable = $importCount + $skipCount
    if ($totalAvailable -eq 0) {
        Write-Host "`n`e[33mImport provided no usable images. Falling back to source build mode...`e[0m"
        Build-AllImages
        $script:imagesChanged = $true
    }
}

# -- Final image check: verify all required images exist, build any missing --
Write-Host "`n`e[33mVerifying all required images exist in ACR...`e[0m"
$missingImages = @()
foreach ($image in $IMAGES) {
    if (-not (Test-ImageExists -Name $image -Tag "latest")) {
        Write-Host "  `e[31mMISSING: ${image}:latest`e[0m"
        $missingImages += $image
    } else {
        Write-Host "  `e[32mOK: ${image}:latest`e[0m"
    }
}

if ($missingImages.Count -gt 0) {
    Write-Host "`n`e[33mBuilding missing images from source...`e[0m"
    Build-MissingImages -Images $missingImages

    $stillMissing = @()
    foreach ($image in $missingImages) {
        if (-not (Test-ImageExists -Name $image -Tag "latest")) {
            $stillMissing += $image
        }
    }
    if ($stillMissing.Count -gt 0) {
        Write-Host "`n`e[31mERROR: Required images are still missing after build attempt: $($stillMissing -join ', ')`e[0m"
        Write-Host "  Ensure docgrok source exists in-repo, then re-run: azd hooks run postprovision"
        exit 1
    }
    Write-Host "`e[32mMissing images built and verified.`e[0m"
} else {
    Write-Host "`e[32mAll required images present in ACR.`e[0m"
}

# =============================================================================
# PHASE 2: Get AKS credentials
# =============================================================================

Write-Host "`n`e[33mPhase 2: Getting AKS credentials...`e[0m"
$KUBE_CONTEXT = $AKS_CLUSTER

# Use a separate kubeconfig to avoid overwriting user's default context
$OMNIVEC_KUBECONFIG = Join-Path $HOME ".kube" "omnivec-$env:AZURE_ENV_NAME"
$env:KUBECONFIG = $OMNIVEC_KUBECONFIG

az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --file $OMNIVEC_KUBECONFIG --overwrite-existing
if ($LASTEXITCODE -ne 0) {
    Write-Host "`e[31mFailed to fetch AKS credentials for cluster $AKS_CLUSTER`e[0m"
    exit 1
}
Write-Host "`e[32mConnected to AKS cluster: $AKS_CLUSTER`e[0m"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

Write-Host "`n`e[33mPhase 3: Creating namespaces and secrets...`e[0m"

kubectl --context $KUBE_CONTEXT create namespace omnivec --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
if ($LASTEXITCODE -ne 0) { Write-Host "`e[31mFailed to create namespace omnivec`e[0m"; exit 1 }
kubectl --context $KUBE_CONTEXT create namespace docgrok --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
if ($LASTEXITCODE -ne 0) { Write-Host "`e[31mFailed to create namespace docgrok`e[0m"; exit 1 }
kubectl --context $KUBE_CONTEXT label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite | Out-Null
kubectl --context $KUBE_CONTEXT annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite | Out-Null

if ($ENABLE_BLOB_SOURCE -eq "true") {
    kubectl --context $KUBE_CONTEXT create secret generic omnivec-storage `
        --namespace omnivec `
        --from-literal=account-name="$STORAGE_ACCOUNT" `
        --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" `
        --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
    Write-Host "  `e[32momnivec-storage secret created.`e[0m"
}

# Agent internal token secret (used for agent <-> API service-to-service auth)
$AGENT_INTERNAL_TOKEN = Get-AzdValue "OMNIVEC_AGENT_INTERNAL_TOKEN"
if ([string]::IsNullOrWhiteSpace($AGENT_INTERNAL_TOKEN)) {
    # Use 48 bytes so the stripped-alnum base64 is reliably >= 44 chars.
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $tokenCandidate = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]','')
    if ($tokenCandidate.Length -lt 44) {
        # Extremely unlikely; pad with another draw to guarantee length.
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $tokenCandidate += ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]','')
    }
    $AGENT_INTERNAL_TOKEN = $tokenCandidate.Substring(0, [Math]::Min(44, $tokenCandidate.Length))
    azd env set OMNIVEC_AGENT_INTERNAL_TOKEN $AGENT_INTERNAL_TOKEN | Out-Null
}
kubectl --context $KUBE_CONTEXT create secret generic omnivec-agent-internal `
    --namespace omnivec `
    --from-literal=token="$AGENT_INTERNAL_TOKEN" `
    --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
Write-Host "  `e[32momnivec-agent-internal secret created.`e[0m"

Write-Host "`e[32mNamespaces and secrets created.`e[0m"

# =============================================================================
# PHASE 4: Deploy with Helm
# =============================================================================

Write-Host "`n`e[33mPhase 4: Deploying OmniVec via Helm...`e[0m"

# Resolve helm chart dependencies — skip if already up to date
$chartDir = "$RootDir/helm/omnivec"
$lockFile = "$chartDir/Chart.lock"
$lockHashFile = "$chartDir/charts/.lock-hash"
$currentHash = ""
if (Test-Path $lockFile) {
    $currentHash = (Get-FileHash $lockFile -Algorithm SHA256).Hash
}
$cachedHash = ""
if (Test-Path $lockHashFile) {
    $cachedHash = (Get-Content $lockHashFile -Raw).Trim()
}
if ($currentHash -and $currentHash -eq $cachedHash) {
    Write-Host "  `e[32mHelm dependencies up to date, skipping.`e[0m"
} else {
    Write-Host "  `e[36mResolving helm dependencies...`e[0m"
    helm dependency build $chartDir 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  `e[31mhelm dependency build failed.`e[0m"
        exit 1
    }
    if ($currentHash) {
        $currentHash | Set-Content $lockHashFile -NoNewline
    }
}

# Generate admin token if not already set
$ADMIN_TOKEN = Get-AzdValue "OMNIVEC_ADMIN_TOKEN"
if (-not $ADMIN_TOKEN) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $ADMIN_TOKEN = [Convert]::ToBase64String($bytes) -replace '[+/=]','' | ForEach-Object { $_.Substring(0, [Math]::Min(44, $_.Length)) }
    azd env set OMNIVEC_ADMIN_TOKEN $ADMIN_TOKEN
    Write-Host "  `e[32mGenerated new admin token.`e[0m"
} else {
    Write-Host "  `e[32mUsing existing admin token.`e[0m"
}

# Generate search-service bootstrap + s2s tokens (distinct from admin token)
$SEARCH_BOOTSTRAP_TOKEN = Get-AzdValue "OMNIVEC_SEARCH_TOKEN"
if (-not $SEARCH_BOOTSTRAP_TOKEN) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $SEARCH_BOOTSTRAP_TOKEN = [Convert]::ToBase64String($bytes) -replace '[+/=]','' | ForEach-Object { $_.Substring(0, [Math]::Min(44, $_.Length)) }
    azd env set OMNIVEC_SEARCH_TOKEN $SEARCH_BOOTSTRAP_TOKEN
    Write-Host "  `e[32mGenerated new search bootstrap token.`e[0m"
}
$SEARCH_INTERNAL_TOKEN = Get-AzdValue "SEARCH_INTERNAL_TOKEN"
if (-not $SEARCH_INTERNAL_TOKEN) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $SEARCH_INTERNAL_TOKEN = [Convert]::ToBase64String($bytes) -replace '[+/=]','' | ForEach-Object { $_.Substring(0, [Math]::Min(44, $_.Length)) }
    azd env set SEARCH_INTERNAL_TOKEN $SEARCH_INTERNAL_TOKEN
    Write-Host "  `e[32mGenerated new search internal token.`e[0m"
}

$IMAGE_TAG = "latest"

$helmArgs = @(
    "upgrade", "--install", "omnivec", "$RootDir/helm/omnivec",
    "--namespace", "omnivec",
    "--set", "global.imageRegistry=$ACR_LOGIN_SERVER",
    "--set", "azure.workloadIdentity.clientId=$IDENTITY_CLIENT_ID",
    "--set", "azure.cosmos.endpoint=$COSMOS_ENDPOINT",
    "--set", "api.image.tag=$IMAGE_TAG",
    "--set", "controller.image.tag=$IMAGE_TAG",
    "--set", "web.image.tag=$IMAGE_TAG",
    "--set", "changefeed.image.tag=$IMAGE_TAG",
    "--set", "docgrok.global.imageRegistry=$ACR_LOGIN_SERVER",
    "--set", "docgrok.azure.workloadIdentity.clientId=$IDENTITY_CLIENT_ID",
    "--set", "docgrok.azure.cosmos.endpoint=$COSMOS_ENDPOINT",
    "--set", "docgrok.azure.cosmos.database=omnivec",
    "--set", "docgrok.azure.cosmos.container=metadata",
    "--set", "docgrok.docgrok.image.tag=$IMAGE_TAG",
    "--set", "api.adminToken=$ADMIN_TOKEN",
    "--set", "search.image.tag=$IMAGE_TAG",
    "--set", "search.bootstrapToken=$SEARCH_BOOTSTRAP_TOKEN",
    "--set", "search.internalToken=$SEARCH_INTERNAL_TOKEN",
    "--set", "dotnetWorker.enabled=true",
    "--set", "web.service.dnsLabel=$WEB_DNS_LABEL"
)

if ($KEYVAULT_URI) {
    $helmArgs += @(
        "--set", "azure.keyVault.uri=$KEYVAULT_URI"
    )
}

if ($APPINSIGHTS_CS) {
    $helmArgs += @(
        "--set", "azure.appInsights.connectionString=$APPINSIGHTS_CS"
    )
}

if ($LOG_ANALYTICS_WS) {
    $helmArgs += @(
        "--set", "azure.appInsights.workspaceId=$LOG_ANALYTICS_WS"
    )
}

if ($SB_ENDPOINT) {
    $helmArgs += @(
        "--set", "azure.serviceBus.namespace=$SB_ENDPOINT"
    )
}

if ($ENABLE_BLOB_SOURCE -eq "true") {
    $helmArgs += @(
        "--set", "azure.storage.accountName=$STORAGE_ACCOUNT",
        "--set", "azure.storage.blobEndpoint=$STORAGE_BLOB_ENDPOINT",
        "--set", "blobIngestor.enabled=true"
    )
} else {
    $helmArgs += @("--set", "blobIngestor.enabled=false")
}

# Image tags are NOT overridden here — postprovision imports every image
# into the env-specific ACR tagged :latest (from OMNIVEC_IMAGE_TAG / branch),
# so the default values.yaml (image.tag: latest) resolves for every service.

$helmArgs += @("--kube-context", $KUBE_CONTEXT, "--kubeconfig", $OMNIVEC_KUBECONFIG, "--wait", "--timeout", "10m")
# Intentionally NO --atomic: on failure, --atomic runs `helm uninstall`, which
# strips the release metadata but can leave Deployments/Services behind (they
# have finalizers or take time to delete). Next run sees "release not found"
# + orphaned resources → fresh install conflicts on AlreadyExists → --atomic
# times out → uninstall again → infinite loop.

# Detect stuck Helm release (pending-install / pending-upgrade from interrupted deploy)
$helmStatus = helm status omnivec -n omnivec --kube-context $KUBE_CONTEXT --kubeconfig $OMNIVEC_KUBECONFIG -o json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
$helmState = if ($helmStatus -and $helmStatus.info) { $helmStatus.info.status } else { "" }
if ($helmStatus -and $helmStatus.info -and $helmStatus.info.status -match "^pending-") {
    Write-Host "`e[33mDetected stuck Helm release (status: $($helmStatus.info.status)). Rolling back...`e[0m"
    helm rollback omnivec -n omnivec --kube-context $KUBE_CONTEXT --kubeconfig $OMNIVEC_KUBECONFIG 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`e[33mRollback failed — uninstalling stuck release...`e[0m"
        helm uninstall omnivec -n omnivec --kube-context $KUBE_CONTEXT --kubeconfig $OMNIVEC_KUBECONFIG 2>$null
        $helmState = ""
    }
    Write-Host "`e[32mStuck release cleared. Proceeding with fresh deploy.`e[0m"
}

# If no helm release exists but resources do (orphaned from a prior --atomic
# uninstall), relabel them with Helm ownership so `helm install` can adopt
# them instead of failing with AlreadyExists.
if (-not $helmState) {
    $existing = kubectl --context $KUBE_CONTEXT get deploy -n omnivec -o name 2>$null | Select-Object -First 1
    if ($existing) {
        Write-Host "`e[33mNo Helm release found but resources exist in omnivec ns — adopting them for Helm ownership...`e[0m"
        foreach ($kind in @('deploy','svc','sa','cm','secret','hpa','ingress')) {
            $resources = kubectl --context $KUBE_CONTEXT get $kind -n omnivec -o name 2>$null
            foreach ($res in $resources) {
                if (-not $res) { continue }
                if ($res -eq 'secret/omnivec-storage') { continue }
                if ($res -like 'secret/sh.helm.*') { continue }
                if ($res -like 'secret/default-token-*') { continue }
                kubectl --context $KUBE_CONTEXT annotate $res -n omnivec --overwrite `
                    meta.helm.sh/release-name=omnivec `
                    meta.helm.sh/release-namespace=omnivec 2>$null | Out-Null
                kubectl --context $KUBE_CONTEXT label $res -n omnivec --overwrite `
                    app.kubernetes.io/managed-by=Helm 2>$null | Out-Null
            }
        }
        Write-Host "`e[32mAdoption annotations applied — helm install will take ownership.`e[0m"
    }
}

# ── Skip helm upgrade if nothing has changed ────────────────────────────────
# Rationale: helm upgrade --install --wait takes 1-2 minutes even
# when the computed manifest is identical to the live one. Skip when:
#   1. release is currently 'deployed' (healthy, not pending/failed),
#   2. no images were imported/rebuilt this run ($script:imagesChanged is false),
#   3. helm args fingerprint matches the one cached after the last successful deploy,
#   4. all deployments in the omnivec namespace have >=1 available replica.
# Set $env:OMNIVEC_FORCE_HELM = 'true' to bypass.
$fingerprintFile = "$chartDir/.last-deploy-fingerprint"
# Fingerprint captures everything that determines the rendered manifest:
#   - the helm args (values + --set) we're about to pass in
#   - every file under the chart dir (templates, values.yaml, Chart.yaml,
#     Chart.lock, built subcharts) — so template edits invalidate the cache.
$currentFp = ""
try {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $argsHash = [BitConverter]::ToString($sha.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes(($helmArgs -join "`n"))
    )) -replace '-',''
    $chartHashes = Get-ChildItem -Path $chartDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.last-deploy-fingerprint' } |
        Sort-Object FullName |
        ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash }
    $combined = $argsHash + "`n" + ($chartHashes -join "`n")
    $currentFp = [BitConverter]::ToString($sha.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($combined)
    )) -replace '-',''
} catch {
    $currentFp = ""
}
$cachedFp = ""
if (Test-Path $fingerprintFile) { $cachedFp = (Get-Content $fingerprintFile -Raw).Trim() }

$skipHelm = $false
if ($env:OMNIVEC_FORCE_HELM -ne "true" `
    -and -not $script:imagesChanged `
    -and $helmState -eq "deployed" `
    -and $currentFp -and $currentFp -eq $cachedFp) {
    $unavail = kubectl --context $KUBE_CONTEXT get deploy -n omnivec -o jsonpath='{range .items[?(@.status.availableReplicas==0)]}{.metadata.name}{"\n"}{end}' 2>$null
    if ($LASTEXITCODE -eq 0 -and -not $unavail) {
        $skipHelm = $true
    }
}

if ($skipHelm) {
    Write-Host "  `e[32mNo image/config changes detected and cluster is healthy — skipping helm upgrade.`e[0m"
    Write-Host "  `e[36m(Set `$env:OMNIVEC_FORCE_HELM='true' to force a redeploy.)`e[0m"
} else {
    # Retry helm on transient ARM / Kubernetes errors (mirrors retry_run in sh).
    $maxAttempts = if ($env:OMNIVEC_RETRY_ATTEMPTS) { [int]$env:OMNIVEC_RETRY_ATTEMPTS } else { 4 }
    $baseSec = if ($env:OMNIVEC_RETRY_BASE_SEC) { [int]$env:OMNIVEC_RETRY_BASE_SEC } else { 5 }
    $transientPatterns = @(
        '429','throttl','Too Many Requests','ServiceBusy','ServerBusy',
        'RequestTimeout','OperationTimedOut','503','502','504',
        'Service Unavailable','Temporary failure','Connection reset',
        'TLS handshake','InternalServerError','i/o timeout',
        'context deadline exceeded','no such host','dial tcp'
    )
    $helmRc = 1
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        if ($attempt -gt 1) {
            $sleep = $baseSec * [Math]::Pow(2, $attempt - 2)
            Write-Host "  `e[33m[helm-deploy] attempt $attempt/$maxAttempts after $([int]$sleep)s backoff...`e[0m"
            Start-Sleep -Seconds ([int]$sleep)
        }
        $helmOutput = (& helm @helmArgs 2>&1 | Out-String)
        $helmRc = $LASTEXITCODE
        Write-Host $helmOutput
        if ($helmRc -eq 0) { break }
        $isTransient = $false
        foreach ($pat in $transientPatterns) {
            if ($helmOutput -match [regex]::Escape($pat)) { $isTransient = $true; break }
        }
        if (-not $isTransient) {
            Write-Host "  `e[31m[helm-deploy] non-transient failure — not retrying.`e[0m"
            break
        }
        Write-Host "  `e[33m[helm-deploy] transient failure detected — will retry.`e[0m"
    }
    if ($helmRc -ne 0) {
        Write-Host "`e[31mHelm deploy failed. Collecting pod diagnostics...`e[0m"
        kubectl --context $KUBE_CONTEXT get pods -n omnivec -o wide

        $problemPods = kubectl --context $KUBE_CONTEXT get pods -n omnivec --no-headers 2>$null | `
            Where-Object { $_ -match "ImagePullBackOff|ErrImagePull|CrashLoopBackOff|Error|Pending" }

        foreach ($line in $problemPods) {
            $parts = ($line -replace '\s+', ' ').Trim().Split(' ')
            if ($parts.Count -lt 3) { continue }
            $podName = $parts[0]
            $status = $parts[2]
            Write-Host "`n`e[33m=== $podName ($status) ===`e[0m"
            kubectl --context $KUBE_CONTEXT describe pod $podName -n omnivec | Select-String -Pattern "Events:" -Context 0,60
            kubectl --context $KUBE_CONTEXT logs $podName -n omnivec --tail=80 2>$null
        }
        exit 1
    }
    # Cache fingerprint only on success so a failed run doesn't poison future skips
    if ($currentFp) { $currentFp | Set-Content $fingerprintFile -NoNewline }
}

Write-Host "`e[32mHelm deployment complete.`e[0m"

# Force pod restart if images were updated (tag is always 'latest', so Helm won't restart on its own)
if ($script:imagesChanged) {
    Write-Host "`n`e[33mImages updated — restarting pods to pull new images...`e[0m"
    kubectl --context $KUBE_CONTEXT rollout restart deployment -n omnivec 2>$null
    kubectl --context $KUBE_CONTEXT rollout status deployment/omnivec-api -n omnivec --timeout=5m 2>$null
    Write-Host "`e[32mPods restarted with new images.`e[0m"
}

# =============================================================================
# PHASE 5: Verify and print info
# =============================================================================

Write-Host "`n`e[33mPhase 5: Verifying deployment...`e[0m"

Write-Host "`n`e[36mOmniVec pods:`e[0m"
kubectl --context $KUBE_CONTEXT get pods -n omnivec --no-headers 2>$null

Write-Host "`n`e[36mDocGrok pods:`e[0m"
kubectl --context $KUBE_CONTEXT get pods -n omnivec -l app=docgrok --no-headers 2>$null
kubectl --context $KUBE_CONTEXT get pods -n omnivec -l app=docgrok-controller --no-headers 2>$null

# Wait for external IP
Write-Host "`n`e[33mWaiting for external IP...`e[0m"
$externalIp = $null
for ($i = 0; $i -lt 30; $i++) {
    $externalIp = kubectl --context $KUBE_CONTEXT get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($externalIp) { break }
    Start-Sleep -Seconds 5
}

kubectl --context $KUBE_CONTEXT rollout status deployment/omnivec-api -n omnivec --timeout=5m 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`e[31mAPI deployment did not become ready.`e[0m"
    exit 1
}

Write-Host ""
Write-Host "`e[32m+==========================================+`e[0m"
Write-Host "`e[32m|         Deployment Successful!           |`e[0m"
Write-Host "`e[32m+==========================================+`e[0m"
Write-Host ""
Write-Host "  Instance ID:   `e[36m$INSTANCE_ID`e[0m"
Write-Host "  Environment:   `e[36m$($env:AZURE_ENV_NAME)`e[0m"
Write-Host "  AKS Cluster:   `e[36m$AKS_CLUSTER`e[0m"
Write-Host "  ACR Registry:  `e[36m$ACR_LOGIN_SERVER`e[0m"
Write-Host "  CosmosDB:      `e[36m$COSMOS_ENDPOINT`e[0m"

Write-Host "  Admin Token:   `e[36m$ADMIN_TOKEN`e[0m"

$LOCATION = $env:AZURE_LOCATION
if (-not $LOCATION) { $LOCATION = Get-AzdValue "AZURE_LOCATION" }
if (-not $LOCATION) { $LOCATION = "eastus2" }
$FQDN = "${INSTANCE_ID}.${LOCATION}.cloudapp.azure.com"

# Persist the OmniVec server URL into the azd env so subsequent CLI
# invocations / tooling can pick it up without re-deriving from the cluster.
$OMNIVEC_API_URL = "http://$FQDN"
azd env set OMNIVEC_API_URL $OMNIVEC_API_URL 2>$null | Out-Null
azd env set OMNIVEC_UI_URL  "$OMNIVEC_API_URL/ui" 2>$null | Out-Null
if ($externalIp) {
    azd env set OMNIVEC_API_IP "http://$externalIp" 2>$null | Out-Null
}

if ($externalIp) {
    Write-Host ""
    Write-Host "  OmniVec FQDN:  `e[36mhttp://${FQDN}/ui`e[0m"
    Write-Host "  OmniVec IP:    `e[36mhttp://${externalIp}/ui`e[0m"
    Write-Host "  Health Check:  `e[36mhttp://${FQDN}/health`e[0m"
} else {
    Write-Host ""
    Write-Host "  `e[33mExternal IP not yet assigned. Check with:`e[0m"
    Write-Host "  kubectl get svc omnivec-web -n omnivec"
}
Write-Host ""

} finally {
    Release-PostLock
}
