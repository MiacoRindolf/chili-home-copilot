# Recreate `chili_staging` from the newest custom-format dump produced by backup_chili_db.ps1.
# - Finds the latest D:\CHILI-Docker\backup\chili_*.dump
# - DROP DATABASE + CREATE DATABASE chili_staging (with FORCE terminate on PG13+)
# - pg_restore --no-owner --no-privileges (same spirit as the backup script)
#
# Run AFTER the daily backup task (e.g. backup 03:30, this job 04:00). See docs/STAGING_DATABASE.md.
# Register a scheduled task (run from elevated PowerShell), e.g.:
#   schtasks /Create /TN "CHILI refresh staging" /SC DAILY /ST 04:00 /RL HIGHEST /F /TR "powershell -ExecutionPolicy Bypass -File C:\dev\chili-home-copilot\scripts\refresh_staging_from_backup.ps1"
#
# On restore failure, chili_staging may be empty or missing — re-run after fixing the dump path or container.
# Optional: -ParallelJobs 4  →  pg_restore -j 4 (faster on large DBs; 0 = omit -j)

[CmdletBinding()]
param(
    [string] $Container = 'chili-home-copilot-postgres-1',
    [string] $BackupDir  = 'D:\CHILI-Docker\backup',
    [string] $StagingDb  = 'chili_staging',
    [string] $PostgresDb = 'postgres',
    [int]    $ParallelJobs = 0
)

$ErrorActionPreference = 'Stop'

$logDir = Join-Path $BackupDir 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log   = Join-Path $logDir "staging_refresh_$stamp.log"

function Write-Log([string] $m) {
    $line = "[$(Get-Date -Format o)] $m"
    $line | Tee-Object -FilePath $log -Append
}

try {
    Write-Log "starting; container=$Container backupDir=$BackupDir staging=$StagingDb"

    $latest = Get-ChildItem -Path $BackupDir -Filter 'chili_*.dump' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if (-not $latest) {
        Write-Log "FAILED: no chili_*.dump under $BackupDir (run backup_chili_db.ps1 first)"
        exit 1
    }
    Write-Log "using dump: $($latest.FullName) ($([math]::Round($latest.Length/1MB,1)) MB)"

    $tmpInContainer = '/tmp/staging_restore.dump'
    & docker cp $latest.FullName "${Container}:$tmpInContainer" 2>>$log
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAILED: docker cp exit $LASTEXITCODE"
        exit 1
    }

    # pg_stat_activity has datname/pid only — not datistemplate (that is on pg_database)
    $term = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$StagingDb' AND pid <> pg_backend_pid();"
    & docker exec $Container psql -U chili -d $PostgresDb -c $term 2>>$log
    & docker exec $Container psql -U chili -d $PostgresDb -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $StagingDb WITH (FORCE);" 2>>$log
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAILED: DROP DATABASE (psql exit $LASTEXITCODE)"
        exit 1
    }
    & docker exec $Container psql -U chili -d $PostgresDb -v ON_ERROR_STOP=1 -c "CREATE DATABASE $StagingDb;" 2>>$log
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAILED: CREATE DATABASE (psql exit $LASTEXITCODE)"
        exit 1
    }

    Write-Log "pg_restore into $StagingDb (may take several minutes) parallel=$ParallelJobs..."
    if ($ParallelJobs -gt 1) {
        & docker exec $Container pg_restore -U chili -d $StagingDb --no-owner --no-privileges -j $ParallelJobs $tmpInContainer 2>>$log
    } else {
        & docker exec $Container pg_restore -U chili -d $StagingDb --no-owner --no-privileges $tmpInContainer 2>>$log
    }
    $restoreCode = $LASTEXITCODE
    & docker exec $Container rm -f $tmpInContainer 2>>$log | Out-Null

    if ($restoreCode -ne 0) {
        Write-Log "FAILED: pg_restore exit $restoreCode — $StagingDb may be empty; fix issue and re-run"
        exit 1
    }

    Write-Log "OK: $StagingDb refreshed from $($latest.Name)"
    exit 0
} catch {
    Write-Log "FAILED: $_"
    exit 1
}
