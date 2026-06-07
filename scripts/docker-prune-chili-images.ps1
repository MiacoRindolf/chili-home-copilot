<#
.SYNOPSIS
    Prune stale chili-app:main-clean-* Docker images + idle build cache so the
    Docker WSL data store (on D:) does not slowly fill from per-deploy images.

.DESCRIPTION
    The deploy flow builds a fresh ~1.4-3.8GB chili-app:main-clean-<sha> image per
    deploy (~3/hour) and never cleans up. This script keeps the N most-recent
    main-clean tags as rollback targets, ALWAYS keeps any image referenced by a
    container (running OR stopped), and removes the rest. It also reclaims
    dangling images and caps the BuildKit cache.

    SAFETY: image removal uses `docker rmi` WITHOUT -f. Even if the protection
    filter were wrong, docker refuses to delete an image a container references.
    Nothing here stops or restarts running containers, so it is safe to run live.

    Default mode is a DRY RUN. Pass -Execute to actually remove.

.PARAMETER KeepRecent
    Number of most-recent main-clean tags to keep as rollback targets. Default 15
    (~5h of deploy history). Container-referenced images are kept regardless.

.PARAMETER KeepCacheHours
    Trim BuildKit cache entries idle longer than this many hours (keeps recent
    cache so frequent rebuilds stay fast; evicts old cache to bound growth).
    Default 12. NOTE: an age filter is used rather than --max-used-space because
    BuildKit's size cap will not evict recently-touched entries (it reclaimed 0B
    in testing against a 55GB store), so the cap does not reliably bound D:.

.PARAMETER Execute
    Actually remove. Without it the script only prints what it would do.

.PARAMETER LogPath
    Append a one-line summary here (for the scheduled task). Default
    %LOCALAPPDATA%\chili\docker-prune.log

.EXAMPLE
    .\scripts\docker-prune-chili-images.ps1                 # dry run
    .\scripts\docker-prune-chili-images.ps1 -Execute        # do it
    .\scripts\docker-prune-chili-images.ps1 -Execute -KeepRecent 20
#>
[CmdletBinding()]
param(
    [int]    $KeepRecent      = 15,
    [int]    $KeepCacheHours  = 12,
    [switch] $Execute,
    [string] $LogPath = (Join-Path $env:LOCALAPPDATA 'chili\docker-prune.log')
)

$ErrorActionPreference = 'Stop'
$mode = if ($Execute) { 'EXECUTE' } else { 'DRY-RUN' }
Write-Host "[docker-prune] mode=$mode KeepRecent=$KeepRecent KeepCacheHours=$KeepCacheHours"

# --- 1. Protected image IDs: anything a container (running or stopped) references ---
$protected = New-Object System.Collections.Generic.HashSet[string]
$containerImages = docker ps -a --format '{{.Image}}' | Sort-Object -Unique
foreach ($img in $containerImages) {
    if ([string]::IsNullOrWhiteSpace($img)) { continue }
    try {
        $id = (docker image inspect $img --format '{{.Id}}' 2>$null)
        if ($id) { [void]$protected.Add($id.Trim()) }
    } catch { }
}
Write-Host "[docker-prune] $($protected.Count) image IDs are container-referenced (protected)"

# --- 2. All main-clean tags, newest first (sort by CreatedAt prefix = chronological) ---
$rows = docker images chili-app --format '{{.Tag}}|{{.ID}}|{{.CreatedAt}}' |
    Where-Object { $_ -like 'main-clean-*' } |
    ForEach-Object {
        $p = $_ -split '\|', 3
        [pscustomobject]@{ Tag = $p[0]; ShortId = $p[1]; Created = $p[2]; SortKey = $p[2].Substring(0, [Math]::Min(19, $p[2].Length)) }
    } | Sort-Object SortKey -Descending

Write-Host "[docker-prune] $($rows.Count) main-clean tags found"

# --- 3. Classify: keep newest N, keep container-referenced, remove the rest ---
$toRemove = @()
$keptRecent = 0; $keptInUse = 0
for ($i = 0; $i -lt $rows.Count; $i++) {
    $row = $rows[$i]
    if ($i -lt $KeepRecent) { $keptRecent++; continue }
    $fullId = $null
    try { $fullId = (docker image inspect "chili-app:$($row.Tag)" --format '{{.Id}}' 2>$null).Trim() } catch { }
    if ($fullId -and $protected.Contains($fullId)) {
        $keptInUse++
        continue
    }
    $toRemove += $row
}

Write-Host "[docker-prune] keep(recent)=$keptRecent  keep(in-use,older)=$keptInUse  remove=$($toRemove.Count)"
foreach ($r in $toRemove) { Write-Host ("  remove  {0}  ({1})" -f $r.Tag, $r.Created) }

# --- 4. Execute removals (rmi WITHOUT -f: docker still refuses container-referenced) ---
$removedOk = 0; $removedFail = 0
if ($Execute) {
    foreach ($r in $toRemove) {
        try {
            docker rmi "chili-app:$($r.Tag)" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $removedOk++ } else { $removedFail++ ; Write-Host "  [skip] $($r.Tag) (docker refused -- likely became referenced)" }
        } catch { $removedFail++ ; Write-Host "  [skip] $($r.Tag) ($_)" }
    }

    Write-Host "[docker-prune] pruning dangling images..."
    docker image prune -f | Out-Null

    Write-Host "[docker-prune] trimming build cache idle > $KeepCacheHours h..."
    docker builder prune -f --filter "until=${KeepCacheHours}h" | Out-Null
} else {
    Write-Host "[docker-prune] DRY RUN -- pass -Execute to remove the above, prune dangling, and cap build cache."
}

# --- 5. Log one line for the scheduled task ---
if ($Execute) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line  = "$stamp mode=$mode removed=$removedOk failed=$removedFail keptRecent=$keptRecent keptInUse=$keptInUse"
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
        Write-Host "[docker-prune] $line  (logged to $LogPath)"
    } catch { Write-Host "[docker-prune] $line  (log write failed: $_)" }
}

Write-Host "[docker-prune] done."
