<#
.SYNOPSIS
  CHILI Docker watchdog - keeps the Docker engine and the live CHILI stack alive.

.DESCRIPTION
  Invoked every ~5 min by the "CHILI Docker Socket Watchdog" scheduled task
  (LogonType Interactive as user 'rindo' - required to reach the Docker named
  pipe; SYSTEM cannot. RunLevel Limited / non-elevated.).

  Responsibilities:

    1) AUTO-RECOVERY (primary). Bring the stack back after a STACK-WIDE outage
       (host reboot, or the Docker engine crashing/bouncing). If the engine is
       unreachable it (re)launches Docker Desktop, waits for the WSL2 engine,
       then starts the LAST-KNOWN-GOOD set of containers in dependency order
       (postgres -> healthy -> ollama -> chili-app workers). It also detects the
       post-reboot case where the engine auto-started (on login) but the
       containers are still Exited.

    2) SOCKET MONITOR (secondary). Count Docker-owned host sockets; WARN at
       -WarnBoundSockets, CRITICAL at -CriticalDockerBoundSockets. Observability
       only - a healthy engine is never restarted just for a high count
       (force-killing com.docker.backend is what CAUSES the socket crash-loop).

  CRITICAL SAFETY DESIGN - this host is littered with STOPPED old-deploy
  generations (chili-clean-recovery-<role>-pre<NNN>, -prem<NN>, -preconcurrency,
  ...). The deploy flow renames the outgoing container to a -pre* suffix and
  reuses the canonical name for the new one. The watchdog must NEVER start those
  stale generations (doing so = duplicate autotrader/brain/scheduler on a live
  trading box). So it ONLY ever starts containers from the persisted
  last-known-good set, and only when a LARGE fraction of that set is down (a
  stack-wide event) - a single down container during a normal deploy is left to
  Docker's own restart policy / the operator.

.NOTES
  Background: [[reference_docker_recovery]], [[project_docker_disk_hygiene]],
  [[project_docker_deploy_model]].
#>
[CmdletBinding()]
param(
    # Socket-count thresholds (passed by the scheduled task). Config, not magic
    # numbers - tune on the task action line, not in this file.
    [int]$WarnBoundSockets = 2000,
    [int]$CriticalDockerBoundSockets = 8000,

    # Fraction of the last-known-good set that must be down for a healthy-engine
    # tick to treat it as a stack-wide outage and restore. The single documented
    # knob: below this, individual-container recovery is left to Docker's policy.
    [double]$MinDownFractionToRestore = 0.5,

    [int]$EngineWaitSeconds = 300,
    [switch]$DryRun,

    [string]$DockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe',
    [string]$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe',
    # Absolute repo root - the single documented setting that anchors the log +
    # state paths. Deterministic regardless of $PSScriptRoot / cwd (the
    # scheduled-task context resolves $PSScriptRoot-relative paths to D:\logs).
    [string]$RepoRoot = 'D:\dev\chili-home-copilot',
    [string]$LogPath,
    [string]$StatePath
)

$ErrorActionPreference = 'Stop'
if (-not $LogPath) { $LogPath = Join-Path $RepoRoot 'logs\docker-watchdog.log' }
if (-not $StatePath) { $StatePath = Join-Path $RepoRoot 'logs\docker-watchdog-stack.json' }

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'CRIT', 'OK')] [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = "[$ts][docker-watchdog][$Level] $Message"
    Write-Output $line
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 5MB)) {
            Set-Content -Path $LogPath -Value (Get-Content $LogPath -Tail 2000) -Encoding utf8
        }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch { }
}

if (-not (Test-Path $DockerExe)) {
    $resolved = (Get-Command docker -ErrorAction SilentlyContinue).Source
    if ($resolved) { $DockerExe = $resolved }
}

function Invoke-Docker {
    # Run a docker CLI call; return @{ Ok=$bool; Out=$string; Err=$string }. Never throws.
    param([string[]]$DockerArgs, [int]$TimeoutSec = 30)
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $DockerExe
        $psi.Arguments = ($DockerArgs -join ' ')
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch { }; return @{ Ok = $false; Out = ''; Err = "timeout ${TimeoutSec}s" } }
        return @{ Ok = ($p.ExitCode -eq 0); Out = $p.StandardOutput.ReadToEnd().Trim(); Err = $p.StandardError.ReadToEnd().Trim() }
    } catch { return @{ Ok = $false; Out = ''; Err = $_.Exception.Message } }
}

