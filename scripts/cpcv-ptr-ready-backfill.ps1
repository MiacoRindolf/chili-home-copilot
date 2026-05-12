# Enqueue CPCV gate work for PTR-ready ScanPatterns that never received a
# backtest_completed brain_work_event. This is the missing historical bridge
# after Phase 1c: Phase 1c replays existing events; this script creates the
# absent events for patterns that already have enough PatternTradeRow evidence.

[CmdletBinding()]
param(
    [int]$MinTrades = 30,
    [int]$MaxRows = 0,
    [bool]$DryRun = $true
)

$ErrorActionPreference = 'Stop'

if ($MinTrades -lt 1) {
    throw "-MinTrades must be >= 1"
}
if ($MaxRows -lt 0) {
    throw "-MaxRows must be >= 0"
}

$BACKFILL_SOURCE = 'cpcv_ptr_ready_backfill_2026_05_12'

function Invoke-Psql {
    param([Parameter(Mandatory=$true)][string]$Sql)
    $out = $Sql | & docker exec -i chili-home-copilot-postgres-1 psql -U chili -d chili -At -v ON_ERROR_STOP=1 2>&1
    if ($LASTEXITCODE -ne 0) {
        $joined = ($out | Out-String).Trim()
        throw "psql failed (exit=$LASTEXITCODE): $joined"
    }
    return $out
}

$limitClause = if ($MaxRows -gt 0) { "LIMIT $MaxRows" } else { "" }

$selector = @"
WITH ptr_counts AS (
    SELECT scan_pattern_id, COUNT(*)::int AS ptr_rows
    FROM trading_pattern_trades
    WHERE outcome_return_pct IS NOT NULL
    GROUP BY scan_pattern_id
),
ptr_ready AS (
    SELECT
        sp.id AS scan_pattern_id,
        sp.user_id,
        COALESCE(sp.lifecycle_stage, 'candidate') AS lifecycle_stage,
        pc.ptr_rows
    FROM scan_patterns sp
    JOIN ptr_counts pc
      ON pc.scan_pattern_id = sp.id
    WHERE sp.cpcv_n_paths IS NULL
      AND COALESCE(sp.lifecycle_stage, 'candidate') NOT IN ('promoted', 'retired', 'decayed')
      AND pc.ptr_rows >= $MinTrades
),
candidate AS (
    SELECT pr.*
    FROM ptr_ready pr
    WHERE NOT EXISTS (
        SELECT 1
        FROM brain_work_events existing
        WHERE existing.dedupe_key = 'cpcv_ptr_ready:' || pr.scan_pattern_id::text
    )
    ORDER BY pr.ptr_rows ASC, pr.scan_pattern_id ASC
    $limitClause
)
"@

Write-Host ""
Write-Host "=== cpcv-ptr-ready-backfill ===" -ForegroundColor Cyan
Write-Host "MinTrades: $MinTrades"
Write-Host ("MaxRows:   {0}" -f $(if ($MaxRows -eq 0) { 'all matching' } else { $MaxRows }))
Write-Host "DryRun:    $DryRun"
Write-Host "Source:    $BACKFILL_SOURCE"
Write-Host ""

$countSql = @"
$selector
SELECT COUNT(*), COALESCE(SUM(ptr_rows), 0), COALESCE(MAX(ptr_rows), 0)
FROM candidate;
"@
$countRow = (Invoke-Psql -Sql $countSql | Select-Object -First 1)
$parts = "$countRow".Split('|')
$n = [int]$parts[0]
$sumPtr = [int64]$parts[1]
$maxPtr = [int]$parts[2]
Write-Host "Candidates: $n pattern(s); total PTR rows=$sumPtr; max PTR rows=$maxPtr"

if ($n -eq 0) {
    Write-Host "Nothing to enqueue." -ForegroundColor Green
    exit 0
}

$previewSql = @"
$selector
SELECT scan_pattern_id, lifecycle_stage, ptr_rows
FROM candidate
ORDER BY ptr_rows ASC, scan_pattern_id ASC
LIMIT 20;
"@
Write-Host ""
Write-Host "Preview (first 20):"
Invoke-Psql -Sql $previewSql | ForEach-Object { Write-Host "  $_" }

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY-RUN: no rows inserted. Re-run with -DryRun:`$false to enqueue." -ForegroundColor Yellow
    exit 0
}

$insertSql = @"
$selector
INSERT INTO brain_work_events (
    domain,
    event_type,
    event_kind,
    payload,
    dedupe_key,
    lease_scope,
    status,
    attempts,
    max_attempts,
    next_run_at,
    correlation_id,
    created_at,
    updated_at,
    processed_at
)
SELECT
    'trading',
    'backtest_completed',
    'outcome',
    jsonb_build_object(
        'scan_pattern_id', scan_pattern_id,
        'user_id', user_id,
        'backtests_run', ptr_rows,
        'ptr_rows', ptr_rows,
        'backfill_source', '$BACKFILL_SOURCE'
    ),
    'cpcv_ptr_ready:' || scan_pattern_id::text,
    'general',
    'pending',
    0,
    5,
    CURRENT_TIMESTAMP,
    '$BACKFILL_SOURCE',
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP,
    NULL
FROM candidate
RETURNING id, payload->>'scan_pattern_id' AS scan_pattern_id, payload->>'ptr_rows' AS ptr_rows;
"@

Write-Host ""
Write-Host "Enqueuing..."
$inserted = Invoke-Psql -Sql $insertSql
$insertedRows = @($inserted | Where-Object { "$_".Trim() -and "$_" -notmatch '^INSERT\\s' })
$insertedRows | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host ("Inserted {0} event(s)." -f ($insertedRows.Count)) -ForegroundColor Green
