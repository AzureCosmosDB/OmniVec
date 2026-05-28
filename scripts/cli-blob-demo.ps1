# OmniVec CLI demo — Azure Blob (text files) → Cosmos DB (vectors)
#
# Mirror of the cosmosdb-to-cosmosdb demo, but with blob storage as the source.
# All resource operations go through the OmniVec CLI; only the Azure infra setup
# (blob container, vector cosmos container, RBAC) uses az/python directly.
#
# Required env vars (or edit the constants below):
#   OMNIVEC_URL          OmniVec public URL  (e.g. https://wintest-3-...cloudapp.azure.com)
#   OMNIVEC_ADMIN_TOKEN  admin bearer token
#   AOAI_ENDPOINT        Azure OpenAI endpoint
#   AOAI_KEY             Azure OpenAI key
#   COSMOS_ACCOUNT       Cosmos account name (no .documents.azure.com suffix)
#   COSMOS_RG            Resource group of the Cosmos account
#   STORAGE_ACCOUNT      Storage account name (where blob container will be created)
#   STORAGE_SUB          (optional) subscription ID; defaults to current az context
#   COSMOS_DB            (optional) Cosmos database name; defaults to "testdb"
#
# Usage:
#   pwsh scripts/cli-blob-demo.ps1
#
# What it does (8 steps):
#   0. omnivec config + auth
#   1. cleanup prior cli-* artefacts
#   2. omnivec model add (Azure OpenAI embeddings)
#   3. create vector Cosmos container (az rest)
#   4. create blob container + upload 20 sample text files
#   5. omnivec source create (azure-blob) + destination create (cosmosdb-vector)
#   6. omnivec pipeline create (content-fields=content, file-types=txt, inline)
#   7. omnivec pipeline resume + poll until embedded
#   8. omnivec search × 3 over the new index

[CmdletBinding()]
param(
    [string]$Url            = $env:OMNIVEC_URL,
    [string]$Token          = $env:OMNIVEC_ADMIN_TOKEN,
    [string]$AoaiEndpoint   = $env:AOAI_ENDPOINT,
    [string]$AoaiKey        = $env:AOAI_KEY,
    [string]$CosmosAccount  = $env:COSMOS_ACCOUNT,
    [string]$CosmosRg       = $env:COSMOS_RG,
    [string]$CosmosDb       = $(if ($env:COSMOS_DB) { $env:COSMOS_DB } else { "testdb" }),
    [string]$StorageAccount = $env:STORAGE_ACCOUNT,
    [string]$Subscription   = $env:STORAGE_SUB,
    [string]$TranscriptPath = $(Join-Path $env:TEMP "cli_blob_demo_raw.log")
)

$ErrorActionPreference = "Stop"

# ── Validate required inputs ────────────────────────────────────────────────
foreach ($p in @{
    OMNIVEC_URL=$Url; OMNIVEC_ADMIN_TOKEN=$Token; AOAI_ENDPOINT=$AoaiEndpoint;
    AOAI_KEY=$AoaiKey; COSMOS_ACCOUNT=$CosmosAccount; COSMOS_RG=$CosmosRg;
    STORAGE_ACCOUNT=$StorageAccount
}.GetEnumerator()) {
    if (-not $p.Value) { Write-Error "Missing: $($p.Key)"; exit 1 }
}

if (-not $Subscription) {
    $Subscription = (az account show --query id -o tsv) 2>$null
    if (-not $Subscription) { Write-Error "Could not detect subscription. Set STORAGE_SUB."; exit 1 }
}

Remove-Item $TranscriptPath -ErrorAction SilentlyContinue
Start-Transcript -Path $TranscriptPath -IncludeInvocationHeader | Out-Null

$H = @{ Authorization = "Bearer $Token" }
$OMNIVEC = ".\bin\omnivec.exe"
$STAMP = Get-Date -Format HHmmss
$CONT = "cli-blob-$STAMP"           # blob container & cosmos vector container reuse the name
$LOCAL_SAMPLES = Join-Path $env:TEMP "cli_blob_samples_$STAMP"

Write-Host "########## STEP 0: omnivec config + auth (CLI) ##########"
Write-Host "PS> $OMNIVEC config set server $Url"
& $OMNIVEC config set server $Url
Write-Host "PS> $OMNIVEC auth login --token ***"
& $OMNIVEC auth login --token $Token
Write-Host "PS> $OMNIVEC config view"
& $OMNIVEC config view

