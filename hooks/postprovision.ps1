# OmniVec — postprovision hook (PowerShell)
# Pushes images to ACR, configures AKS, creates K8s secrets, deploys via Helm

$ErrorActionPreference = "Stop"

# Refresh PATH (tools installed by preprovision may not be in current PATH)
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

$RootDir = (Resolve-Path "$PSScriptRoot/..").Path

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
    $ErrorActionPreference = "Stop"
    if ($LASTEXITCODE -eq 0 -and $val -and $val -notmatch "^ERROR") {
        return $val.Trim()
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
$BUILD_MODE = if (Get-AzdValue "OMNIVEC_BUILD_MODE") { Get-AzdValue "OMNIVEC_BUILD_MODE" } else { "acr" }
$ENABLE_BLOB_SOURCE = if (Get-AzdValue "AZURE_ENABLE_BLOB_SOURCE") { Get-AzdValue "AZURE_ENABLE_BLOB_SOURCE" } else { "false" }

$STORAGE_ACCOUNT = Get-AzdValue "AZURE_STORAGE_ACCOUNT_NAME"
$STORAGE_BLOB_ENDPOINT = Get-AzdValue "AZURE_STORAGE_BLOB_ENDPOINT"
$STORAGE_QUEUE_ENDPOINT = Get-AzdValue "AZURE_STORAGE_QUEUE_ENDPOINT"
$SB_ENDPOINT = Get-AzdValue "AZURE_SERVICEBUS_ENDPOINT"
$KEYVAULT_URI = Get-AzdValue "AZURE_KEYVAULT_URI"

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
    "omnivec-web",
    "omnivec-changefeed",
    "omnivec-dotnet-worker",
    "docgrok-pipeline-worker",
    "docgrok-router"
)

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
        docker push "${ACR_LOGIN_SERVER}/${Name}:${Tag}"
    } else {
        az acr build --registry $ACR_NAME --image "${Name}:${Tag}" --file $Dockerfile $Context --no-logs 2>$null
        if ($LASTEXITCODE -ne 0) {
            az acr build --registry $ACR_NAME --image "${Name}:${Tag}" --file $Dockerfile $Context
        }
    }
    Write-Host "  `e[32m${Name}:${Tag} pushed.`e[0m"
}

