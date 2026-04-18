# OmniVec - heartbeat helpers (PowerShell)
# Mirror of hooks/lib/heartbeat.sh for Windows hooks.

if ($script:_OmnivecHbLoaded) { return }
$script:_OmnivecHbLoaded = $true
$script:_HbStepStarts = @{}

function Get-UnixTime {
    [int][double]::Parse((Get-Date -UFormat %s))
}

function Initialize-Heartbeat {
    if (-not $env:OMNIVEC_RUN_START) {
        $env:OMNIVEC_RUN_START = (Get-UnixTime).ToString()
    }
    if (-not $env:OMNIVEC_TIMINGS_FILE) {
        $base = $env:HOME
        if (-not $base) { $base = $env:USERPROFILE }
        if (-not $base) { $base = $env:TEMP }
        $dir = Join-Path $base ".omnivec\runs"
        New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $env:OMNIVEC_TIMINGS_FILE = Join-Path $dir ("timings-{0}-{1}.jsonl" -f $stamp, $PID)
    }
    if (-not $env:OMNIVEC_HEARTBEAT_INTERVAL) {
        $env:OMNIVEC_HEARTBEAT_INTERVAL = "15"
    }
}

function Get-HeartbeatElapsed {
    Initialize-Heartbeat
    $start = [int]$env:OMNIVEC_RUN_START
    $e = (Get-UnixTime) - $start
    if ($e -lt 0) { $e = 0 }
    return ("[{0:D2}:{1:D2}]" -f [int]($e / 60), ($e % 60))
}

function Write-HeartbeatLog {
    param(
        [Parameter(Mandatory=$true)][ValidateSet("info","ok","warn","err","tick")][string]$Level,
        [Parameter(Mandatory=$true, Position=1, ValueFromRemainingArguments=$true)][string[]]$Message
    )
    $msg = $Message -join ' '
    $ts = Get-HeartbeatElapsed
    $colorOn = ''; $colorOff = ''
    if ($Host.UI.SupportsVirtualTerminal -or $env:TERM) {
        switch ($Level) {
            "info" { $colorOn = "`e[0;36m" }
            "ok"   { $colorOn = "`e[0;32m" }
            "warn" { $colorOn = "`e[1;33m" }
            "err"  { $colorOn = "`e[0;31m" }
            "tick" { $colorOn = "`e[2m" }
        }
        $colorOff = "`e[0m"
    }
    Write-Host "$colorOn$ts $msg$colorOff"
}

function Invoke-WithHeartbeat {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Label,
        [Parameter(Mandatory=$true)][scriptblock]$ScriptBlock
    )
    Initialize-Heartbeat
    $interval = [int]$env:OMNIVEC_HEARTBEAT_INTERVAL
    if ($interval -lt 1) { $interval = 15 }
    if ($interval -gt 3600) { $interval = 3600 }
    $quiet = ($env:OMNIVEC_HEARTBEAT_QUIET -eq "1")

    $start = Get-UnixTime
    $job = Start-Job -ScriptBlock $ScriptBlock
    try {
        while ($true) {
            $waited = 0
            while ($waited -lt $interval) {
                if ($null -ne (Wait-Job -Job $job -Timeout 1)) { break }
                $waited++
            }
            if ($job.State -ne 'Running') { break }
            if (-not $quiet) {
                $elapsed = (Get-UnixTime) - $start
                Write-HeartbeatLog -Level tick -Message "still $Label... (${elapsed}s)"
            }
        }
        $output = Receive-Job -Job $job -ErrorAction Continue 2>&1
        $failed = ($job.State -eq 'Failed')
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null
        if ($failed) {
            throw "Invoke-WithHeartbeat: '$Label' failed"
        }
        return $output
    } catch {
        if ($job -and $job.State -eq 'Running') {
            Stop-Job -Job $job -ErrorAction SilentlyContinue | Out-Null
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null
        }
        throw
    }
}

function Start-HeartbeatStep {
    param([Parameter(Mandatory=$true)][string]$Name)
    Initialize-Heartbeat
    $script:_HbStepStarts[$Name] = Get-UnixTime
    Write-HeartbeatLog -Level info -Message "> $Name"
}

function Stop-HeartbeatStep {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [ValidateSet("ok","fail","skip")][string]$Status = "ok"
    )
    Initialize-Heartbeat
    $start = $script:_HbStepStarts[$Name]
    $now = Get-UnixTime
    $dur = if ($start) { $now - $start } else { 0 }
    if ($env:OMNIVEC_TIMINGS_FILE) {
        $rec = [ordered]@{ name = $Name; status = $Status; start = $start; end = $now; duration = $dur }
        ($rec | ConvertTo-Json -Compress) | Add-Content -Path $env:OMNIVEC_TIMINGS_FILE -ErrorAction SilentlyContinue
    }
    switch ($Status) {
        "ok"   { Write-HeartbeatLog -Level ok   -Message "OK $Name (${dur}s)" }
        "fail" { Write-HeartbeatLog -Level err  -Message "FAIL $Name (${dur}s)" }
        "skip" { Write-HeartbeatLog -Level info -Message "SKIP $Name" }
    }
}

function Write-HeartbeatSummary {
    if (-not $env:OMNIVEC_TIMINGS_FILE) { return }
    if (-not (Test-Path $env:OMNIVEC_TIMINGS_FILE)) { return }
    Write-HeartbeatLog -Level info -Message "Step timing summary (slowest first):"
    $rows = Get-Content $env:OMNIVEC_TIMINGS_FILE -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_ | ConvertFrom-Json } catch { $null }
    } | Where-Object { $_ -and $_.duration -ne $null } |
      Sort-Object -Property duration -Descending |
      Select-Object -First 5
    foreach ($r in $rows) {
        "    {0,4}s  {1}" -f $r.duration, $r.name | Write-Host
    }
}

Initialize-Heartbeat