Write-Host ""
Write-Host "########## STEP 1: cleanup via CLI (pipelines -> sources -> dests -> model) ##########"
$pipes = & $OMNIVEC pipeline list -o json | ConvertFrom-Json
foreach ($p in @($pipes) | Where-Object { $_.name -like "cli-*" }) {
    Write-Host "PS> $OMNIVEC pipeline delete $($p.id) -y"
    & $OMNIVEC pipeline delete $p.id -y
}
$srcs = & $OMNIVEC source list -o json | ConvertFrom-Json
foreach ($s in @($srcs) | Where-Object { $_.name -like "cli-*" }) {
    Write-Host "PS> $OMNIVEC source delete $($s.id) -y"
    & $OMNIVEC source delete $s.id -y
}
$dsts = & $OMNIVEC destination list -o json | ConvertFrom-Json
foreach ($d in @($dsts) | Where-Object { $_.name -like "cli-*" }) {
    Write-Host "PS> $OMNIVEC destination delete $($d.id) -y"
    & $OMNIVEC destination delete $d.id -y
}
$mdls = & $OMNIVEC model list -o json | ConvertFrom-Json
foreach ($m in @($mdls) | Where-Object { $_.name -eq "cli-aoai-sweden" }) {
    Write-Host "PS> $OMNIVEC model delete $($m.id) -y"
    & $OMNIVEC model delete $m.id -y
}

Write-Host ""
Write-Host "########## STEP 2: omnivec model add + test (CLI) ##########"
Write-Host "PS> $OMNIVEC model add --name cli-aoai-sweden --type azure-openai --model text-embedding-3-small --endpoint $AoaiEndpoint --api-key ***"
& $OMNIVEC model add --name cli-aoai-sweden --type azure-openai --model text-embedding-3-small --endpoint $AoaiEndpoint --api-key $AoaiKey
Write-Host "PS> $OMNIVEC model test cli-aoai-sweden"
& $OMNIVEC model test cli-aoai-sweden
$mdlId = (@(& $OMNIVEC model list -o json | ConvertFrom-Json) | Where-Object { $_.name -eq "cli-aoai-sweden" }).id
Write-Host "model id => $mdlId"

Write-Host ""
Write-Host "########## STEP 3: create vector Cosmos container (Azure infra - az rest) ##########"
Write-Host "container => $CONT"
$payload = @{
    properties = @{
        resource = @{
            id = $CONT
            partitionKey = @{ paths = @("/id"); kind = "Hash" }
            vectorEmbeddingPolicy = @{ vectorEmbeddings = @(@{ path="/embedding"; dataType="float32"; distanceFunction="cosine"; dimensions=1536 }) }
            indexingPolicy = @{
                indexingMode="consistent"; automatic=$true
                includedPaths=@(@{path="/*"})
                excludedPaths=@(@{path='/"_etag"/?'}, @{path="/embedding/*"})
                vectorIndexes=@(@{path="/embedding"; type="diskANN"})
            }
        }
        options = @{}
    }
} | ConvertTo-Json -Depth 12
$payload | Out-File _c.json -Encoding UTF8
$uri = "https://management.azure.com/subscriptions/$Subscription/resourceGroups/$CosmosRg/providers/Microsoft.DocumentDB/databaseAccounts/$CosmosAccount/sqlDatabases/$CosmosDb/containers/$($CONT)?api-version=2024-05-15"
Write-Host "PS> az rest --method PUT --url <mgmt>/containers/$CONT --body @_c.json"
az rest --method PUT --url $uri --body "@_c.json" --query "properties.resource.id" -o tsv
Remove-Item _c.json

