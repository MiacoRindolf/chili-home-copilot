<#
.SYNOPSIS
  CHILI Docker hygiene - reap stale deploy-rollback containers, prune stale
  chili-app:main-clean-<sha> images, and trim the BuildKit cache.

.DESCRIPTION
  Invoked hourly by the "CHILI-Docker-Prune" scheduled task
  (LogonType Interactive as user 'rindo' - needs the Docker named pipe; SYSTEM
  cannot. RunLevel Limited / non-elevated.).

  The deploy flow builds a fresh ~1.4-3.8 GB `chili-app:main-clean-<sha>` image
  per deploy (several per hour) with no cleanup, which slowly fills the disk.
  This keeps the newest -KeepRecent main-clean tags and removes the rest, then
  trims build cache older than -KeepCacheHours.

  STAGE 1 - ROLLBACK CONTAINER RETENTION (added 2026-08-10)
  The deploy flow also *renames* the previous container out of the way
  (`chili-clean-recovery-scheduler` -> `...-scheduler-pre-<sha>-<stamp>`) as a
  one-click rollback, and never reaps it. 245 such containers had accumulated,
  the oldest ~2 months old. The containers themselves are small (~700 MB of
  writable layers total) but each one PINS its image, so the image stage below
  could never reclaim those layers - 142 GB had gone unreclaimable.

  Policy: per service (scheduler / web / brain / momentum-exec / broker-sync /
  backtest-worker / autotrader / market-snapshot-worker) keep the newest
  -KeepRollbacks rollbacks, remove the rest. Runs BEFORE the image stage so the
  layers it unpins are reclaimed in the same pass.

  SAFETY (containers):
   * Only containers matching a known *rollback* name pattern are ever
     considered. The canonical un-suffixed service name (`...-scheduler`) is the
     live slot and is always protected, running or not.
   * `docker rm` WITHOUT -f: Docker itself refuses to remove a running
     container, so a running service cannot be reaped even if misclassified.
   * WITHOUT -v: named volumes (e.g. chili_rh_agentic_secrets, referenced by 11
     rollbacks) are never touched.
   * Age floor -MinRollbackAgeDays uses max(CreatedAt, FinishedAt), NOT
     CreatedAt. A container that ran for weeks then got demoted an hour ago has
     an OLD CreatedAt - ranking on it would reap the newest rollback first.
     Observed deltas here reached 26 days.
   * Removal is batched (-RollbackBatchSize) and between batches the engine is
     re-pinged and the running set re-verified; any drift aborts the stage.
     Docker Desktop on this box has crashed on en-masse force-kills.
   * -NeverRemoveContainers is an explicit belt-and-braces deny list.

  SAFETY (images):
   * Never removes an image that is referenced by ANY container (`docker ps -a`),
     matched by image ID - recomputed AFTER stage 1.
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
    [int]$RmiBatchSize = 15,          # images per `docker rmi` call (see note below)
    [int]$RmiTimeoutSec = 600,        # a 2 GB image can take >2 min to delete here
    [string]$Repository = 'chili-app',
    [string]$TagPrefix = 'main-clean-',

    # ---- stage 1: rollback-container retention ----
    [int]$KeepRollbacks = 5,          # newest N rollback containers PER SERVICE
    [double]$MinRollbackAgeDays = 7,  # never reap a rollback younger than this
    [int]$RollbackBatchSize = 10,     # remove in batches; verify between each
    [switch]$SkipContainerPrune,      # disable stage 1 entirely
    # explicit deny list - names here are never removed regardless of age/rank
    [string[]]$NeverRemoveContainers = @('chili-clean-recovery-scheduler-prealarm'),
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
    # A silent `catch {}` here used to drop the audit trail without a trace: in the
    # 2026-08-10 reap the first 12 lines landed and every line after them vanished,
    # so the run had no record of what it removed. Retry, then fall back to a
    # sibling .err file, and never fail silently.
    $dir = Split-Path -Parent $LogPath
    try { if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null } } catch { }
    try {
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 5MB)) {
            Set-Content -Path $LogPath -Value (Get-Content $LogPath -Tail 2000) -Encoding utf8
        }
    } catch { }
    $err = $null
    foreach ($attempt in 1..3) {
        try {
            # AppendAllText opens/closes with plain FileShare semantics and survives
            # concurrent readers (tail -f, editors) better than Add-Content.
            [System.IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, [System.Text.Encoding]::UTF8)
            return
        } catch { $err = $_; Start-Sleep -Milliseconds (50 * $attempt) }
    }
    try {
        [System.IO.File]::AppendAllText("$LogPath.err",
            ("[{0}] LOG WRITE FAILED: {1} :: original line: {2}{3}" -f $ts, $err.Exception.Message, $line, [Environment]::NewLine),
            [System.Text.Encoding]::UTF8)
    } catch { }
    Write-Host ("[docker-prune] LOG WRITE FAILED: {0}" -f $err.Exception.Message)
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
        # Drain BOTH pipes while waiting. Reading only after WaitForExit deadlocks
        # as soon as output exceeds the ~4 KB pipe buffer: the child blocks on
        # write, we block on exit. That is what produced the
        #   "docker images failed: timeout"
        # on every run from 2026-07-26 onward (616 images ~= 60 KB of output) -
        # the image stage never ran, and 142 GB of unreferenced layers piled up.
        # `docker images` itself takes ~7 s from a shell.
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch { }; return @{ Ok = $false; Out = "timeout"; Err = "timeout" } }
        return @{ Ok = ($p.ExitCode -eq 0); Out = $outTask.GetAwaiter().GetResult().Trim(); Err = $errTask.GetAwaiter().GetResult().Trim() }
    } catch { return @{ Ok = $false; Out = ''; Err = $_.Exception.Message } }
}

