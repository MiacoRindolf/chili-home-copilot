# Daily pg_dump of the live chili database.
# - Writes to D:\CHILI-Docker\backup\chili_YYYYMMDD_HHmm.dump (custom format, compressed)
# - Keeps the 14 most recent backups; older dumps auto-pruned.
# - Runs pg_dump inside the postgres container so no local psql install is needed.
#
# Staging refresh (run after this backup, e.g. +30 min): copies the latest .dump into
# database `chili_staging` for production-shaped script dry-runs. See:
#   scripts/refresh_staging_from_backup.ps1
#   docs/STAGING_DATABASE.md
#
# Register as a daily scheduled task with (run from an elevated PowerShell):
#   schtasks /Create /TN "CHILI pg_dump daily" /SC DAILY /ST 03:30 ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\dev\chili-home-copilot\scripts\backup_chili_db.ps1" ^
#     /RL HIGHEST /F


# ── MARKET-WINDOW GUARD (added 2026-06-11): the momentum lane now trades the FULL
# US data session (premarket 4:00 AM ET -> after-hours 8:00 PM ET = 01:00-17:00 PT
# on this box). Heavy DB/CPU work in that window contends with LIVE trading (this
# task's old slot landed mid-premarket / pre-open). Inside the window: defer — the
# CHILI-Evening-* companion task runs this same script after the session closes.
$__nowLocal = Get-Date
if ($__nowLocal.DayOfWeek -ne 'Saturday' -and $__nowLocal.DayOfWeek -ne 'Sunday') {
    $__mod = $__nowLocal.Hour * 60 + $__nowLocal.Minute
    if ($__mod -ge 60 -and $__mod -lt 1020) {
        Write-Output "[market-window-guard] deferred (PT $($__nowLocal.ToString('HH:mm')) inside data session); evening task covers this."
        exit 0
    }
}
$ErrorActionPreference = 'Stop'

$Container  = 'chili-home-copilot-postgres-1'
$DbName     = 'chili'
$DbUser     = 'chili'
$BackupDir  = 'D:\CHILI-Docker\backup'
$KeepCount  = 14

if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir | Out-Null }

$stamp  = Get-Date -Format 'yyyyMMdd_HHmm'
$dump   = Join-Path $BackupDir "chili_$stamp.dump"
$logDir = Join-Path $BackupDir 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log    = Join-Path $logDir "backup_$stamp.log"

"[$(Get-Date -Format o)] starting pg_dump -> $dump" | Tee-Object -FilePath $log

# -Fc = custom format (compressed, selective restore via pg_restore -t).
# Dump inside the container to /tmp, then docker cp out (preserves binary cleanly on Windows).
$tmpInContainer = "/tmp/chili_$stamp.dump"
# The docker calls below redirect stderr into the log. Under
# $ErrorActionPreference='Stop', PowerShell 5.1 turns any redirected native
# stderr line into a terminating NativeCommandError - docker cp reports
# "Successfully copied ..." on stderr, which used to kill the script right
# here, BEFORE the in-container cleanup and the local prune (so /tmp dumps
# and >14 local dumps silently accumulated). Exit codes are checked
# explicitly instead.
$ErrorActionPreference = 'Continue'
& docker exec $Container pg_dump -U $DbUser -d $DbName -Fc --no-owner --no-privileges -f $tmpInContainer 2>>$log
if ($LASTEXITCODE -ne 0) {
    "[$(Get-Date -Format o)] FAILED: pg_dump exit $LASTEXITCODE" | Tee-Object -FilePath $log -Append
    exit 1
}
& docker cp "${Container}:$tmpInContainer" $dump 2>>$log
if ($LASTEXITCODE -ne 0) {
    "[$(Get-Date -Format o)] FAILED: docker cp exit $LASTEXITCODE" | Tee-Object -FilePath $log -Append
    exit 1
}
& docker exec $Container rm -f $tmpInContainer 2>>$log | Out-Null
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $dump) -or (Get-Item $dump).Length -lt 1MB) {
    "[$(Get-Date -Format o)] FAILED: dump missing or < 1MB" | Tee-Object -FilePath $log -Append
    exit 1
}

"[$(Get-Date -Format o)] dump OK ($([math]::Round((Get-Item $dump).Length/1MB,1)) MB)" |
    Tee-Object -FilePath $log -Append

# Prune: keep $KeepCount most recent chili_*.dump files.
Get-ChildItem $BackupDir -Filter 'chili_*.dump' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $KeepCount |
    ForEach-Object {
        "[$(Get-Date -Format o)] pruning $($_.Name)" | Tee-Object -FilePath $log -Append
        Remove-Item $_.FullName -Force
    }

"[$(Get-Date -Format o)] done" | Tee-Object -FilePath $log -Append
