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

# Validate required vars
foreach ($var in @("INSTANCE_ID","AKS_CLUSTER","ACR_LOGIN_SERVER","ACR_NAME","COSMOS_ENDPOINT","IDENTITY_CLIENT_ID","RESOURCE_GROUP")) {
    if (-not (Get-Variable $var -ValueOnly)) {
        Write-Host "`e[31mMissing required output: $var. Run 'azd provision' first.`e[0m"
        exit 1
    }
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
$SHARED_REGISTRY_TOKEN = "CWTNilJX3hS5ZLpf3f1bGwYD1Go8QA4HlB13l53XCzqIpNIxw2mPJQQJ99CCACHYHv6Eqg7NAAABAZCR94z7"

# Check if we should build or import
$DO_BUILD = if (Get-AzdValue "OMNIVEC_BUILD") { (Get-AzdValue "OMNIVEC_BUILD") -eq "true" } else { $false }
$FORCE_IMPORT = if ($env:OMNIVEC_FORCE_IMPORT) { $env:OMNIVEC_FORCE_IMPORT -eq "true" } else { $false }

# Images to import/build
$IMAGES = @(
    "omnivec-api",
    "omnivec-web",
    "omnivec-changefeed",
    "docgrok-pipeline-worker",
    "docgrok-router"
)

function Test-ImageExists {
    param($Name, $Tag)
    $ErrorActionPreference = "SilentlyContinue"
    $existing = az acr repository show-tags --name $ACR_NAME --repository $Name --query "[?@ == '$Tag']" -o tsv 2>$null
    $ErrorActionPreference = "Stop"
    return ($existing -and $existing.Trim())
}

if ($DO_BUILD) {
    # BUILD MODE: Build images from source
    Write-Host "`n`e[33mPhase 1: Building images from source...`e[0m"

    function Build-Image {
        param($Name, $Dockerfile, $Context, $Tag = "v1")

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

    Build-Image -Name "omnivec-api" -Dockerfile "$RootDir/api/Dockerfile" -Context $RootDir -Tag "v1"
    Build-Image -Name "omnivec-web" -Dockerfile "$RootDir/web/Dockerfile" -Context "$RootDir/web/" -Tag "v1"
    Build-Image -Name "omnivec-changefeed" -Dockerfile "$RootDir/connectors/ingestion/dotnet/Dockerfile" -Context "$RootDir/connectors/ingestion/dotnet/" -Tag "v1"

    if (Test-Path "$RootDir/docgrok/pipeline-worker/Dockerfile") {
        Build-Image -Name "docgrok-pipeline-worker" -Dockerfile "$RootDir/docgrok/pipeline-worker/Dockerfile" -Context "$RootDir/docgrok/pipeline-worker/" -Tag "v1"
    }
    if (Test-Path "$RootDir/docgrok/router/Dockerfile") {
        Build-Image -Name "docgrok-router" -Dockerfile "$RootDir/docgrok/router/Dockerfile" -Context "$RootDir/docgrok/router/" -Tag "v1"
    }

    Write-Host "`e[32mAll images built and pushed.`e[0m"
} else {
    # IMPORT MODE: Import pre-built images from shared registry (fast!)
    Write-Host "`n`e[33mPhase 1: Importing pre-built images from shared registry...`e[0m"
    Write-Host "  `e[36mSource: $SHARED_REGISTRY`e[0m"
    Write-Host "  `e[36mTo build from source instead: azd env set OMNIVEC_BUILD true`e[0m"

    $importCount = 0
    $skipCount = 0

    foreach ($image in $IMAGES) {
        if (-not $FORCE_IMPORT -and (Test-ImageExists -Name $image -Tag "v1")) {
            Write-Host "  `e[32m${image}:v1 exists, skipping.`e[0m"
            $skipCount++
            continue
        }

        Write-Host "  `e[36mImporting ${image}:v1...`e[0m"

        # Import from shared registry to user's ACR with retry
        $importSuccess = $false
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            $importError = az acr import `
                --name $ACR_NAME `
                --source "${SHARED_REGISTRY}/${image}:v1" `
                --image "${image}:v1" `
                --username $SHARED_REGISTRY_USER `
                --password $SHARED_REGISTRY_TOKEN `
                --force 2>&1

            if ($LASTEXITCODE -eq 0) {
                $importSuccess = $true
                break
            }

            # Don't retry auth or not-found errors
            if ($importError -match "unauthorized|authentication|401|not found|does not exist") {
                break
            }

            if ($attempt -lt 2) {
                Write-Host "  `e[33mRetrying ${image}:v1...`e[0m"
                Start-Sleep -Seconds 2
            }
        }

        if ($importSuccess) {
            Write-Host "  `e[32m${image}:v1 imported.`e[0m"
            $importCount++
        } else {
            Write-Host "  `e[31m${image}:v1 import FAILED`e[0m"
            Write-Host "  `e[31mError: $importError`e[0m"
            if ($importError -match "unauthorized|authentication|401") {
                Write-Host "  `e[31mHint: Token may be expired. Contact repo maintainer to regenerate.`e[0m"
            } elseif ($importError -match "not found|does not exist") {
                Write-Host "  `e[31mHint: Image not found. Run: azd env set OMNIVEC_BUILD true`e[0m"
            }
        }
    }

    Write-Host "`e[32mImage import complete: $importCount imported, $skipCount skipped.`e[0m"
}

# =============================================================================
# PHASE 2: Get AKS credentials
# =============================================================================

Write-Host "`n`e[33mPhase 2: Getting AKS credentials...`e[0m"
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER --overwrite-existing
Write-Host "`e[32mConnected to AKS cluster: $AKS_CLUSTER`e[0m"

# =============================================================================
# PHASE 3: Create namespaces and K8s secrets
# =============================================================================

Write-Host "`n`e[33mPhase 3: Creating namespaces and secrets...`e[0m"

kubectl create namespace omnivec --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace docgrok --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace omnivec app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate namespace omnivec meta.helm.sh/release-name=omnivec meta.helm.sh/release-namespace=omnivec --overwrite

if ($ENABLE_BLOB_SOURCE -eq "true" -and $STORAGE_ACCOUNT) {
    kubectl create secret generic omnivec-storage `
        --namespace omnivec `
        --from-literal=account-name="$STORAGE_ACCOUNT" `
        --from-literal=queue-endpoint="$STORAGE_QUEUE_ENDPOINT" `
        --dry-run=client -o yaml | kubectl apply -f -
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

$IMAGE_TAG = "v1"

$helmArgs = @(
    "upgrade", "--install", "omnivec", "$RootDir/helm/omnivec",
    "--namespace", "omnivec",
    "--set", "global.imageRegistry=$ACR_LOGIN_SERVER",
    "--set", "azure.workloadIdentity.clientId=$IDENTITY_CLIENT_ID",
    "--set", "azure.cosmos.endpoint=$COSMOS_ENDPOINT",
    "--set", "api.image.tag=$IMAGE_TAG",
    "--set", "controller.image.tag=$IMAGE_TAG",
    "--set", "worker.image.tag=$IMAGE_TAG",
    "--set", "web.image.tag=$IMAGE_TAG",
    "--set", "changefeed.image.tag=$IMAGE_TAG",
    "--set", "docgrok.global.imageRegistry=$ACR_LOGIN_SERVER",
    "--set", "docgrok.azure.workloadIdentity.clientId=$IDENTITY_CLIENT_ID",
    "--set", "docgrok.azure.cosmos.endpoint=$COSMOS_ENDPOINT",
    "--set", "docgrok.azure.cosmos.database=omnivec",
    "--set", "docgrok.azure.cosmos.container=metadata",
    "--set", "docgrok.docgrok.image.tag=$IMAGE_TAG"
)

if ($ENABLE_BLOB_SOURCE -eq "true") {
    $helmArgs += @(
        "--set", "azure.storage.accountName=$STORAGE_ACCOUNT",
        "--set", "azure.storage.blobEndpoint=$STORAGE_BLOB_ENDPOINT",
        "--set", "azure.serviceBus.namespace=$SB_ENDPOINT"
    )
}

$helmArgs += @("--wait", "--timeout", "10m")

helm @helmArgs

Write-Host "`e[32mHelm deployment complete.`e[0m"

# =============================================================================
# PHASE 5: Verify and print info
# =============================================================================

Write-Host "`n`e[33mPhase 5: Verifying deployment...`e[0m"

Write-Host "`n`e[36mOmniVec pods:`e[0m"
kubectl get pods -n omnivec --no-headers 2>$null

Write-Host "`n`e[36mDocGrok pods:`e[0m"
kubectl get pods -n docgrok --no-headers 2>$null

# Wait for external IP
Write-Host "`n`e[33mWaiting for external IP...`e[0m"
$externalIp = $null
for ($i = 0; $i -lt 30; $i++) {
    $externalIp = kubectl get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($externalIp) { break }
    Start-Sleep -Seconds 5
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

if ($externalIp) {
    Write-Host ""
    Write-Host "  OmniVec UI:    `e[36mhttp://${externalIp}/ui`e[0m"
    Write-Host "  Health Check:  `e[36mhttp://${externalIp}/health`e[0m"
} else {
    Write-Host ""
    Write-Host "  `e[33mExternal IP not yet assigned. Check with:`e[0m"
    Write-Host "  kubectl get svc omnivec-web -n omnivec"
}
Write-Host ""
