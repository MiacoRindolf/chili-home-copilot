# Phase 1c — controlled backfill of historical outcome/done orphans in
# brain_work_events. See docs/runbooks/BRAIN_EVENT_BACKFILL.md for full
# operator procedure. Re-runs are idempotent: rows already carrying the
# phase_1c_backfill_2026_05_11 marker are skipped.
#
# Hard contract (do not relax without a new Cowork brief):
#   - -EventType is REQUIRED; whitelist-validated against the 7 known
#     Phase 1a orphan types.
#   - -DryRun defaults to $true. Live runs require explicit -DryRun:$false.
#   - Inter-batch sleep is hardcoded to 30s (dispatcher headroom).
#   - market_snapshots_batch is GATED on mine_patterns inner-contract
#     verification (see runbook). Script warns + pauses 5s before any
#     run targeting that event type.
#   - psql is invoked via `docker compose exec -T postgres` to match the
#     codebase convention and avoid host-psql / PGPASSWORD dependencies.
#   - The -EventType string is whitelist-validated before any SQL is
#     built; row IDs are produced by our own SELECT and never come from
#     user input. No operator string is ever interpolated into SQL.

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$EventType,

    [int]$BatchSize = 8,

    [int]$MaxRows = 0,

    [bool]$DryRun = $true
)

$ErrorActionPreference = 'Stop'

$INTER_BATCH_SLEEP_SECONDS = 30
$BACKFILL_MARKER = 'phase_1c_backfill_2026_05_11'
$STOP_FLAG = Join-Path $PSScriptRoot 'brain-event-backfill-stop.flag'
$PROGRESS_LOG = Join-Path $PSScriptRoot 'brain-event-backfill-progress.log'

$KNOWN_EVENT_TYPES = @(
    'backtest_completed',
    'market_snapshots_batch',
    'broker_fill_closed',
    'live_trade_closed',
    'paper_trade_closed',
    'pattern_eligible_promotion'
)

if ($KNOWN_EVENT_TYPES -notcontains $EventType) {
    Write-Host "ERROR: unknown -EventType '$EventType'." -ForegroundColor Red
    Write-Host "Allowed: $($KNOWN_EVENT_TYPES -join ', ')" -ForegroundColor Red
    exit 2
}

if ($BatchSize -lt 1) {
    Write-Host "ERROR: -BatchSize must be >= 1 (got $BatchSize)" -ForegroundColor Red
    exit 2
}

