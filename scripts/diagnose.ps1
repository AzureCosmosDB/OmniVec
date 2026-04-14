# OmniVec Deployment Diagnostics
# Comprehensive health check across infrastructure, pods, networking, auth,
# images, pipelines, models, and common failure modes.
#
# Usage:
#   pwsh scripts/diagnose.ps1
#   pwsh scripts/diagnose.ps1 -EnvName my-omnivec
#   pwsh scripts/diagnose.ps1 -ServerUrl http://1.2.3.4 -AdminToken <token>

param(
    [string]$EnvName,
    [string]$ServerUrl,
    [string]$AdminToken
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