function Build-AllImages {
    Build-Image -Name "omnivec-api" -Dockerfile "$RootDir/api/Dockerfile" -Context $RootDir -Tag "latest"
    Build-Image -Name "omnivec-web" -Dockerfile "$RootDir/web/Dockerfile" -Context "$RootDir/web/" -Tag "latest"
    Build-Image -Name "omnivec-changefeed" -Dockerfile "$RootDir/connectors/ingestion/dotnet/Dockerfile" -Context "$RootDir/connectors/ingestion/dotnet/" -Tag "latest"
    Build-Image -Name "omnivec-dotnet-worker" -Dockerfile "$RootDir/connectors/worker/dotnet/Dockerfile" -Context "$RootDir/connectors/worker/dotnet/" -Tag "latest"

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
            "omnivec-web"             { Build-Image -Name $image -Dockerfile "$RootDir/web/Dockerfile" -Context "$RootDir/web/" -Tag "latest" }
            "omnivec-changefeed"      { Build-Image -Name $image -Dockerfile "$RootDir/connectors/ingestion/dotnet/Dockerfile" -Context "$RootDir/connectors/ingestion/dotnet/" -Tag "latest" }
            "omnivec-dotnet-worker"   { Build-Image -Name $image -Dockerfile "$RootDir/connectors/worker/dotnet/Dockerfile" -Context "$RootDir/connectors/worker/dotnet/" -Tag "latest" }
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

# -- If not explicitly set to build, try import --
if (-not $DO_BUILD) {
    Write-Host "`n`e[33mPhase 1: Importing pre-built images from shared registry...`e[0m"
    Write-Host "  `e[36mSource: $SHARED_REGISTRY`e[0m"
    if ($SHARED_REGISTRY_TOKEN) {
        Write-Host "  `e[36mUsing provided registry token for import.`e[0m"
    } else {
        Write-Host "  `e[36mNo token provided - assuming public registry (anonymous pull).`e[0m"
    }
}

if ($DO_BUILD) {
    # BUILD MODE: Build images from source
    Write-Host "`n`e[33mPhase 1: Building images from source...`e[0m"

    Build-AllImages

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
        if (-not $FORCE_IMPORT -and (Test-ImageExists -Name $image -Tag "latest")) {
            Write-Host "  `e[32m${image}:latest exists, skipping.`e[0m"
            $skipCount++
            continue
        }

        Write-Host "  `e[36mImporting ${image}:latest...`e[0m"
        $imagesToImport += $image

        $job = Start-Job -ScriptBlock {
            param($ACR, $SHARED, $IMG, $USER, $TOKEN)
            $authArgs = @()
            if ($TOKEN) { $authArgs = @("--username", $USER, "--password", $TOKEN) }
            $importError = az acr import --name $ACR --source "${SHARED}/${IMG}:latest" --image "${IMG}:latest" @authArgs --force 2>&1
            if ($LASTEXITCODE -eq 0) { return "OK" }
            # Retry once on transient errors
            if ($importError -notmatch "unauthorized|authentication|401|not found|does not exist|InvalidHostName|could not be resolved") {
                Start-Sleep -Seconds 2
                az acr import --name $ACR --source "${SHARED}/${IMG}:latest" --image "${IMG}:latest" @authArgs --force 2>&1
                if ($LASTEXITCODE -eq 0) { return "OK" }
            }
            return "FAIL: $importError"
        } -ArgumentList $ACR_NAME, $SHARED_REGISTRY, $image, $SHARED_REGISTRY_USER, $SHARED_REGISTRY_TOKEN

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
            Write-Host "  `e[32m$($entry.Image):latest imported.`e[0m"
            $importCount++
        } else {
            Write-Host "  `e[31m$($entry.Image):latest import FAILED`e[0m"
            Write-Host "  `e[31m$result`e[0m"
        }
    }

    Write-Host "`e[32mImage import complete: $importCount imported, $skipCount skipped.`e[0m"

    # If import yielded no usable images, auto-fallback to source builds
    $totalAvailable = $importCount + $skipCount
    if ($totalAvailable -eq 0) {
        Write-Host "`n`e[33mImport provided no usable images. Falling back to source build mode...`e[0m"
        Build-AllImages
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
        Write-Host "  Ensure docgrok source/submodule exists, then re-run: azd hooks run postprovision"
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
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --context $KUBE_CONTEXT --overwrite-existing
Write-Host "`e[32mConnected to AKS cluster: $AKS_CLUSTER`e[0m"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

Write-Host "`n`e[33mPhase 3: Creating namespaces and secrets...`e[0m"

kubectl --context $KUBE_CONTEXT create namespace omnivec --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
kubectl --context $KUBE_CONTEXT create namespace docgrok --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
kubectl --context $KUBE_CONTEXT label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite
kubectl --context $KUBE_CONTEXT annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite

if ($ENABLE_BLOB_SOURCE -eq "true" -and $STORAGE_ACCOUNT) {
    kubectl --context $KUBE_CONTEXT create secret generic omnivec-storage `
        --namespace omnivec `
        --from-literal=account-name="$STORAGE_ACCOUNT" `
        --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" `
        --dry-run=client -o yaml | kubectl --context $KUBE_CONTEXT apply -f -
    Write-Host "  `e[32momnivec-storage secret created.`e[0m"
}

Write-Host "`e[32mNamespaces and secrets created.`e[0m"

# =============================================================================
# PHASE 4: Deploy with Helm
# =============================================================================

Write-Host "`n`e[33mPhase 4: Deploying OmniVec via Helm...`e[0m"

# Resolve helm chart dependencies (docgrok subchart)
Write-Host "  `e[36mResolving helm dependencies...`e[0m"
helm dependency build "$RootDir/helm/omnivec"

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
    "--set", "dotnetWorker.enabled=true",
    "--set", "web.service.dnsLabel=$INSTANCE_ID"
)

if ($KEYVAULT_URI) {
    $helmArgs += @(
        "--set", "azure.keyVault.uri=$KEYVAULT_URI"
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
        "--set", "azure.storage.blobEndpoint=$STORAGE_BLOB_ENDPOINT"
    )
}

$helmArgs += @("--kube-context", $KUBE_CONTEXT, "--wait", "--timeout", "10m")

& helm @helmArgs
if ($LASTEXITCODE -ne 0) {
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

Write-Host "`e[32mHelm deployment complete.`e[0m"

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
if (-not $LOCATION) { $LOCATION = "eastus2" }
$FQDN = "${INSTANCE_ID}.${LOCATION}.cloudapp.azure.com"

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