Write-Log ("prune run (Execute={0}, KeepRecent={1}, KeepCacheHours={2})" -f [bool]$Execute, $KeepRecent, $KeepCacheHours) 'INFO'

# fail fast if the engine isn't up (the watchdog handles recovery; we just skip)
if (-not (Invoke-Docker @('version', '--format', '"{{.Server.Version}}"') -TimeoutSec 20).Ok) {
    Write-Log 'Docker engine not reachable - skipping this prune run' 'WARN'
    return
}

# ---- container inventory (ONE batched inspect, not N) ----------------------
# The old per-container `docker inspect` loop cost 251 process spawns / ~40 s and
# pushed the run into the 120 s `docker images` timeout seen in the Jul-26 logs.
# Docker stamps RFC3339 with 9 fractional digits (2026-08-10T19:41:28.123456789Z);
# .NET parses at most 7, so trim before handing it over. Zero time -> MinValue.
function ConvertTo-Dt {
    param([string]$Raw)
    [datetime]$dt = [datetime]::MinValue
    if (-not $Raw) { return $dt }
    $s = $Raw.Trim()
    if ($s.StartsWith('0001-01-01')) { return $dt }
    $s = [regex]::Replace($s, '(\.\d{1,7})\d*', '$1')
    if ([datetime]::TryParse($s, [ref]$dt)) { return $dt.ToLocalTime() }
    return [datetime]::MinValue
}

function Get-ContainerInventory {
    $out = Invoke-Docker @('ps', '-a', '--no-trunc', '--format', '"{{.ID}}"') -TimeoutSec 120
    if (-not $out.Ok -or -not $out.Out) { return @() }
    $cids = @($out.Out -split "`n" | ForEach-Object { $_.Trim('"', ' ') } | Where-Object { $_ })
    $inv = @()
    # chunk: 251 * 64-char ids would otherwise approach the Windows cmdline cap
    for ($i = 0; $i -lt $cids.Count; $i += 40) {
        $chunk = $cids[$i..([Math]::Min($i + 39, $cids.Count - 1))]
        $fmt = '"{{.Name}}|{{.Id}}|{{.Image}}|{{.State.Status}}|{{.Created}}|{{.State.FinishedAt}}"'
        $res = Invoke-Docker (@('inspect', '-f', $fmt) + $chunk) -TimeoutSec 180
        if (-not $res.Ok) { Write-Log ("inspect chunk failed: {0}" -f $res.Err) 'WARN'; continue }
        foreach ($line in ($res.Out -split "`n")) {
            $p = $line.Trim('"', ' ') -split '\|'
            if ($p.Count -lt 6) { continue }
            $created = ConvertTo-Dt $p[4]
            $finished = ConvertTo-Dt $p[5]
            # max(Created, FinishedAt) - see SAFETY note in the header
            $last = $created
            if ($finished -gt $last) { $last = $finished }
            $inv += [pscustomobject]@{
                Name   = $p[0].TrimStart('/')
                Id     = $p[1]
                Image  = ($p[2] -replace '^sha256:', '')
                Status = $p[3]
                Last   = $last
            }
        }
    }
    return $inv
}

# service token -> the container is a deploy rollback for that service.
# Returns $null for anything that is NOT a rollback (replay clones, compose
# services, bench runs) - those are out of scope and never touched.
$RollbackServices = @('momentum-exec', 'backtest-worker', 'broker-sync',
    'market-snapshot-worker', 'scheduler', 'web', 'brain', 'autotrader')