Write-Host ""
Write-Host "########## STEP 4: generate + upload 20 sample text files (Azure infra) ##########"
New-Item -ItemType Directory -Force -Path $LOCAL_SAMPLES | Out-Null
$samples = @(
    @{name="alpine-shell.txt";     text="Alpine Shell Jacket. Lightweight three-layer waterproof breathable shell. Sealed seams, helmet-compatible hood, pit zips. Ideal for high-altitude mountaineering in driving rain and wet snow."},
    @{name="trail-runner.txt";     text="Coastal Quick-Lace Trail Runner. Featherlight running shoe with rock plate and aggressive lugs. Drains water quickly. For wet single-track and muddy trails."},
    @{name="merino-baselayer.txt"; text="Ridgeline Merino Baselayer 200. Long-sleeve crew in 200gsm fine-gauge merino wool. Odor resistant, regulates temperature winter and summer. Flatlock seams."},
    @{name="puffy-jacket.txt";     text="Stratos 800-Fill Down Hooded Puffy. Ultralight goose-down insulation. Compresses to softball size. For belay warmth, cold camp evenings, and shoulder-season alpine."},
    @{name="rain-pant.txt";        text="Pivot Stretch Rain Pant. 4-way stretch waterproof breathable shell pant. Articulated knees, full side zips for layering over boots. Hiking and approach climbing in storms."},
    @{name="ski-mitts.txt";        text="Stormtrack Insulated Ski Mitts. Goretex outer, primaloft gold insulation, leather palm. Long gauntlet cuff seals out powder. Cold-weather skiing and snowboarding."},
    @{name="trek-pole.txt";        text="Summit Carbon Foldable Trekking Poles. Z-fold three-section carbon shaft. EVA foam grips, tungsten carbide tips, snow baskets included. Backpacking and ultralight thru-hikes."},
    @{name="sleeping-bag.txt";     text="Hollow Peak 15F Down Sleeping Bag. 850-fill water-resistant down, mummy cut, draft tube and collar. Compresses small. Three-season backpacking down to fifteen Fahrenheit."},
    @{name="headlamp.txt";         text="Beacon 500 Rechargeable Headlamp. 500 lumen flood + spot, red night-vision mode. USB-C charging, ipx7 waterproof, freeze resistant. Trail running, climbing, camp chores."},
    @{name="day-pack.txt";         text="Talon 22 Day Pack. 22 liter ventilated mesh back panel, hipbelt pockets, trekking pole loops, rain cover. For day hikes, peak bagging, fast and light alpine."},
    @{name="approach-shoe.txt";    text="Granite Edge Approach Shoe. Sticky vibram rubber, climbing zone at toe, supportive midsole. Scrambling, via ferrata, approach to alpine routes."},
    @{name="ice-axe.txt";          text="Crag Climber Mountaineering Ice Axe. Aluminum shaft, steel head with adze. Self-arrest, glacier travel, moderate snow couloirs. CE-T rated."},
    @{name="climbing-harness.txt"; text="Sender Sport Climbing Harness. Padded waist and leg loops, four gear loops, haul loop. Lightweight and comfortable for redpoint sessions and multi-pitch."},
    @{name="bouldering-pad.txt";   text="Sentinel Triple-Fold Bouldering Pad. 4-inch closed-cell + open-cell foam stack, hinged folds, carry straps. Highball boulder problems and outdoor sessions."},
    @{name="bike-helmet.txt";      text="Vortex MIPS Road Bike Helmet. Aerodynamic shell, MIPS rotational protection, 22 vents. Road cycling, gravel, and group ride training."},
    @{name="hydration-pack.txt";   text="Tempo Vest 12L. Lightweight running hydration vest with two 500ml soft flasks. Multiple gel pockets, phone sleeve, whistle. Marathon, trail ultra, fastpacking."},
    @{name="winter-gloves.txt";    text="Glacier Pro Insulated Gloves. Goretex shell, primaloft gold, leather palm with reinforcements. Touchscreen index finger. Ice climbing and frigid winter hikes."},
    @{name="wool-sweater.txt";     text="Hearth Heavyweight Wool Crew Sweater. 100% Australian merino wool, ribbed cuffs and hem. Cozy and warm for cold autumn campfires and apres-ski."},
    @{name="water-filter.txt";     text="Sourcestream Pump Water Filter. Hollow fiber filter removes bacteria and protozoa, 1L per minute. Backcountry hiking, paddling, and international travel."},
    @{name="hiking-pants.txt";     text="Traverse Stretch Hiking Pants. Lightweight 4-way stretch nylon, DWR finish, articulated knees, zippered cargo pockets. All-season hiking and climbing approach."}
)
foreach ($s in $samples) {
    $path = Join-Path $LOCAL_SAMPLES $s.name
    [System.IO.File]::WriteAllText($path, $s.text, [System.Text.UTF8Encoding]::new($false))
}
Write-Host "Generated $($samples.Count) sample .txt files at $LOCAL_SAMPLES"

