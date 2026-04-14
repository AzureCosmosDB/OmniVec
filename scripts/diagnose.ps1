# OmniVec Deployment Diagnostics
# Comprehensive health check across infrastructure, pods, networking, auth,
# images, pipelines, models, and common failure modes.
#
# Usage:
#   pwsh scripts/diagnose.ps1                                    # full deployment check
#   pwsh scripts/diagnose.ps1 -EnvName my-omnivec                # specific environment
#   pwsh scripts/diagnose.ps1 -Pipeline pip-abc123               # deep-diagnose one pipeline
#   pwsh scripts/diagnose.ps1 -ServerUrl http://1.2.3.4 -AdminToken <token> -Pipeline pip-abc123

param(
    [string]$EnvName,
    [string]$ServerUrl,
    [string]$AdminToken,
    [string]$Pipeline   # Optional: diagnose a single pipeline in depth
)

$ErrorActionPreference = "SilentlyContinue"

$pass = 0; $warn = 0; $fail = 0
function Pass   { param([string]$Msg) $script:pass++; Write-Host "  `e[32m✓ PASS`e[0m  $Msg" }
function Warn   { param([string]$Msg, [string]$Fix) $script:warn++; Write-Host "  `e[33m⚠ WARN`e[0m  $Msg"; if ($Fix) { Write-Host "          `e[36mFix: $Fix`e[0m" } }
function Fail   { param([string]$Msg, [string]$Fix) $script:fail++; Write-Host "  `e[31m✗ FAIL`e[0m  $Msg"; if ($Fix) { Write-Host "          `e[36mFix: $Fix`e[0m" } }
function Header { param([string]$Msg) Write-Host "`n`e[33m── $Msg ──`e[0m" }

function ApiGet {
    param([string]$Path)
    try {
        $headers = @{}
        if ($script:AdminToken) { $headers["Authorization"] = "Bearer $script:AdminToken" }
        return Invoke-RestMethod -Uri "$script:ServerUrl$Path" -Headers $headers -TimeoutSec 15 -ErrorAction Stop
    } catch { return $null }
}

Write-Host "`n`e[36m╔══════════════════════════════════════════════════╗`e[0m"
Write-Host "`e[36m║       OmniVec Deployment Diagnostics              ║`e[0m"
Write-Host "`e[36m╚══════════════════════════════════════════════════╝`e[0m"

# ── Resolve environment ──────────────────────────────────────────────────────

if (-not $EnvName) { $EnvName = $env:AZURE_ENV_NAME }
if (-not $EnvName) {
    $envList = azd env list --output json 2>$null | ConvertFrom-Json
    $default = $envList | Where-Object { $_.IsDefault -eq $true }
    if ($default) { $EnvName = $default.Name }
}
if ($EnvName) {
    Write-Host "`n  Environment: `e[36m$EnvName`e[0m"
    azd env select $EnvName 2>$null
} else {
    Write-Host "`n  `e[33mNo environment — using current kubectl context.`e[0m"
}

$RG = "rg-omnivec-$EnvName"
$KUBE_CONTEXT = $null
$AKS_NAME = $null
$ACR_NAME = $null
$COSMOS_NAME = $null
$IDENTITY_ID = $null

# ═════════════════════════════════════════════════════════════════════════════
# 1. INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

Header "1. Infrastructure"