function Get-RollbackService {
    param([string]$Name)
    foreach ($pre in @('chili-clean-recovery-', 'chili-recovery-')) {
        if ($Name.StartsWith($pre)) {
            $rest = $Name.Substring($pre.Length)
            foreach ($s in $RollbackServices) {
                # `-<svc>-...` or `-<svc>....` only; the bare `-<svc>` canonical
                # name is the LIVE slot and deliberately returns $null (protected)
                if ($rest.StartsWith("$s-") -or $rest.StartsWith("$s.")) { return $s }
            }
            return $null
        }
    }
    if ($Name.StartsWith('chili-web-rollback-')) { return 'web' }      # current web naming
    if ($Name.StartsWith('chili-sched-prev-')) { return 'scheduler' }  # legacy sched naming
    return $null
}

$inventory = Get-ContainerInventory
Write-Log ("container inventory: {0} total, {1} running" -f $inventory.Count,
    (@($inventory | Where-Object { $_.Status -eq 'running' }).Count)) 'INFO'

# ---- STAGE 1: rollback container retention --------------------------------
if ($SkipContainerPrune) {
    Write-Log 'container prune skipped (-SkipContainerPrune)' 'INFO'
} elseif ($inventory.Count -eq 0) {
    Write-Log 'container inventory empty - skipping container prune' 'WARN'
} else {
    $runningBefore = @($inventory | Where-Object { $_.Status -eq 'running' } |
        ForEach-Object { $_.Name } | Sort-Object)
    $deny = New-Object System.Collections.Generic.HashSet[string] (
        [string[]]$NeverRemoveContainers, [StringComparer]::OrdinalIgnoreCase)

    $cutoff = (Get-Date).AddDays(-$MinRollbackAgeDays)
    $rollbacks = @()
    foreach ($c in $inventory) {
        $svc = Get-RollbackService -Name $c.Name
        if ($svc) { $rollbacks += ($c | Add-Member -NotePropertyName Service -NotePropertyValue $svc -PassThru) }
    }
    Write-Log ("rollback containers: {0} (of {1})" -f $rollbacks.Count, $inventory.Count) 'INFO'

    $victims = @()
    foreach ($grp in ($rollbacks | Group-Object Service)) {
        $ranked = @($grp.Group | Sort-Object Last -Descending)
        for ($i = 0; $i -lt $ranked.Count; $i++) {
            $c = $ranked[$i]
            if ($c.Status -eq 'running' -or $c.Status -eq 'restarting' -or $c.Status -eq 'paused') { continue }
            if ($deny.Contains($c.Name)) { Write-Log ("keep {0} (deny list)" -f $c.Name) 'INFO'; continue }
            if ($i -lt $KeepRollbacks) { continue }                       # newest N per service
            if ($c.Last -gt $cutoff) { continue }                         # age floor
            $victims += $c
        }
        Write-Log ("  {0,-22} total={1,3} keep={2,3} remove={3,3}" -f $grp.Name, $ranked.Count,
            ($ranked.Count - @($victims | Where-Object { $_.Service -eq $grp.Name }).Count),
            @($victims | Where-Object { $_.Service -eq $grp.Name }).Count) 'INFO'
    }

    if ($victims.Count -eq 0) {
        Write-Log 'no rollback containers past retention' 'OK'
    } elseif (-not $Execute) {
        Write-Log ("[dry-run] would remove {0} rollback container(s)" -f $victims.Count) 'INFO'
        foreach ($v in ($victims | Sort-Object Last)) {
            Write-Log ("[dry-run]   rm {0} (svc={1} last={2})" -f $v.Name, $v.Service,
                $v.Last.ToString('s')) 'INFO'
        }
    } else {
        # oldest first, in batches, verifying the engine + running set between each
        $ordered = @($victims | Sort-Object Last)
        $rm = 0; $fail = 0; $aborted = $false
        for ($i = 0; $i -lt $ordered.Count; $i += $RollbackBatchSize) {
            $batch = $ordered[$i..([Math]::Min($i + $RollbackBatchSize - 1, $ordered.Count - 1))]
            foreach ($v in $batch) {
                # NO -f (a running container cannot be reaped), NO -v (named volumes stay)
                $res = Invoke-Docker @('rm', $v.Name) -TimeoutSec 60
                if ($res.Ok) { $rm++ }
                else { Write-Log ("rm {0} failed: {1}" -f $v.Name, $res.Err) 'WARN'; $fail++ }
            }
            # --- verify: engine alive and every previously-running container still up ---
            if (-not (Invoke-Docker @('version', '--format', '"{{.Server.Version}}"') -TimeoutSec 30).Ok) {
                Write-Log 'engine unreachable after batch - ABORTING container prune' 'CRIT'
                $aborted = $true; break
            }
            $now = Invoke-Docker @('ps', '--format', '"{{.Names}}"') -TimeoutSec 60
            $runningNow = @($now.Out -split "`n" | ForEach-Object { $_.Trim('"', ' ') } |
                Where-Object { $_ } | Sort-Object)
            $lost = @($runningBefore | Where-Object { $runningNow -notcontains $_ })
            if ($lost.Count -gt 0) {
                Write-Log ("running container(s) DISAPPEARED: {0} - ABORTING" -f ($lost -join ', ')) 'CRIT'
                $aborted = $true; break
            }
            Write-Log ("batch ok: removed={0}/{1}, running={2}" -f $rm, $ordered.Count,
                $runningNow.Count) 'INFO'
            Start-Sleep -Milliseconds 400   # be gentle on the engine
        }
        Write-Log ("containers: removed=$rm failed=$fail aborted=$aborted") $(if ($aborted) { 'CRIT' } else { 'OK' })
        if ($rm -gt 0) { $inventory = Get-ContainerInventory }   # refresh before the image stage
    }
}