Write-Host "PS> az storage container create --name $CONT --account-name $StorageAccount --auth-mode login"
az storage container create --name $CONT --account-name $StorageAccount --auth-mode login --only-show-errors --output none
Write-Host "PS> az storage blob upload-batch -d $CONT -s $LOCAL_SAMPLES --account-name $StorageAccount --auth-mode login"
az storage blob upload-batch -d $CONT -s $LOCAL_SAMPLES --account-name $StorageAccount --auth-mode login --overwrite --only-show-errors --output none
$blobCount = (az storage blob list --container-name $CONT --account-name $StorageAccount --auth-mode login --query "length(@)" -o tsv 2>$null)
Write-Host "Uploaded $blobCount blobs to container $CONT"

Write-Host ""
Write-Host "########## STEP 5: source/destination create (CLI) ##########"
$srcCfg = @{
    account_url = "https://$StorageAccount.blob.core.windows.net"
    container   = $CONT
    file_type   = "txt"
    auth_type   = "managed-identity"
} | ConvertTo-Json -Compress
$dstCfg = @{
    endpoint  = "https://$CosmosAccount.documents.azure.com:443/"
    database  = $CosmosDb
    container = $CONT
    auth_type = "managed-identity"
} | ConvertTo-Json -Compress
Write-Host "PS> $OMNIVEC source create --name cli-src-$CONT --type azure-blob --config '$srcCfg'"
& $OMNIVEC source create --name "cli-src-$CONT" --type azure-blob --config $srcCfg
Write-Host "PS> $OMNIVEC destination create --name cli-dst-$CONT --type cosmosdb-vector --config '$dstCfg'"
& $OMNIVEC destination create --name "cli-dst-$CONT" --type cosmosdb-vector --config $dstCfg

$srcId = (@(& $OMNIVEC source list -o json | ConvertFrom-Json) | Where-Object { $_.name -eq "cli-src-$CONT" }).id
$dstId = (@(& $OMNIVEC destination list -o json | ConvertFrom-Json) | Where-Object { $_.name -eq "cli-dst-$CONT" }).id
Write-Host "srcId=$srcId  dstId=$dstId"

Write-Host ""
Write-Host "########## STEP 6: pipeline create (CLI, queue) ##########"
Write-Host "Note: azure-blob -> cosmosdb-vector requires --processing-mode queue (different connectors)."
Write-Host "PS> $OMNIVEC pipeline create --name cli-pipe-$CONT --source $srcId --destination $dstId --model $mdlId --content-fields content --file-types txt --embedding-field embedding --vector-index-path /embedding --processing-mode queue"
& $OMNIVEC pipeline create `
    --name "cli-pipe-$CONT" `
    --source $srcId `
    --destination $dstId `
    --model $mdlId `
    --content-fields content `
    --file-types txt `
    --embedding-field embedding `
    --vector-index-path /embedding `
    --processing-mode queue
$pipeId = (@(& $OMNIVEC pipeline list -o json | ConvertFrom-Json) | Where-Object { $_.name -eq "cli-pipe-$CONT" }).id
Write-Host "pipeId=$pipeId"

Write-Host ""
Write-Host "########## STEP 7: pipeline resume + source sync + poll ##########"
Write-Host "PS> $OMNIVEC pipeline resume $pipeId"
& $OMNIVEC pipeline resume $pipeId
Write-Host "PS> $OMNIVEC source sync $srcId --full"
& $OMNIVEC source sync $srcId --full
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 3
    $s = (& $OMNIVEC pipeline show $pipeId -o json | ConvertFrom-Json).stats
    Write-Host ("t+{0,3}s  embedded={1}/{2}  pending={3}  failed={4}" -f ($i*3), $s.embedded_count, $s.source_doc_count, $s.jobs.pending, $s.jobs.failed)
    if ($s.embedded_count -ge $s.source_doc_count -and $s.source_doc_count -gt 0) { Write-Host "DONE"; break }
}
Write-Host "PS> $OMNIVEC pipeline show $pipeId"
& $OMNIVEC pipeline show $pipeId

Write-Host ""
Write-Host "########## STEP 8: omnivec search (CLI) ##########"
foreach ($q in @(
    "warm waterproof hiking pants",
    "lightweight running shoes",
    "cozy wool sweater for winter"
)) {
    Write-Host ""
    Write-Host "PS> $OMNIVEC search `"$q`" --index $dstId --top-k 3"
    & $OMNIVEC search "$q" --index $dstId --top-k 3
}

# ── Cleanup local samples ────────────────────────────────────────────────────
Remove-Item -Recurse -Force $LOCAL_SAMPLES -ErrorAction SilentlyContinue

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "Raw transcript: $TranscriptPath  ($((Get-Item $TranscriptPath).Length) bytes)"
