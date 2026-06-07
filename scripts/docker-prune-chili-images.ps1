<#
.SYNOPSIS
  CHILI Docker image hygiene - prune stale chili-app:main-clean-<sha> images and
  trim the BuildKit cache.

.DESCRIPTION
  Invoked hourly by the "CHILI-Docker-Prune" scheduled task
  (LogonType Interactive as user 'rindo' - needs the Docker named pipe; SYSTEM
  cannot. RunLevel Limited / non-elevated.).

  The deploy flow builds a fresh ~1.4-3.8 GB `chili-app:main-clean-<sha>` image
  per deploy (several per hour) with no cleanup, which slowly fills the disk.
  This keeps the newest -KeepRecent main-clean tags and removes the rest, then
  trims build cache older than -KeepCacheHours.

  SAFETY:
   * Never removes an image that is referenced by ANY container (`docker ps -a`),
     matched by image ID.
   * Uses `docker rmi` WITHOUT -f as a second safety net (Docker itself refuses
     to delete an in-use image).
   * Without -Execute it only REPORTS what it would remove (dry run).
   * Build cache is trimmed with `--filter until=<h>h` (NOT --max-used-space,
     which reclaimed 0 B in testing - BuildKit won't evict recent entries).

  Background: [[project_docker_disk_hygiene]], [[project_docker_deploy_model]].
#>
[CmdletBinding()]
param(
    [switch]$Execute,                 # without this, dry-run / report only
    [int]$KeepRecent = 15,            # newest N main-clean tags to keep
    [int]$KeepCacheHours = 12,        # trim build cache older than this
    [string]$Repository = 'chili-app',
    [string]$TagPrefix = 'main-clean-',
    [string]$DockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe',
    # Absolute repo root - anchors the log path deterministically regardless of
    # $PSScriptRoot / cwd (the scheduled-task context mis-resolves relative paths).
    [string]$RepoRoot = 'D:\dev\chili-home-copilot',
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
if (-not $LogPath) { $LogPath = Join-Path $RepoRoot 'logs\docker-prune.log' }

function Write-Log {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'CRIT', 'OK')] [string]$Level = 'INFO')
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = "[$ts][docker-prune][$Level] $Message"
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
    param([string[]]$DockerArgs, [int]$TimeoutSec = 120)
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $DockerExe
        $psi.Arguments = ($DockerArgs -join ' ')
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch { }; return @{ Ok = $false; Out = "timeout"; Err = "timeout" } }
        return @{ Ok = ($p.ExitCode -eq 0); Out = $p.StandardOutput.ReadToEnd().Trim(); Err = $p.StandardError.ReadToEnd().Trim() }
    } catch { return @{ Ok = $false; Out = ''; Err = $_.Exception.Message } }
}

Write-Log ("prune run (Execute={0}, KeepRecent={1}, KeepCacheHours={2})" -f [bool]$Execute, $KeepRecent, $KeepCacheHours) 'INFO'

# fail fast if the engine isn't up (the watchdog handles recovery; we just skip)
if (-not (Invoke-Docker @('version', '--format', '"{{.Server.Version}}"') -TimeoutSec 20).Ok) {
    Write-Log 'Docker engine not reachable - skipping this prune run' 'WARN'
    return
}

# ---- container-referenced image IDs (never delete these) -------------------
$referenced = New-Object System.Collections.Generic.HashSet[string]
$ids = Invoke-Docker @('ps', '-a', '-q')
if ($ids.Ok -and $ids.Out) {
    foreach ($cid in ($ids.Out -split "`n")) {
        $cid = $cid.Trim(); if (-not $cid) { continue }
        $img = (Invoke-Docker @('inspect', '-f', '"{{.Image}}"', $cid)).Out.Trim('"', ' ')
        if ($img) { [void]$referenced.Add(($img -replace '^sha256:', '')) }
    }
}
Write-Log ("container-referenced image IDs: {0}" -f $referenced.Count) 'INFO'

# ---- enumerate main-clean tags, newest first ------------------------------
$imgRes = Invoke-Docker @('images', $Repository, '--no-trunc', '--format', '"{{.ID}}|{{.Tag}}|{{.CreatedAt}}"')
if (-not $imgRes.Ok) { Write-Log "docker images failed: $($imgRes.Err)" 'CRIT'; return }

$rows = @()
foreach ($line in ($imgRes.Out -split "`n")) {
    $parts = $line.Trim('"', ' ') -split '\|'
    if ($parts.Count -lt 3) { continue }
    if ($parts[1] -notlike "$TagPrefix*") { continue }
    $rows += [pscustomobject]@{
        Id      = ($parts[0] -replace '^sha256:', '')
        Tag     = $parts[1]
        Ref     = "$Repository`:$($parts[1])"
        Created = [datetime]::Parse(($parts[2] -replace ' [A-Z]{3,4}$', ''))
    }
}
$rows = $rows | Sort-Object Created -Descending
Write-Log ("found {0} {1}{2}* tags" -f $rows.Count, $Repository, $TagPrefix) 'INFO'

if ($rows.Count -le $KeepRecent) {
    Write-Log ("nothing to remove (<= KeepRecent={0})" -f $KeepRecent) 'OK'
} else {
    $keep = $rows | Select-Object -First $KeepRecent
    $candidates = $rows | Select-Object -Skip $KeepRecent
    Write-Log ("keeping newest {0}, {1} older candidate(s)" -f $keep.Count, $candidates.Count) 'INFO'

    $removed = 0; $skippedInUse = 0; $failed = 0
    foreach ($c in $candidates) {
        if ($referenced.Contains($c.Id)) {
            Write-Log ("skip {0} (container-referenced)" -f $c.Ref) 'INFO'; $skippedInUse++; continue
        }
        if (-not $Execute) { Write-Log ("[dry-run] would remove {0} ({1})" -f $c.Ref, $c.Created.ToString('s')) 'INFO'; continue }
        $rmi = Invoke-Docker @('rmi', $c.Ref)            # NO -f: safety net
        if ($rmi.Ok) { Write-Log ("removed {0}" -f $c.Ref) 'OK'; $removed++ }
        elseif ($rmi.Err -match 'image is being used|conflict|in use') { Write-Log ("skip {0} (in use): {1}" -f $c.Ref, $rmi.Err) 'INFO'; $skippedInUse++ }
        else { Write-Log ("rmi {0} failed: {1}" -f $c.Ref, $rmi.Err) 'WARN'; $failed++ }
    }
    if ($Execute) { Write-Log ("images: removed=$removed skipped-in-use=$skippedInUse failed=$failed") 'OK' }
}

# ---- trim build cache -----------------------------------------------------
if ($Execute) {
    $bc = Invoke-Docker @('builder', 'prune', '-f', '--filter', "until=${KeepCacheHours}h") -TimeoutSec 300
    if ($bc.Ok) { Write-Log ("builder prune (until=${KeepCacheHours}h): {0}" -f (($bc.Out -split "`n") | Select-Object -Last 1)) 'OK' }
    else { Write-Log ("builder prune failed: {0}" -f $bc.Err) 'WARN' }
} else {
    Write-Log ("[dry-run] would run: builder prune -f --filter until=${KeepCacheHours}h") 'INFO'
}

$df = Invoke-Docker @('system', 'df', '--format', '"{{.Type}}: {{.Size}} (reclaimable {{.Reclaimable}})"')
if ($df.Ok) { foreach ($l in ($df.Out -split "`n")) { Write-Log ("df: {0}" -f $l.Trim('"', ' ')) 'INFO' } }
Write-Log 'prune run done' 'INFO'