# ---- container-referenced image IDs (never delete these) -------------------
# recomputed from the POST-stage-1 inventory so freed layers become eligible
$referenced = New-Object System.Collections.Generic.HashSet[string]
foreach ($c in $inventory) { if ($c.Image) { [void]$referenced.Add($c.Image) } }
Write-Log ("container-referenced image IDs: {0}" -f $referenced.Count) 'INFO'

# ---- enumerate main-clean tags, newest first ------------------------------
# 300 s, not the 120 s default: with 600+ images this call timed out on every run
# from 2026-07-26 onward, which silently disabled the whole image stage.
$imgRes = Invoke-Docker @('images', $Repository, '--no-trunc', '--format', '"{{.ID}}|{{.Tag}}|{{.CreatedAt}}"') -TimeoutSec 300
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

    $skippedInUse = 0
    $eligible = @()
    foreach ($c in $candidates) {
        if ($referenced.Contains($c.Id)) { $skippedInUse++; continue }
        $eligible += $c
    }
    Write-Log ("{0} eligible, {1} skipped (container-referenced)" -f $eligible.Count, $skippedInUse) 'INFO'

    if (-not $Execute) {
        foreach ($c in $eligible) { Write-Log ("[dry-run] would remove {0} ({1})" -f $c.Ref, $c.Created.ToString('s')) 'INFO' }
    } else {
        # Batched `docker rmi ref1 ref2 ...`, NOT one call per image. Deleting a
        # ~2 GB chili-app image takes >2 min here, so the per-image call blew the
        # 120 s client timeout on every image: the client got killed, the daemon
        # finished the delete anyway, and the loop crawled at ~1 image / 2 min
        # (475 images would have taken 16 h). One call per batch with a generous
        # timeout lets the daemon stream through them.
        $removed = 0; $failed = 0
        for ($i = 0; $i -lt $eligible.Count; $i += $RmiBatchSize) {
            $batch = $eligible[$i..([Math]::Min($i + $RmiBatchSize - 1, $eligible.Count - 1))]
            $rmi = Invoke-Docker (@('rmi') + @($batch.Ref)) -TimeoutSec $RmiTimeoutSec   # NO -f: safety net
            if ($rmi.Ok) { $removed += $batch.Count }
            else {
                # partial success is normal: in-use/conflict images are refused
                # individually while the rest of the batch still gets deleted
                $gone = @($rmi.Out -split "`n" | Where-Object { $_ -match 'Untagged|Deleted' }).Count
                $removed += [Math]::Min($gone, $batch.Count)
                $failed += [Math]::Max(0, $batch.Count - $gone)
                Write-Log ("rmi batch partial: {0}" -f (($rmi.Err -split "`n" | Select-Object -First 1))) 'WARN'
            }
            Write-Log ("images {0}/{1} processed" -f [Math]::Min($i + $RmiBatchSize, $eligible.Count), $eligible.Count) 'INFO'
            # Docker Desktop on this box has died under sustained bulk operations;
            # re-ping the engine and stop cleanly rather than pile on.
            if (-not (Invoke-Docker @('version', '--format', '"{{.Server.Version}}"') -TimeoutSec 30).Ok) {
                Write-Log 'engine unreachable mid-image-prune - stopping early' 'CRIT'; break
            }
        }
        Write-Log ("images: removed=$removed skipped-in-use=$skippedInUse failed=$failed") 'OK'
    }
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
