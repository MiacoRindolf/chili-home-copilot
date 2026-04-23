# Recreate `chili_staging` from a fresh pg_dump of the live `chili` database (no backup file).
# Use when a .dump file is missing or you want staging to match prod without relying on the daily backup.
# Same drop/create/restore flow as refresh_staging_from_backup.ps1; heavier read load on `chili` during pg_dump.
#
# See docs/STAGING_DATABASE.md.
# Optional: -ParallelJobs 4  →  pg_restore -j 4 (0 = omit -j)

[CmdletBinding()]
param(
    [string] $Container  = 'chili-home-copilot-postgres-1',
    [string] $SourceDb   = 'chili',
    [string] $StagingDb  = 'chili_staging',
    [string] $PostgresDb = 'postgres',
    [string] $LogBackupDir = 'D:\CHILI-Docker\backup\logs',
    [int]    $ParallelJobs = 0
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $LogBackupDir)) { New-Item -ItemType Directory -Path $LogBackupDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log   = Join-Path $LogBackupDir "staging_refresh_live_$stamp.log"

function Write-Log([string] $m) {
    $line = "[$(Get-Date -Format o)] $m"
    $line | Tee-Object -FilePath $log -Append
}

try {
    Write-Log "starting; container=$Container source=$SourceDb -> $StagingDb"

    $tmpInContainer = '/tmp/chili_staging_source.dump'
    & docker exec $Container pg_dump -U chili -d $SourceDb -Fc --no-owner --no-privileges -f $tmpInContainer 2>>$log
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAILED: pg_dump exit $LASTEXITCODE"
        exit 1
    }
    Write-Log "pg_dump OK -> $tmpInContainer"

    $term = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datistemplate = false AND datname = '$StagingDb' AND pid <> pg_backend_pid();"
    & docker exec $Container psql -U chili -d $PostgresDb -c $term 2>>$log
    & docker exec $Container psql -U chili -d $PostgresDb -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $StagingDb WITH (FORCE);" 2>>$log
    if ($LASTEXITCODE -ne 0) {
        & docker exec $Container rm -f $tmpInContainer 2>>$log | Out-Null
        Write-Log "FAILED: DROP DATABASE (psql exit $LASTEXITCODE)"
        exit 1
    }
    & docker exec $Container psql -U chili -d $PostgresDb -v ON_ERROR_STOP=1 -c "CREATE DATABASE $StagingDb;" 2>>$log
    if ($LASTEXITCODE -ne 0) {
        & docker exec $Container rm -f $tmpInContainer 2>>$log | Out-Null
        Write-Log "FAILED: CREATE DATABASE (psql exit $LASTEXITCODE)"
        exit 1
    }

    if ($ParallelJobs -gt 1) {
        & docker exec $Container pg_restore -U chili -d $StagingDb --no-owner --no-privileges -j $ParallelJobs $tmpInContainer 2>>$log
    } else {
        & docker exec $Container pg_restore -U chili -d $StagingDb --no-owner --no-privileges $tmpInContainer 2>>$log
    }
    $rc = $LASTEXITCODE
    & docker exec $Container rm -f $tmpInContainer 2>>$log | Out-Null

    if ($rc -ne 0) {
        Write-Log "FAILED: pg_restore exit $rc"
        exit 1
    }

    Write-Log "OK: $StagingDb restored from live $SourceDb"
    exit 0
} catch {
    Write-Log "FAILED: $_"
    exit 1
}