function Test-Engine { (Invoke-Docker @('version', '--format', '"{{.Server.Version}}"') -TimeoutSec 20).Ok }

# ---------------------------------------------------------------------------
# stack discovery (adaptive - by image, not by hardcoded name)
# ---------------------------------------------------------------------------
function Get-ContainerTier {
    param([string]$Image)
    if ($Image -match '^postgres:') { return 0 }
    if ($Image -match 'ollama/ollama') { return 1 }
    if ($Image -match '^chili-app:') { return 2 }
    return $null   # not part of the CHILI stack
}

function Get-StackContainers {
    # Returns objects: Name, Image, State, Tier for every CHILI stack container.
    $res = Invoke-Docker @('ps', '-a', '--no-trunc', '--format', '"{{.Names}}|{{.Image}}|{{.State}}"')
    if (-not $res.Ok -or -not $res.Out) { return @() }
    $list = @()
    foreach ($row in ($res.Out -split "`n")) {
        $parts = $row.Trim('"', ' ') -split '\|'
        if ($parts.Count -lt 3) { continue }
        $tier = Get-ContainerTier $parts[1]
        if ($null -eq $tier) { continue }
        $list += [pscustomobject]@{ Name = $parts[0]; Image = $parts[1]; State = $parts[2]; Tier = $tier }
    }
    return $list
}

