# reclaim_exit_parity_log.ps1
#
# Thin wrapper around scripts/reclaim_exit_parity_log.sql -- the ONE-TIME heap
# reclaim for trading_exit_parity_log. Run in a LOW-ACTIVITY window AFTER
# migration 301 has applied and the writers are paused. See the header of
# reclaim_exit_parity_log.sql for the full runbook.
#
# Usage:
#   .\scripts\reclaim_exit_parity_log.ps1
#   .\scripts\reclaim_exit_parity_log.ps1 -Container chili-home-copilot-postgres-1 -Db chili -DbUser chili

param(
    [string]$Container = "chili-home-copilot-postgres-1",
    [string]$Db = "chili",
    [string]$DbUser = "chili"
)

$ErrorActionPreference = "Stop"
$sqlPath = Join-Path $PSScriptRoot "reclaim_exit_parity_log.sql"
if (-not (Test-Path $sqlPath)) {
    throw "Cannot find $sqlPath"
}

Write-Host "[reclaim] VACUUM FULL takes ACCESS EXCLUSIVE and blocks the table." -ForegroundColor Yellow
Write-Host "[reclaim] Confirm migration 301 is applied and writers are paused first." -ForegroundColor Yellow
Write-Host "[reclaim] Running against container=$Container db=$Db ..." -ForegroundColor Cyan

Get-Content -Raw $sqlPath | docker exec -i $Container psql -U $DbUser -d $Db -P pager=off
