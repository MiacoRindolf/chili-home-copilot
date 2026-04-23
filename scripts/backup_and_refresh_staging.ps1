# Chain: daily pg_dump of `chili` (backup_chili_db.ps1) then full refresh of `chili_staging` from the new dump.
# Use as a single Task Scheduler target if you want one daily job for both steps.
# See scripts/backup_chili_db.ps1, scripts/refresh_staging_from_backup.ps1, docs/STAGING_DATABASE.md

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
& (Join-Path $here 'backup_chili_db.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $here 'refresh_staging_from_backup.ps1')
exit $LASTEXITCODE