if ($EnvName) {
    $rgExists = az group exists --name $RG 2>$null
    if ("$rgExists".Trim() -eq "true") {
        Pass "Resource group $RG exists"

        $resources = az resource list --resource-group $RG --query "[].{type:type,name:name}" -o json 2>$null | ConvertFrom-Json
        $aksRes    = $resources | Where-Object { $_.type -match "containerService/managedClusters" } | Select-Object -First 1
        $cosmosRes = $resources | Where-Object { $_.type -match "documentDB/databaseAccounts" } | Select-Object -First 1
        $acrRes    = $resources | Where-Object { $_.type -match "containerRegistry" } | Select-Object -First 1
        $kvRes     = $resources | Where-Object { $_.type -match "vaults" } | Select-Object -First 1
        $storRes   = $resources | Where-Object { $_.type -match "storageAccounts" } | Select-Object -First 1
        $sbRes     = $resources | Where-Object { $_.type -match "servicebus" } | Select-Object -First 1
        $miRes     = $resources | Where-Object { $_.type -match "userAssignedIdentities" } | Select-Object -First 1

        if ($aksRes)    { Pass "AKS: $($aksRes.name)";           $AKS_NAME = $aksRes.name }       else { Fail "No AKS cluster found" "azd up" }
        if ($cosmosRes) { Pass "CosmosDB: $($cosmosRes.name)";   $COSMOS_NAME = $cosmosRes.name }  else { Fail "No CosmosDB account found" }
        if ($acrRes)    { Pass "ACR: $($acrRes.name)";           $ACR_NAME = $acrRes.name }        else { Fail "No Container Registry found" }
        if ($kvRes)     { Pass "Key Vault: $($kvRes.name)" }     else { Warn "No Key Vault found" }
        if ($storRes)   { Pass "Storage: $($storRes.name)" }     else { Warn "No Storage Account (blob source disabled?)" }
        if ($sbRes)     { Pass "Service Bus: $($sbRes.name)" }   else { Warn "No Service Bus (blob source disabled?)" }
        if ($miRes)     { Pass "Managed Identity: $($miRes.name)"; $IDENTITY_ID = $miRes.name } else { Warn "No Managed Identity found" }

        if ($AKS_NAME) {
            az aks get-credentials --resource-group $RG --name $AKS_NAME --overwrite-existing 2>$null
            $KUBE_CONTEXT = $AKS_NAME
        }
    } else {
        Fail "Resource group $RG does not exist" "azd up"
    }
} else {
    Warn "Skipping infrastructure checks (no environment name)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. POD HEALTH
# ═════════════════════════════════════════════════════════════════════════════

Header "2. Pod Health"

if ($KUBE_CONTEXT) {
    $pods = kubectl --context $KUBE_CONTEXT get pods -n omnivec --no-headers 2>$null
    if ($pods) {
        $podLines = $pods -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch "Terminating" }
        $running   = ($podLines | Where-Object { $_ -match "\s+(Running|Completed)\s+" }).Count
        $totalPods = $podLines.Count

        if ($running -eq $totalPods -and $totalPods -gt 0) { Pass "All $totalPods pods healthy" }
        elseif ($totalPods -eq 0) { Fail "No pods found in omnivec namespace" "azd hooks run postprovision" }
        else { Warn "$running/$totalPods pods healthy" }

        # Detect specific failure modes
        foreach ($line in $podLines) {
            $parts = ($line -replace '\s+', ' ').Trim().Split(' ')
            if ($parts.Count -lt 3) { continue }
            $podName = $parts[0]; $status = $parts[2]; $restarts = if ($parts.Count -ge 4) { $parts[3] } else { "0" }

            if ($status -eq "ImagePullBackOff" -or $status -eq "ErrImagePull") {
                Fail "$podName — $status (image missing or ACR auth failed)" "az acr repository list --name $ACR_NAME && azd hooks run postprovision"
            }
            elseif ($status -eq "CrashLoopBackOff") {
                Fail "$podName — CrashLoopBackOff (container crashes on start)" "kubectl logs $podName -n omnivec --tail=50 --previous"
            }
            elseif ($status -eq "Pending") {
                Fail "$podName — Pending (not scheduled)" "kubectl describe pod $podName -n omnivec | Select-String 'Events:' -Context 0,20"
            }
            elseif ($status -eq "Error") {
                Fail "$podName — Error" "kubectl logs $podName -n omnivec --tail=50"
            }
            elseif ([int]$restarts -gt 5) {
                Warn "$podName — $restarts restarts (may be unstable)" "kubectl logs $podName -n omnivec --tail=50 --previous"
            }
        }

        # Check expected deployments
        $expectedDeploys = @("omnivec-api", "omnivec-controller", "omnivec-web", "omnivec-cosmos-changefeed", "docgrok", "docgrok-controller")
        $deploys = kubectl --context $KUBE_CONTEXT get deployments -n omnivec --no-headers 2>$null
        foreach ($d in $expectedDeploys) {
            $match = ($deploys -split "`n" | Where-Object { $_ -match "^$d\s" }) | Select-Object -First 1
            if ($match) {
                if ($match -match "(\d+)/(\d+)") {
                    $ready = [int]$Matches[1]; $desired = [int]$Matches[2]
                    if ($desired -eq 0) { Warn "$d — scaled to 0 replicas" "kubectl scale deployment $d -n omnivec --replicas=1" }
                    elseif ($ready -lt $desired) { Warn "$d — $ready/$desired ready" }
                    else { Pass "$d — $ready/$desired ready" }
                }
            } else {
                Fail "Deployment $d not found" "azd hooks run postprovision"
            }
        }
    } else {
        Fail "Cannot list pods (kubectl connection failed)" "az aks get-credentials --resource-group $RG --name $AKS_NAME"
    }
} else {
    Warn "Skipping pod checks (no AKS context)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. HELM RELEASE STATE
# ═════════════════════════════════════════════════════════════════════════════

Header "3. Helm Release"

if ($KUBE_CONTEXT) {
    $helmJson = helm status omnivec -n omnivec --kube-context $KUBE_CONTEXT -o json 2>$null | ConvertFrom-Json
    if ($helmJson) {
        $helmStatus = $helmJson.info.status
        if ($helmStatus -eq "deployed") {
            Pass "Helm release 'omnivec' — deployed (revision $($helmJson.version))"
        } elseif ($helmStatus -match "^pending-") {
            Fail "Helm release stuck in '$helmStatus' (interrupted deploy)" "helm rollback omnivec -n omnivec --kube-context $KUBE_CONTEXT"
        } elseif ($helmStatus -eq "failed") {
            Fail "Helm release in 'failed' state" "helm rollback omnivec -n omnivec && azd hooks run postprovision"
        } else {
            Warn "Helm release status: $helmStatus"
        }
    } else {
        Fail "No Helm release 'omnivec' found" "azd hooks run postprovision"
    }
} else {
    Warn "Skipping Helm checks (no AKS context)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. NETWORKING & DNS
# ═════════════════════════════════════════════════════════════════════════════

Header "4. Networking & DNS"

if ($KUBE_CONTEXT) {
    $externalIp = kubectl --context $KUBE_CONTEXT get svc omnivec-web -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($externalIp) {
        Pass "External IP: $externalIp"
        if (-not $ServerUrl) { $ServerUrl = "http://$externalIp" }
    } else {
        Fail "No external IP on omnivec-web service" "kubectl get svc omnivec-web -n omnivec — wait 2-3 min or check NSG rules"
    }

    # Check FQDN DNS resolution
    $location = azd env get-value AZURE_LOCATION 2>$null
    if (-not $location) { $location = "eastus2" }
    $instanceId = $null
    $tags = az group show --name $RG --query "tags" -o json 2>$null | ConvertFrom-Json
    if ($tags -and $tags.'omnivec-instance') { $instanceId = $tags.'omnivec-instance' }
    if (-not $instanceId) { $instanceId = azd env get-value INSTANCE_ID 2>$null }
    if ($instanceId) {
        $fqdn = "$instanceId.$location.cloudapp.azure.com"
        try {
            $resolved = [System.Net.Dns]::GetHostAddresses($fqdn) | Select-Object -First 1
            if ($resolved) { Pass "FQDN resolves: $fqdn → $($resolved.IPAddressToString)" }
            else { Warn "FQDN $fqdn does not resolve" "DNS may take a few minutes after deploy" }
        } catch {
            Warn "FQDN $fqdn does not resolve yet" "DNS propagation can take a few minutes"
        }
    }
}

if ($ServerUrl) {
    try {
        $health = Invoke-RestMethod -Uri "$ServerUrl/health" -TimeoutSec 10 -ErrorAction Stop
        if ($health.status -eq "healthy") { Pass "API /health — healthy (v$($health.version))" }
        else { Warn "API /health returned status: $($health.status)" }
    } catch {
        Fail "API unreachable at $ServerUrl/health" "Check omnivec-api pods: kubectl logs -l app=omnivec-api -n omnivec --tail=20"
    }
} else {
    Warn "No server URL — skipping API checks"
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. AUTH, RBAC & WORKLOAD IDENTITY
# ═════════════════════════════════════════════════════════════════════════════

Header "5. Auth & RBAC"

if (-not $AdminToken) { $AdminToken = azd env get-value OMNIVEC_ADMIN_TOKEN 2>$null }

if ($ServerUrl -and $AdminToken) {
    $resp = ApiGet "/health"
    if ($resp) {
        Pass "Admin token accepted"
    } else {
        Fail "Admin token rejected or API unreachable" "Regenerate: azd env set OMNIVEC_ADMIN_TOKEN <new> && azd hooks run postprovision"
    }
} elseif (-not $AdminToken) {
    Warn "No admin token available" "azd env get-value OMNIVEC_ADMIN_TOKEN"
}

# Workload Identity webhook
if ($KUBE_CONTEXT) {
    $wiPods = kubectl --context $KUBE_CONTEXT get pods -n kube-system --no-headers 2>$null | Where-Object { $_ -match "azure-wi-webhook|workload-identity" }
    if ($wiPods -and ($wiPods | Where-Object { $_ -match "Running" })) {
        Pass "Workload Identity webhook — running"
    } elseif ($wiPods) {
        Fail "Workload Identity webhook — NOT running (pods exist but unhealthy)" "kubectl describe pods -n kube-system -l app.kubernetes.io/name=azure-workload-identity-webhook"
    } else {
        Warn "Workload Identity webhook not found" "All pods using managed identity will fail. Check AKS OIDC/WI addon."
    }
}

# CosmosDB RBAC check
if ($IDENTITY_ID -and $COSMOS_NAME -and $RG) {
    $cosmosId = az cosmosdb show --name $COSMOS_NAME --resource-group $RG --query "id" -o tsv 2>$null
    $principalId = az identity show --name $IDENTITY_ID --resource-group $RG --query "principalId" -o tsv 2>$null
    if ($cosmosId -and $principalId) {
        # Check ARM RBAC (Account Reader)
        $armRoles = az role assignment list --assignee $principalId --scope $cosmosId --query "[].roleDefinitionName" -o json 2>$null | ConvertFrom-Json
        if ($armRoles -match "Reader") {
            Pass "CosmosDB ARM RBAC — Account Reader assigned"
        } else {
            Fail "CosmosDB ARM RBAC — missing Account Reader role" "az role assignment create --assignee $principalId --role 'Cosmos DB Account Reader Role' --scope $cosmosId"
        }

        # Check SQL RBAC (Data Contributor)
        $sqlRoles = az cosmosdb sql role assignment list --account-name $COSMOS_NAME --resource-group $RG --query "[?principalId=='$principalId'].roleDefinitionId" -o json 2>$null | ConvertFrom-Json
        if ($sqlRoles -and $sqlRoles.Count -gt 0) {
            Pass "CosmosDB SQL RBAC — Data role assigned"
        } else {
            Fail "CosmosDB SQL RBAC — no data role for managed identity" "Grant 'Cosmos DB Built-in Data Contributor' via az cosmosdb sql role assignment create"
        }
    }
}

# Storage RBAC check
if ($IDENTITY_ID -and $storRes -and $RG) {
    $storId = az storage account show --name $storRes.name --resource-group $RG --query "id" -o tsv 2>$null
    $principalId2 = az identity show --name $IDENTITY_ID --resource-group $RG --query "principalId" -o tsv 2>$null
    if ($storId -and $principalId2) {
        $storRoles = az role assignment list --assignee $principalId2 --scope $storId --query "[].roleDefinitionName" -o json 2>$null | ConvertFrom-Json
        if ($storRoles -match "Blob") {
            Pass "Storage RBAC — Blob role assigned"
        } else {
            Warn "Storage RBAC — no Blob Data Reader role" "az role assignment create --assignee $principalId2 --role 'Storage Blob Data Reader' --scope $storId"
        }
    }
}

# ═════════════════════════════════════════════════════════════════════════════
# 6. CONTAINER IMAGES
# ═════════════════════════════════════════════════════════════════════════════

Header "6. Container Images"

if ($ACR_NAME) {
    $requiredImages = @("omnivec-api", "omnivec-web", "omnivec-changefeed", "omnivec-dotnet-worker", "docgrok-router", "docgrok-pipeline-worker")
    $repos = az acr repository list --name $ACR_NAME -o json 2>$null | ConvertFrom-Json

    foreach ($img in $requiredImages) {
        if ($repos -contains $img) {
            Pass "$img — present"
        } else {
            Fail "$img — MISSING from ACR" "azd hooks run postprovision"
        }
    }
} else {
    Warn "Skipping image checks (no ACR)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 7. NODE CAPACITY
# ═════════════════════════════════════════════════════════════════════════════

Header "7. Node Capacity"

if ($KUBE_CONTEXT) {
    $nodes = kubectl --context $KUBE_CONTEXT get nodes --no-headers 2>$null
    if ($nodes) {
        $nodeLines = $nodes -split "`n" | Where-Object { $_.Trim() }
        $readyNodes = ($nodeLines | Where-Object { $_ -match "\s+Ready\s+" }).Count
        $totalNodes = $nodeLines.Count

        if ($readyNodes -eq $totalNodes) { Pass "All $totalNodes nodes Ready" }
        else { Warn "$readyNodes/$totalNodes nodes Ready" }

        # Check for resource pressure
        foreach ($line in $nodeLines) {
            $nodeName = ($line -replace '\s+', ' ').Trim().Split(' ')[0]
            $conditions = kubectl --context $KUBE_CONTEXT get node $nodeName -o jsonpath='{range .status.conditions[*]}{.type}={.status}{" "}{end}' 2>$null
            if ($conditions -match "MemoryPressure=True") { Fail "Node $nodeName — MemoryPressure" "Consider scaling up node pool or VM SKU" }
            if ($conditions -match "DiskPressure=True")   { Fail "Node $nodeName — DiskPressure" "Clear disk space or resize OS disk" }
            if ($conditions -match "PIDPressure=True")    { Fail "Node $nodeName — PIDPressure" "Too many processes — check for runaway pods" }
        }

        # Check unschedulable pods
        $pendingPods = kubectl --context $KUBE_CONTEXT get pods -n omnivec --field-selector=status.phase=Pending --no-headers 2>$null
        if ($pendingPods -and $pendingPods.Trim()) {
            $pendingCount = ($pendingPods -split "`n" | Where-Object { $_.Trim() }).Count
            Fail "$pendingCount pods Pending — likely insufficient node capacity" "Scale node pool: az aks nodepool scale --resource-group $RG --cluster-name $AKS_NAME --name system --node-count 3"
        } else {
            Pass "No pods pending due to capacity"
        }
    }
} else {
    Warn "Skipping node checks (no AKS context)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 8. MODELS
# ═════════════════════════════════════════════════════════════════════════════

Header "8. Models"

if ($ServerUrl -and $AdminToken) {
    $modelsResp = ApiGet "/api/docgrok/models"
    $models = if ($modelsResp.models) { $modelsResp.models } elseif ($modelsResp -is [array]) { $modelsResp } else { @() }

    if ($models.Count -eq 0) {
        Warn "No embedding models registered" "Register via UI (Models → Add) or CLI: omnivec model add ..."
    } else {
        foreach ($m in $models) {
            $mname = $m.name; $mstatus = $m.status; $mkind = $m.kind

            if ($mstatus -eq "available" -or $mstatus -eq "running" -or $mstatus -eq "healthy") {
                Pass "Model '$mname' ($mkind) — $mstatus"

                # Test external model reachability
                if ($mkind -eq "external" -and $m.endpoint) {
                    try {
                        $null = Invoke-WebRequest -Uri $m.endpoint -Method Head -TimeoutSec 5 -ErrorAction Stop
                        Pass "  Endpoint reachable: $($m.endpoint)"
                    } catch {
                        if ($_.Exception.Response.StatusCode -eq 401 -or $_.Exception.Response.StatusCode -eq 403) {
                            Pass "  Endpoint reachable (auth required): $($m.endpoint)"
                        } else {
                            Warn "  Endpoint unreachable: $($m.endpoint)" "Verify endpoint URL and network access"
                        }
                    }
                }
            } elseif ($mstatus -eq "stopped") {
                Warn "Model '$mname' ($mkind) — stopped" "omnivec model start $mname"
            } else {
                Warn "Model '$mname' ($mkind) — $mstatus"
            }
        }
    }
} else {
    Warn "Skipping model checks (no API access)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 9. PIPELINES
# ═════════════════════════════════════════════════════════════════════════════

Header "9. Pipelines"

if ($ServerUrl -and $AdminToken) {
    $pipResp = ApiGet "/api/pipelines"
    $pipelines = if ($pipResp.pipelines) { $pipResp.pipelines } elseif ($pipResp -is [array]) { $pipResp } else { @() }

    if ($pipelines.Count -eq 0) {
        Warn "No pipelines configured" "Create via UI or CLI: omnivec pipeline create ..."
    } else {
        foreach ($p in $pipelines) {
            $pname = $p.name; $pid = $p.id; $pstatus = $p.status
            $stats = $p.stats
            $completed = if ($stats.documents_processed) { [int]$stats.documents_processed } else { 0 }
            $embedded  = if ($stats.embedded_count) { [int]$stats.embedded_count } else { 0 }
            $pct       = if ($stats.completion_pct) { [math]::Round($stats.completion_pct, 1) } else { 0 }
            $failedJobs = 0
            if ($stats.jobs) {
                $failedJobs = if ($stats.jobs.failed) { [int]$stats.jobs.failed } else { 0 }
            }

            if ($pstatus -eq "active") {
                if ($failedJobs -gt 0 -and $completed -eq 0) {
                    Fail "Pipeline '$pname' ($pid) — all jobs failing ($failedJobs failed)" "omnivec job list --pipeline $pid --status failed"
                } elseif ($failedJobs -gt 0) {
                    Warn "Pipeline '$pname' ($pid) — $embedded embedded, $failedJobs failed jobs ($pct%)" "omnivec job list --pipeline $pid --status failed"
                } elseif ($embedded -gt 0) {
                    Pass "Pipeline '$pname' ($pid) — active, $embedded embedded ($pct%)"
                } else {
                    Warn "Pipeline '$pname' ($pid) — active but 0 documents embedded" "Check source has documents and changefeed is running"
                }
            } elseif ($pstatus -eq "paused") {
                Warn "Pipeline '$pname' ($pid) — paused" "omnivec pipeline resume $pid"
            } elseif ($pstatus -eq "error") {
                Fail "Pipeline '$pname' ($pid) — error state" "omnivec pipeline show $pid"
            } else {
                Warn "Pipeline '$pname' ($pid) — $pstatus"
            }
        }
    }

    # Check sources
    $srcResp = ApiGet "/api/sources"
    $sources = if ($srcResp.sources) { $srcResp.sources } elseif ($srcResp -is [array]) { $srcResp } else { @() }
    foreach ($s in $sources) {
        if ($s.enabled -eq $false) {
            Warn "Source '$($s.name)' ($($s.id)) — disabled"
        }
    }

    # Check destinations
    $dstResp = ApiGet "/api/destinations"
    $destinations = if ($dstResp.destinations) { $dstResp.destinations } elseif ($dstResp -is [array]) { $dstResp } else { @() }
    foreach ($d in $destinations) {
        if ($d.enabled -eq $false) {
            Warn "Destination '$($d.name)' ($($d.id)) — disabled"
        }
    }
} else {
    Warn "Skipping pipeline checks (no API access)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 10. EVENT GRID & SERVICE BUS
# ═════════════════════════════════════════════════════════════════════════════

Header "10. Event Grid & Service Bus"

if ($sbRes -and $RG) {
    # Check Service Bus queue depth
    $sbName = $sbRes.name
    $queues = az servicebus queue list --namespace-name $sbName --resource-group $RG --query "[].{name:name,messageCount:messageCount}" -o json 2>$null | ConvertFrom-Json
    if ($queues) {
        foreach ($q in $queues) {
            if ([int]$q.messageCount -gt 1000) {
                Warn "Service Bus queue '$($q.name)' — $($q.messageCount) messages backed up" "Scale workers: kubectl scale deployment omnivec-dotnet-worker -n omnivec --replicas=3"
            } elseif ([int]$q.messageCount -gt 0) {
                Pass "Service Bus queue '$($q.name)' — $($q.messageCount) messages"
            } else {
                Pass "Service Bus queue '$($q.name)' — empty (healthy)"
            }
        }
    } else {
        Warn "Could not query Service Bus queues"
    }
} else {
    Warn "Skipping Service Bus checks (not provisioned or blob source disabled)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 11. RECENT ERRORS IN LOGS
# ═════════════════════════════════════════════════════════════════════════════

Header "11. Recent Errors (last 200 log lines)"

if ($KUBE_CONTEXT) {
    $checkDeploys = @("omnivec-api", "omnivec-controller", "omnivec-cosmos-changefeed")
    foreach ($dep in $checkDeploys) {
        $pod = kubectl --context $KUBE_CONTEXT get pods -n omnivec -l "app=$dep" --no-headers 2>$null | Where-Object { $_ -match "Running" } | Select-Object -First 1
        if ($pod) {
            $podName = ($pod -replace '\s+', ' ').Trim().Split(' ')[0]
            $logs = kubectl --context $KUBE_CONTEXT logs $podName -n omnivec --tail=200 2>$null
            $errors = $logs | Select-String -Pattern "ERROR|Exception|Traceback|RBAC|readMetadata|Unauthorized|forbidden|connection refused" | Select-Object -Last 5
            if ($errors) {
                Warn "$dep — recent errors detected:"
                foreach ($e in $errors) {
                    $line = $e.Line.Trim()
                    if ($line.Length -gt 140) { $line = $line.Substring(0, 140) + "..." }
                    Write-Host "          $line"
                }
                if ($logs -match "readMetadata") {
                    Write-Host "          `e[36mFix: Grant 'Cosmos DB Account Reader Role' (ARM RBAC) to the managed identity`e[0m"
                }
                if ($logs -match "Unauthorized" -and $dep -eq "omnivec-cosmos-changefeed") {
                    Write-Host "          `e[36mFix: Ensure API bypasses auth for Host: omnivec-api (internal K8s DNS)`e[0m"
                }
            } else {
                Pass "$dep — no errors in recent logs"
            }
        }
    }
} else {
    Warn "Skipping log checks (no AKS context)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 12. SINGLE PIPELINE DEEP DIAGNOSTICS (when -Pipeline is specified)
# ═════════════════════════════════════════════════════════════════════════════

if ($Pipeline -and $ServerUrl -and $AdminToken) {
    Header "12. Pipeline Deep Diagnostics: $Pipeline"

    # Normalize pipeline ID
    if ($Pipeline -notmatch "^pip-") { $Pipeline = "pip-$Pipeline" }

    # Fetch pipeline detail
    $pip = ApiGet "/api/pipelines/$Pipeline"
    if (-not $pip) {
        Fail "Pipeline $Pipeline not found" "Check ID with: omnivec pipeline list"
    } else {
        $pname = $pip.name
        $pstatus = $pip.status
        $pmode = $pip.processing_mode
        $pstrategy = $pip.content_strategy
        $destId = $pip.destination_id
        $modelId = $pip.docgrok_pipeline
        $vectorPath = $pip.vector_index_path

        Write-Host "  Name:              $pname"
        Write-Host "  Status:            $pstatus"
        Write-Host "  Mode:              $pmode"
        Write-Host "  Content Strategy:  $pstrategy"
        Write-Host "  Model:             $modelId"
        Write-Host "  Destination:       $destId"
        Write-Host "  Vector Path:       $vectorPath"
        Write-Host ""

        # ── Status check ──
        if ($pstatus -eq "active") {
            Pass "Pipeline is active"
        } elseif ($pstatus -eq "paused") {
            Fail "Pipeline is PAUSED — it will not process any documents" "omnivec pipeline resume $Pipeline"
        } elseif ($pstatus -eq "error") {
            Fail "Pipeline is in ERROR state" "omnivec pipeline show $Pipeline"
        } else {
            Warn "Pipeline status is '$pstatus'"
        }

        # ── Stats analysis ──
        $stats = $pip.stats
        $docCount = if ($stats.source_doc_count) { [int]$stats.source_doc_count } else { 0 }
        $embedded = if ($stats.embedded_count) { [int]$stats.embedded_count } else { 0 }
        $processed = if ($stats.documents_processed) { [int]$stats.documents_processed } else { 0 }
        $pct = if ($stats.completion_pct) { [math]::Round($stats.completion_pct, 1) } else { 0 }

        $jobsTotal = 0; $jobsPending = 0; $jobsProcessing = 0; $jobsCompleted = 0; $jobsFailed = 0
        if ($stats.jobs) {
            $jobsTotal = if ($stats.jobs.total) { [int]$stats.jobs.total } else { 0 }
            $jobsPending = if ($stats.jobs.pending) { [int]$stats.jobs.pending } else { 0 }
            $jobsProcessing = if ($stats.jobs.processing) { [int]$stats.jobs.processing } else { 0 }
            $jobsCompleted = if ($stats.jobs.completed) { [int]$stats.jobs.completed } else { 0 }
            $jobsFailed = if ($stats.jobs.failed) { [int]$stats.jobs.failed } else { 0 }
        }

        Write-Host "  Source docs:       $docCount"
        Write-Host "  Embedded:          $embedded ($pct%)"
        Write-Host "  Jobs:              $jobsTotal total ($jobsCompleted done, $jobsFailed failed, $jobsPending pending, $jobsProcessing in-progress)"
        Write-Host ""

        # ── Why is it stuck? Comprehensive stuck-pipeline detection ──
        Write-Host ""
        Write-Host "  `e[33m── Stuck / Not Running Analysis ──`e[0m"

        $stuckReasons = @()

        # 1. Pipeline paused
        if ($pstatus -eq "paused") {
            $stuckReasons += "Pipeline is PAUSED"
            Fail "Pipeline is PAUSED — will not process any documents" "omnivec pipeline resume $Pipeline"
        }

        # 2. Pipeline in error state
        if ($pstatus -eq "error") {
            $stuckReasons += "Pipeline is in ERROR state"
            Fail "Pipeline is in ERROR state — processing is halted" "omnivec pipeline show $Pipeline — check for config errors, then reset: omnivec pipeline reset $Pipeline"
        }

        # 3. Source has 0 documents
        if ($pstatus -eq "active" -and $docCount -eq 0) {
            $stuckReasons += "Source has 0 documents"
            if ($pip.process_existing -eq $false) {
                Warn "Source has 0 documents AND process_existing is OFF — only new documents will trigger" "Insert documents into the source, or update pipeline: omnivec pipeline update $Pipeline --process-existing"
            } else {
                Warn "Source has 0 documents — nothing to process yet" "Add documents to the source container"
            }
        }

        # 4. Documents exist but 0 jobs created (changefeed not triggering)
        if ($pstatus -eq "active" -and $docCount -gt 0 -and $embedded -eq 0 -and $jobsTotal -eq 0) {
            $stuckReasons += "Changefeed not creating jobs"
            Fail "Source has $docCount docs but 0 jobs created — changefeed is not triggering" "Possible causes:`n          - Changefeed pods not running`n          - Pipeline generation mismatch`n          - Source container has no change feed lease`n          Fix: kubectl logs -l app=omnivec-cosmos-changefeed -n omnivec --tail=100"
        }

        # 5. Jobs pending but none processing (workers down)
        if ($pstatus -eq "active" -and $jobsPending -gt 0 -and $jobsProcessing -eq 0) {
            $stuckReasons += "Workers not picking up jobs"
            Fail "$jobsPending jobs PENDING but 0 processing — workers are down or scaled to 0" "Check workers: kubectl get pods -n omnivec -l app=omnivec-dotnet-worker`n          Scale up: kubectl scale deployment omnivec-dotnet-worker -n omnivec --replicas=2"
        }

        # 6. Jobs stuck in processing (worker crash loop)
        if ($jobsProcessing -gt 0) {
            # Check if any jobs have been processing for too long
            $staleJobs = ApiGet "/api/jobs?pipeline_id=$Pipeline&status=processing&limit=5"
            if ($staleJobs.jobs) {
                foreach ($j in $staleJobs.jobs) {
                    $createdAt = $j.created_at
                    if ($createdAt) {
                        try {
                            $jobAge = (Get-Date) - [DateTime]::Parse($createdAt)
                            if ($jobAge.TotalMinutes -gt 10) {
                                $stuckReasons += "Jobs stuck in processing"
                                Fail "Job $($j.id) has been 'processing' for $([math]::Round($jobAge.TotalMinutes))min — likely stuck" "Worker may be crashing mid-job. Check: kubectl logs -l app=omnivec-dotnet-worker -n omnivec --tail=100"
                            }
                        } catch {}
                    }
                }
            }
        }

        # 7. All jobs failing
        if ($jobsFailed -gt 0 -and $jobsCompleted -eq 0) {
            $stuckReasons += "All jobs failing"
            Fail "ALL $jobsFailed jobs have FAILED — systemic problem (0 completed)" "omnivec job list --pipeline $Pipeline --status failed"
        }

        # 8. Partial failure
        if ($jobsFailed -gt 0 -and $jobsCompleted -gt 0) {
            $failRate = [math]::Round(($jobsFailed / ($jobsFailed + $jobsCompleted)) * 100)
            if ($failRate -gt 50) {
                Warn "High failure rate: $failRate% ($jobsFailed/$($jobsFailed + $jobsCompleted) failed)" "omnivec job list --pipeline $Pipeline --status failed"
            } else {
                Warn "$jobsFailed jobs failed ($failRate% failure rate)" "omnivec job list --pipeline $Pipeline --status failed"
            }
        }

        # 9. Stalled mid-progress (embedded < source count, no recent activity)
        if ($pstatus -eq "active" -and $docCount -gt 0 -and $embedded -gt 0 -and $embedded -lt $docCount -and $jobsPending -eq 0 -and $jobsProcessing -eq 0) {
            $remaining = $docCount - $embedded
            $stuckReasons += "Stalled at $pct% ($remaining docs remaining)"
            Warn "Pipeline stalled at $pct% — $embedded/$docCount embedded, $remaining remaining, but no jobs pending or processing" "Possible causes:`n          - New documents added after initial scan — run: omnivec pipeline run $Pipeline`n          - Changefeed lease expired — restart: kubectl rollout restart deployment omnivec-cosmos-changefeed -n omnivec`n          - Controller not scheduling — check: kubectl logs -l app=omnivec-controller -n omnivec --tail=50"

            # Check updated_at freshness
            if ($pip.updated_at) {
                try {
                    $lastUpdate = [DateTime]::Parse($pip.updated_at)
                    $staleness = (Get-Date) - $lastUpdate
                    if ($staleness.TotalMinutes -gt 30) {
                        Fail "Pipeline last updated $([math]::Round($staleness.TotalHours, 1)) hours ago — no recent activity" "Force rescan: omnivec pipeline run $Pipeline"
                    } elseif ($staleness.TotalMinutes -gt 10) {
                        Warn "Pipeline last updated $([math]::Round($staleness.TotalMinutes))min ago"
                    }
                } catch {}
            }
        }

        # 10. process_existing is false and pipeline is new
        if ($pstatus -eq "active" -and $pip.process_existing -eq $false -and $docCount -gt 0 -and $embedded -eq 0 -and $jobsTotal -eq 0) {
            $stuckReasons += "process_existing is OFF"
            Fail "process_existing is OFF — existing documents will NOT be processed" "Update: omnivec pipeline update $Pipeline --process-existing"
        }

        # 11. Summary of stuck analysis
        if ($stuckReasons.Count -eq 0 -and $pstatus -eq "active") {
            if ($embedded -gt 0 -and $pct -ge 99) {
                Pass "Pipeline looks healthy — $embedded documents embedded ($pct%)"
            } elseif ($embedded -gt 0) {
                Pass "Pipeline is actively processing — $embedded embedded so far ($pct%)"
            } else {
                Pass "Pipeline is active — waiting for documents or first changefeed trigger"
            }
        } elseif ($stuckReasons.Count -gt 0) {
            Write-Host ""
            Write-Host "  `e[31mPipeline is stuck. Root causes found: $($stuckReasons.Count)`e[0m"
            $i = 1
            foreach ($r in $stuckReasons) {
                Write-Host "    $i. $r"
                $i++
            }
        }

        # ── Source checks ──
        $srcEntries = $pip.sources
        if ($srcEntries -and $srcEntries.Count -gt 0) {
            foreach ($se in $srcEntries) {
                $srcId = $se.source_id
                Write-Host "  Checking source: $srcId"
                $src = ApiGet "/api/sources/$srcId"
                if (-not $src) {
                    Fail "Source $srcId not found — pipeline references a deleted source" "Create a new source or update the pipeline"
                } else {
                    if ($src.enabled -eq $false) {
                        Fail "Source '$($src.name)' ($srcId) is DISABLED" "Enable it in the UI or API"
                    } else {
                        Pass "Source '$($src.name)' ($srcId) — enabled"
                    }

                    # Test source connectivity via health checks
                    if ($healthResp -and $healthResp.sources) {
                        $srcHealth = $healthResp.sources | Where-Object { $_.id -eq $srcId }
                        if ($srcHealth) {
                            if ($srcHealth.status -eq "healthy") {
                                Pass "Source connectivity — healthy"
                                foreach ($c in $srcHealth.checks) {
                                    if ($c.status -eq "pass") { Pass "  $($c.check): $($c.detail)" }
                                    elseif ($c.status -eq "warn") { Warn "  $($c.check): $($c.detail)" }
                                    else { Fail "  $($c.check): $($c.detail)" }
                                }
                            } else {
                                Fail "Source connectivity — $($srcHealth.status)"
                                foreach ($c in $srcHealth.checks) {
                                    if ($c.status -ne "pass") {
                                        Fail "  $($c.check): $($c.detail)"
                                        if ($c.detail -match "readMetadata|RBAC") {
                                            Write-Host "          `e[36m→ Grant 'Cosmos DB Account Reader Role' to managed identity`e[0m"
                                        }
                                        if ($c.detail -match "Forbidden|403|unauthorized") {
                                            Write-Host "          `e[36m→ Grant 'Cosmos DB Built-in Data Contributor' (SQL RBAC)`e[0m"
                                        }
                                    }
                                }
                            }
                        } else {
                            Warn "Source $srcId not in health checks — run health check from UI first"
                        }
                    }

                    # Check content fields
                    $cfields = $se.content_fields
                    if ($cfields -and $cfields.Count -gt 0) {
                        Pass "Content fields: $($cfields -join ', ')"
                    } else {
                        Warn "No content_fields configured — pipeline may not know what to embed" "Set content_fields on the pipeline source entry"
                    }
                }
            }
        } else {
            Fail "Pipeline has no source entries" "Pipeline must have at least one source"
        }

        # ── Destination checks ──
        Write-Host "  Checking destination: $destId"
        $dst = ApiGet "/api/destinations/$destId"
        if (-not $dst) {
            Fail "Destination $destId not found — pipeline references a deleted destination"
        } else {
            if ($dst.enabled -eq $false) {
                Fail "Destination '$($dst.name)' ($destId) is DISABLED" "Enable it in the UI or API"
            } else {
                Pass "Destination '$($dst.name)' ($destId) — enabled"
            }

            # Check vector index path matches destination policy
            $vectorIndexes = $dst.config.vector_indexes
            if ($vectorIndexes -and $vectorIndexes.Count -gt 0) {
                $pathMatch = $vectorIndexes | Where-Object { $_.path -eq "/$vectorPath" -or $_.path -eq $vectorPath }
                if ($pathMatch) {
                    Pass "Vector path '/$vectorPath' found in destination policy (${($pathMatch.dimensions)}d, $($pathMatch.distanceFunction))"
                } else {
                    $availPaths = ($vectorIndexes | ForEach-Object { $_.path }) -join ", "
                    Fail "Vector path '/$vectorPath' NOT in destination policy. Available: $availPaths" "Update pipeline vector_index_path or add the path to the container's vector policy"
                }
            } else {
                Warn "Destination has no vector indexes — Fetch Vector Index Details may not have been run"
            }
        }

        # ── Model checks ──
        Write-Host "  Checking model: $modelId"
        $healthResp = ApiGet "/api/health/checks"
        if ($healthResp -and $healthResp.models) {
            $modelHealth = $healthResp.models | Where-Object { $_.id -eq $modelId }
            if ($modelHealth) {
                if ($modelHealth.status -eq "healthy") {
                    Pass "Model '$($modelHealth.name)' — healthy"
                    foreach ($c in $modelHealth.checks) {
                        if ($c.status -eq "pass") { Pass "  $($c.check): $($c.detail)" }
                        elseif ($c.status -eq "warn") { Warn "  $($c.check): $($c.detail)" }
                        else { Fail "  $($c.check): $($c.detail)" }
                    }
                } else {
                    Fail "Model '$($modelHealth.name)' — $($modelHealth.status)"
                    foreach ($c in $modelHealth.checks) {
                        if ($c.status -ne "pass") { Fail "  $($c.check): $($c.detail)" }
                    }
                }
            } else {
                Fail "Model $modelId not found in health checks" "Register the model: omnivec model add ..."
            }
        }

        # ── Dimension mismatch check ──
        if ($dst -and $dst.config.vector_dimensions -and $healthResp.models) {
            $modelObj = $healthResp.models | Where-Object { $_.id -eq $modelId }
            if ($modelObj) {
                $modelDims = $null
                foreach ($c in $modelObj.checks) {
                    if ($c.detail -match "(\d+)\s*dim") { $modelDims = [int]$Matches[1] }
                }
                $destDims = [int]$dst.config.vector_dimensions
                if ($modelDims -and $destDims -and $modelDims -ne $destDims) {
                    Fail "DIMENSION MISMATCH: Model outputs ${modelDims}d but destination expects ${destDims}d" "Either change the model or recreate the destination container with matching dimensions"
                } elseif ($modelDims -and $destDims) {
                    Pass "Dimensions match: model ${modelDims}d = destination ${destDims}d"
                }
            }
        }

        # ── Changefeed / worker pod check for this pipeline ──
        if ($KUBE_CONTEXT) {
            if ($pmode -eq "queue") {
                $workerPods = kubectl --context $KUBE_CONTEXT get pods -n omnivec -l "app=omnivec-dotnet-worker" --no-headers 2>$null
                if ($workerPods -and ($workerPods | Where-Object { $_ -match "Running" })) {
                    $runningWorkers = ($workerPods | Where-Object { $_ -match "Running" }).Count
                    Pass "Worker pods: $runningWorkers running (queue mode)"
                } else {
                    Fail "No worker pods running — queue mode jobs cannot be processed" "kubectl scale deployment omnivec-dotnet-worker -n omnivec --replicas=1"
                }
            }

            $cfPods = kubectl --context $KUBE_CONTEXT get pods -n omnivec -l "app=omnivec-cosmos-changefeed" --no-headers 2>$null
            if ($cfPods -and ($cfPods | Where-Object { $_ -match "Running" })) {
                $runningCf = ($cfPods | Where-Object { $_ -match "Running" }).Count
                Pass "Changefeed pods: $runningCf running"
            } else {
                Fail "No changefeed pods running — new documents will not be detected" "kubectl scale deployment omnivec-cosmos-changefeed -n omnivec --replicas=1"
            }

            # Check recent failed job errors
            if ($jobsFailed -gt 0) {
                Write-Host ""
                Write-Host "  `e[33mRecent failed job errors:`e[0m"
                $failedJobs = ApiGet "/api/jobs?pipeline_id=$Pipeline&status=failed&limit=5"
                if ($failedJobs.jobs) {
                    foreach ($j in $failedJobs.jobs) {
                        $errMsg = if ($j.error) { $j.error } else { "no error message" }
                        if ($errMsg.Length -gt 120) { $errMsg = $errMsg.Substring(0, 120) + "..." }
                        Write-Host "    Job $($j.id): $errMsg"

                        # Pattern-match common errors
                        if ($errMsg -match "readMetadata|RBAC") {
                            Write-Host "    `e[36m→ Missing Cosmos DB Account Reader Role on managed identity`e[0m"
                        }
                        if ($errMsg -match "dimension|Dimensions") {
                            Write-Host "    `e[36m→ Embedding dimension mismatch between model and destination`e[0m"
                        }
                        if ($errMsg -match "DeploymentNotFound|deployment.*not found") {
                            Write-Host "    `e[36m→ Azure OpenAI deployment name doesn't match (check exact name in portal)`e[0m"
                        }
                        if ($errMsg -match "401|Unauthorized|InvalidApiKey") {
                            Write-Host "    `e[36m→ Model API key is invalid or expired`e[0m"
                        }
                        if ($errMsg -match "timeout|Timeout|timed out") {
                            Write-Host "    `e[36m→ Model endpoint is slow or unreachable — check network and endpoint health`e[0m"
                        }
                        if ($errMsg -match "rate.*limit|429|throttl") {
                            Write-Host "    `e[36m→ Model endpoint is rate-limiting — reduce worker replicas or increase quota`e[0m"
                        }
                    }
                }
            }
        }
    }
}

# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

Write-Host "`n`e[36m══════════════════════════════════════════════════`e[0m"
Write-Host "`e[36m  Summary: $pass passed, $warn warnings, $fail failures`e[0m"
if ($fail -gt 0) {
    Write-Host "`e[31m  Issues found — review FAIL items above.`e[0m"
} elseif ($warn -gt 0) {
    Write-Host "`e[33m  Mostly healthy — review WARN items above.`e[0m"
} else {
    Write-Host "`e[32m  All checks passed! Deployment is healthy.`e[0m"
}
Write-Host ""