function Wait-PostgresHealthy {
    param([string]$Name, [int]$TimeoutSec = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $h = (Invoke-Docker @('inspect', '-f', '"{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"', $Name)).Out.Trim('"', ' ')
        if ($h -eq 'healthy' -or $h -eq 'none') { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# ---------------------------------------------------------------------------
# last-known-good set persistence
# ---------------------------------------------------------------------------
function Get-GoodSet {
    if (-not (Test-Path $StatePath)) { return @() }
    try {
        $j = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @($j.containers)   # array of @{ name; tier }
    } catch { Write-Log "could not read good-set state: $($_.Exception.Message)" 'WARN'; return @() }
}

function Save-GoodSet {
    param($RunningStack)   # objects with Name, Tier
    if ($DryRun) { return }
    try {
        $payload = [pscustomobject]@{
            updatedUtc = (Get-Date).ToUniversalTime().ToString('o')
            containers = @($RunningStack | ForEach-Object { [pscustomobject]@{ name = $_.Name; tier = $_.Tier } })
        }
        $dir = Split-Path -Parent $StatePath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $payload | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath -Encoding utf8
    } catch { Write-Log "could not save good-set state: $($_.Exception.Message)" 'WARN' }
}

function Restore-GoodSet {
    # Start every last-known-good container that still exists and is down, in
    # tier order. NEVER starts anything not in the good-set (excludes stale -pre*).
    param($Good, $Stack)
    $byName = @{}; foreach ($c in $Stack) { $byName[$c.Name] = $c }
    $toStart = @()
    foreach ($g in $Good) {
        $cur = $byName[$g.name]
        if (-not $cur) { Write-Log "good-set member '$($g.name)' no longer exists - skipping" 'WARN'; continue }
        if ($cur.State -ne 'running') { $toStart += [pscustomobject]@{ Name = $g.name; Tier = [int]$g.tier } }
    }
    if (-not $toStart) { Write-Log 'all good-set containers already running' 'OK'; return }
    Write-Log ("restoring {0} container(s): {1}" -f $toStart.Count, (($toStart.Name) -join ', ')) 'WARN'
    if ($DryRun) { Write-Log '[DryRun] would start the above in tier order' 'INFO'; return }
    foreach ($tier in 0, 1, 2) {
        foreach ($c in ($toStart | Where-Object Tier -eq $tier)) {
            $r = Invoke-Docker @('start', $c.Name) -TimeoutSec 60
            if ($r.Ok) { Write-Log "started $($c.Name)" 'OK' } else { Write-Log "FAILED to start $($c.Name): $($r.Err)" 'CRIT' }
        }
        if ($tier -eq 0) {
            foreach ($pg in ($toStart | Where-Object Tier -eq 0)) {
                if (Wait-PostgresHealthy -Name $pg.Name) { Write-Log "$($pg.Name) healthy" 'OK' }
                else { Write-Log "$($pg.Name) not healthy within timeout - continuing" 'WARN' }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# socket monitor
# ---------------------------------------------------------------------------
function Get-DockerSocketCount {
    # "Bound sockets" = TCP connections owned by Docker host processes + AF_UNIX
    # socket files under %LOCALAPPDATA%\Docker\run. Documented proxy for the
    # socket-bloat the original watchdog guarded against; adjust if needed.
    $tcp = 0
    try {
        $pids = (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'com\.docker|docker|vpnkit|Docker Desktop' }).Id
        if ($pids) { $tcp = (Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object { $pids -contains $_.OwningProcess }).Count }
    } catch { }
    $files = 0
    try { $runDir = Join-Path $env:LOCALAPPDATA 'Docker\run'; if (Test-Path $runDir) { $files = (Get-ChildItem $runDir -Recurse -File -ErrorAction SilentlyContinue).Count } } catch { }
    return ($tcp + $files)
}

function Write-SocketStatus {
    $n = Get-DockerSocketCount
    if ($n -ge $CriticalDockerBoundSockets) { Write-Log "CRITICAL: Docker-owned sockets=$n (>= $CriticalDockerBoundSockets). Engine reachable so NOT auto-restarting (would risk the orphaned-socket crash-loop). Investigate the stats-poll storm / gracefully restart Docker." 'CRIT' }
    elseif ($n -ge $WarnBoundSockets) { Write-Log "WARN: Docker-owned sockets=$n (>= $WarnBoundSockets)." 'WARN' }
    else { Write-Log "sockets=$n (warn $WarnBoundSockets / crit $CriticalDockerBoundSockets)" 'INFO' }
}

# ---------------------------------------------------------------------------
# engine recovery (unreachable)
# ---------------------------------------------------------------------------
function Test-CrashLoopSignature {
    try {
        $log = Join-Path $env:LOCALAPPDATA 'Docker\log\host\com.docker.backend.exe.log'
        if (-not (Test-Path $log)) { return $false }
        return [bool]((Get-Content $log -Tail 120 -ErrorAction SilentlyContinue) | Where-Object { $_ -match 'cannot be accessed by the system|remove .*\.sock' })
    } catch { return $false }
}

function Repair-OrphanedSockets {
    Write-Log 'crash-loop socket signature detected - repairing orphaned socket dirs' 'CRIT'
    if ($DryRun) { Write-Log '[DryRun] would stop docker procs + rename Docker\run / docker-secrets-engine to .broken' 'INFO'; return }
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'com\.docker|Docker Desktop|vpnkit|dockerd' } | ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Seconds 5
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
    foreach ($rel in 'Docker\run', 'docker-secrets-engine') {
        $dir = Join-Path $env:LOCALAPPDATA $rel
        if (Test-Path $dir) {
            try { Rename-Item -Path $dir -NewName ("{0}.broken{1}" -f (Split-Path $dir -Leaf), $stamp) -ErrorAction Stop; Write-Log "renamed $dir -> .broken$stamp" 'OK' }
            catch { Write-Log "could not rename $dir : $($_.Exception.Message)" 'WARN' }
        }
    }
}

function Start-DockerDesktop {
    if ($DryRun) { Write-Log '[DryRun] would launch Docker Desktop' 'INFO'; return }
    if (-not (Test-Path $DockerDesktopExe)) { Write-Log "Docker Desktop.exe not found at $DockerDesktopExe" 'CRIT'; return }
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try { Start-Process -FilePath $DockerDesktopExe | Out-Null } catch { Write-Log "launch attempt $attempt error: $($_.Exception.Message)" 'WARN' }
        Start-Sleep -Seconds 8
        if (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue) { Write-Log "Docker Desktop launched (attempt $attempt)" 'OK'; return }
        Write-Log "no Docker Desktop process after attempt $attempt - retrying" 'WARN'
    }
}

function Invoke-EngineRecovery {
    Write-Log 'engine UNREACHABLE - starting recovery' 'CRIT'
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'com\.docker|Docker Desktop' }
    if ($procs -and (Test-CrashLoopSignature)) { Repair-OrphanedSockets }
    elseif ($procs) { Write-Log 'Docker processes present but engine not answering - may still be starting; waiting' 'INFO' }
    else { Write-Log 'no Docker processes (post-reboot / clean exit)' 'INFO' }

    Start-DockerDesktop
    if ($DryRun) { Write-Log '[DryRun] would poll for engine, then restore the good-set' 'INFO'; return }

    Write-Log "waiting up to ${EngineWaitSeconds}s for the engine..." 'INFO'
    $deadline = (Get-Date).AddSeconds($EngineWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Engine) {
            Write-Log 'engine reachable again' 'OK'
            $good = Get-GoodSet
            if (-not $good) { Write-Log 'no last-known-good set recorded yet - NOT guessing which containers to start (seed logs\docker-watchdog-stack.json)' 'WARN'; return }
            Restore-GoodSet -Good $good -Stack (Get-StackContainers)
            return
        }
        Start-Sleep -Seconds 10
    }
    Write-Log "engine STILL unreachable after ${EngineWaitSeconds}s - manual intervention may be needed (see reference_docker_recovery)" 'CRIT'
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
Write-Log ("watchdog tick (DryRun={0}, minDownFraction={1})" -f [bool]$DryRun, $MinDownFractionToRestore) 'INFO'

if (-not (Test-Engine)) { Invoke-EngineRecovery; Write-Log 'watchdog tick done' 'INFO'; return }

Write-Log 'engine reachable' 'OK'
Write-SocketStatus

$stack = Get-StackContainers
$running = @($stack | Where-Object State -eq 'running')
$good = Get-GoodSet

if (-not $good) {
    # First run after install: seed the good-set from whatever is running now.
    Write-Log ("seeding last-known-good set with {0} running container(s)" -f $running.Count) 'INFO'
    Save-GoodSet $running
    Write-Log 'watchdog tick done' 'INFO'; return
}

$runningNames = $running.Name
$downGood = @($good | Where-Object { $runningNames -notcontains $_.name })
$threshold = [Math]::Max(2, [Math]::Ceiling($good.Count * $MinDownFractionToRestore))

if ($downGood.Count -ge $threshold) {
    # Stack-wide outage (e.g. engine auto-started on login after a reboot but the
    # containers stayed Exited). Restore; do NOT overwrite the good-set this tick.
    Write-Log ("stack-wide outage: {0}/{1} good-set containers down (>= threshold {2}) - restoring" -f $downGood.Count, $good.Count, $threshold) 'CRIT'
    Restore-GoodSet -Good $good -Stack $stack
} else {
    if ($downGood.Count -gt 0) { Write-Log ("{0}/{1} good-set container(s) down (< threshold {2}: '{3}') - leaving to Docker restart policy / operator (likely a deploy)" -f $downGood.Count, $good.Count, $threshold, (($downGood.name) -join ', ')) 'WARN' }
    else { Write-Log ("stack OK - {0}/{1} good-set containers running" -f ($good.Count - $downGood.Count), $good.Count) 'OK' }
    # Refresh good-set as the UNION of the existing set + currently-running stack,
    # pruned to containers that still exist. Never shrinks on a transient single
    # down (so we don't "forget" it); picks up genuinely new containers; drops
    # only containers that have been removed entirely.
    $byName = @{}; foreach ($c in $stack) { $byName[$c.Name] = $c }
    $names = New-Object System.Collections.Generic.HashSet[string]
    foreach ($g in $good) { [void]$names.Add($g.name) }
    foreach ($r in $running) { [void]$names.Add($r.Name) }
    $merged = @(); foreach ($n in $names) { if ($byName.ContainsKey($n)) { $merged += $byName[$n] } }
    Save-GoodSet $merged
}
Write-Log 'watchdog tick done' 'INFO'