if ($MaxRows -lt 0) {
    Write-Host "ERROR: -MaxRows must be >= 0 (got $MaxRows)" -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "=== brain-event-backfill (Phase 1c) ===" -ForegroundColor Cyan
Write-Host "EventType:   $EventType"
Write-Host "BatchSize:   $BatchSize"
Write-Host ("MaxRows:     {0}" -f $(if ($MaxRows -eq 0) { 'all matching' } else { $MaxRows }))
Write-Host "DryRun:      $DryRun"
Write-Host "Marker:      $BACKFILL_MARKER"
Write-Host "StopFlag:    $STOP_FLAG"
Write-Host "ProgressLog: $PROGRESS_LOG"
Write-Host ""
Write-Host "Prereq reminder (operator verifies, script does NOT):" -ForegroundColor Yellow
Write-Host "  1. chili_brain_outcome_claimable_enabled=True in production"
Write-Host "  2. 24h of stable handler activity in prod (no retry pile-up)"
Write-Host "  3. See docs/runbooks/BRAIN_WORK_EVENT_KIND.md for the flag flip"
Write-Host ""

if ($EventType -eq 'market_snapshots_batch') {
    Write-Host "WARNING: -EventType=market_snapshots_batch triggers mine_patterns replay." -ForegroundColor Yellow
    Write-Host "mine_patterns has NO event-level dedupe (Phase 1b runbook flagged this)." -ForegroundColor Yellow
    Write-Host "DO NOT proceed until the inner contract is verified per" -ForegroundColor Yellow
    Write-Host "docs/runbooks/BRAIN_EVENT_BACKFILL.md, section 'GATED event types'." -ForegroundColor Yellow
    Write-Host "Press Ctrl+C now if you have not verified. Continuing in 5s..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

function Invoke-Psql {
    param([Parameter(Mandatory=$true)][string]$Sql)
    # Pipe SQL via stdin to avoid -c argument-quoting issues. Uses
    # ON_ERROR_STOP so a bad statement returns non-zero from psql.
    $output = $Sql | & docker compose exec -T postgres psql -U chili -d chili -At -v ON_ERROR_STOP=1 2>&1
    if ($LASTEXITCODE -ne 0) {
        $joined = ($output | Out-String).Trim()
        throw "psql failed (exit=$LASTEXITCODE): $joined"
    }
    return $output
}

function Get-CandidateIds {
    param([int]$Limit)
    $limitClause = if ($Limit -gt 0) { "LIMIT $Limit" } else { "" }
    # $EventType is whitelist-validated above. No operator string ever
    # reaches SQL outside of that whitelist match.
    $sql = @"
SELECT id FROM brain_work_events bwe1
WHERE domain = 'trading'
  AND event_kind = 'outcome'
  AND event_type = '$EventType'
  AND status = 'done'
  AND processed_at IS NOT NULL
  AND COALESCE(payload->>'backfill_source','') NOT LIKE '$BACKFILL_MARKER%'
  AND NOT EXISTS (
      SELECT 1 FROM brain_work_events bwe2
      WHERE bwe2.dedupe_key = bwe1.dedupe_key
        AND bwe2.id <> bwe1.id
        AND bwe2.status IN ('pending', 'processing', 'retry_wait')
  )
ORDER BY created_at ASC
$limitClause;
"@
    $rows = Invoke-Psql -Sql $sql
    $ids = @()
    foreach ($line in $rows) {
        $trimmed = "$line".Trim()
        if ($trimmed -match '^\d+$') {
            $ids += [int64]$trimmed
        }
    }
    return ,$ids
}

function Invoke-BatchUpdate {
    param([int64[]]$Ids)
    if ($Ids.Count -eq 0) { return 0 }
    $arrayLiteral = '{' + ($Ids -join ',') + '}'
    $sql = @"
UPDATE brain_work_events
SET status = 'pending',
    processed_at = NULL,
    attempts = 0,
    lease_holder = NULL,
    lease_expires_at = NULL,
    next_run_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP,
    payload = jsonb_set(COALESCE(payload, '{}'::jsonb),
                        '{backfill_source}',
                        '"$BACKFILL_MARKER"'::jsonb, true)
WHERE id = ANY('$arrayLiteral'::bigint[]);
"@
    $null = Invoke-Psql -Sql $sql
    return $Ids.Count
}

# --- Candidate enumeration ----------------------------------------------
$effectiveLimit = $MaxRows
Write-Host "[1/3] Selecting candidate row IDs..."
$candidateIds = Get-CandidateIds -Limit $effectiveLimit
$total = $candidateIds.Count
Write-Host "  -> $total candidate row(s) match selector."

if ($total -eq 0) {
    Write-Host "Nothing to do. Exiting." -ForegroundColor Green
    exit 0
}

$batchCount = [Math]::Ceiling($total / [double]$BatchSize)
$estSeconds = [int](($batchCount - 1) * $INTER_BATCH_SLEEP_SECONDS)
$estMinutes = [Math]::Round($estSeconds / 60.0, 1)

Write-Host ""
Write-Host "Plan:" -ForegroundColor Cyan
Write-Host "  $total row(s) in $batchCount batch(es) of up to $BatchSize"
Write-Host "  Inter-batch sleep: ${INTER_BATCH_SLEEP_SECONDS}s (hardcoded)"
Write-Host "  Estimated wall-clock (sleeps only): ${estSeconds}s (~$estMinutes min)"

$previewN = [Math]::Min(20, $total)
$preview = ($candidateIds | Select-Object -First $previewN) -join ', '
Write-Host "  First $previewN IDs: $preview"

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY-RUN: no changes made. Re-run with -DryRun:`$false to apply." -ForegroundColor Yellow
    exit 0
}

# --- Live mode ----------------------------------------------------------
if (Test-Path $STOP_FLAG) {
    Write-Host "ERROR: stop flag present at $STOP_FLAG. Remove it before live run." -ForegroundColor Red
    exit 3
}

$runStart = Get-Date
$startLine = "{0:o}`tSTART`tevent_type={1}`ttotal={2}`tbatch_size={3}`tmarker={4}" -f $runStart, $EventType, $total, $BatchSize, $BACKFILL_MARKER
Add-Content -Path $PROGRESS_LOG -Value $startLine -Encoding utf8
Write-Host ""
Write-Host "[2/3] Live backfill starting..."

$cumulative = 0
$batchIdx = 0
for ($offset = 0; $offset -lt $total; $offset += $BatchSize) {
    $batchIdx += 1

    if (Test-Path $STOP_FLAG) {
        $haltLine = "{0:o}`tHALTED`tevent_type={1}`tcumulative={2}`treason=stop_flag" -f (Get-Date), $EventType, $cumulative
        Add-Content -Path $PROGRESS_LOG -Value $haltLine -Encoding utf8
        Write-Host "HALTED by kill switch ($STOP_FLAG). Cumulative=$cumulative." -ForegroundColor Yellow
        exit 0
    }

    $end = [Math]::Min($offset + $BatchSize, $total) - 1
    $batchIds = $candidateIds[$offset..$end]
    $applied = Invoke-BatchUpdate -Ids $batchIds
    $cumulative += $applied

    $line = "{0:o}`tBATCH`tevent_type={1}`tbatch={2}/{3}`tapplied={4}`tcumulative={5}/{6}`tfirst_id={7}`tlast_id={8}" -f `
        (Get-Date), $EventType, $batchIdx, $batchCount, $applied, $cumulative, $total, $batchIds[0], $batchIds[-1]
    Add-Content -Path $PROGRESS_LOG -Value $line -Encoding utf8
    Write-Host ("  batch {0}/{1}: applied={2} cumulative={3}/{4}" -f $batchIdx, $batchCount, $applied, $cumulative, $total)

    if ($offset + $BatchSize -lt $total) {
        Start-Sleep -Seconds $INTER_BATCH_SLEEP_SECONDS
    }
}

$elapsed = (Get-Date) - $runStart
$doneLine = "{0:o}`tDONE`tevent_type={1}`tcumulative={2}`telapsed_sec={3:N1}" -f (Get-Date), $EventType, $cumulative, $elapsed.TotalSeconds
Add-Content -Path $PROGRESS_LOG -Value $doneLine -Encoding utf8

Write-Host ""
Write-Host "[3/3] Done. Backfilled $cumulative row(s) for event_type=$EventType in $([Math]::Round($elapsed.TotalSeconds,1))s." -ForegroundColor Green
Write-Host "Progress log: $PROGRESS_LOG"
exit 0
