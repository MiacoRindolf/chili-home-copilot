# audit-cpcv-gate-coverage.ps1 - Phase 0 of f-adaptive-promotion-architecture
#
# Read-only audit. Classifies up to 50 active patterns with PTR>=30 and
# cpcv_n_paths IS NULL by where the CPCV gate funnel breaks:
#
#   event_missing                  - no backtest_completed row references this pid
#   event_pending_or_retry         - row exists, status in (pending,processing,retry_wait)
#   event_dead                     - row exists, status = 'dead' (max retries)
#   event_done_but_no_handler_log  - row done, but no [brain_work:cpcv_gate] log in 24h
#   handler_logged_but_no_persist  - log present yet cpcv_n_paths still NULL (critical)
#   unknown                        - anything else
#
# Hard rules: SELECT-only psql, no DB writes, no app/ edits, no restarts.
# Output committed at scripts/audit-cpcv-gate-coverage-out.txt.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$pg   = "chili-home-copilot-postgres-1"
$bw   = "chili-home-copilot-brain-worker-1"
$out  = "$PSScriptRoot\audit-cpcv-gate-coverage-out.txt"
$csv  = "$PSScriptRoot\_audit_cpcv_rows.csv"

"# audit-cpcv-gate-coverage  start=$((Get-Date).ToUniversalTime().ToString('o'))" | Out-File $out -Encoding utf8

# --- 1. Pull candidate set + most recent backtest_completed event per pid (one psql call) ---
$sqlPath = [System.IO.Path]::GetTempFileName()
$sqlText = @'
COPY (
  WITH ptr_counts AS (
      SELECT scan_pattern_id, COUNT(*) AS ptr_rows
        FROM trading_pattern_trades
       WHERE outcome_return_pct IS NOT NULL
       GROUP BY scan_pattern_id
  ),
  candidates AS (
      SELECT sp.id, sp.name, sp.lifecycle_stage, sp.promotion_status,
             sp.last_backtest_at, sp.oos_evaluated_at, pc.ptr_rows
        FROM scan_patterns sp
        JOIN ptr_counts pc ON pc.scan_pattern_id = sp.id
       WHERE sp.active = TRUE
         AND pc.ptr_rows >= 30
         AND sp.cpcv_n_paths IS NULL
         AND COALESCE(sp.lifecycle_stage, '') NOT IN ('promoted', 'retired')
       ORDER BY pc.ptr_rows DESC
       LIMIT 50
  )
  SELECT c.id              AS pid,
         c.name            AS name,
         c.lifecycle_stage AS lifecycle_stage,
         c.promotion_status AS promotion_status,
         c.ptr_rows        AS ptr_rows,
         c.last_backtest_at AS last_backtest_at,
         c.oos_evaluated_at AS oos_evaluated_at,
         bwe.id            AS event_id,
         bwe.status        AS event_status,
         bwe.attempts      AS event_attempts,
         bwe.created_at    AS event_created_at,
         bwe.processed_at  AS event_processed_at,
         bwe.last_error    AS event_last_error
    FROM candidates c
    LEFT JOIN LATERAL (
        SELECT id, status, attempts, created_at, processed_at, last_error
          FROM brain_work_events
         WHERE event_type = 'backtest_completed'
           AND (payload->>'scan_pattern_id')::int = c.id
         ORDER BY created_at DESC
         LIMIT 1
    ) bwe ON TRUE
    ORDER BY c.ptr_rows DESC
) TO '/tmp/audit_cpcv.csv' WITH (FORMAT csv, HEADER true);
'@
$sqlText | Out-File $sqlPath -Encoding ascii

& docker cp $sqlPath "${pg}:/tmp/audit_cpcv.sql" 2>&1 | Out-Null
$psqlMsg = & docker exec $pg psql -U chili -d chili -v ON_ERROR_STOP=1 -f /tmp/audit_cpcv.sql 2>&1
"## psql exec" | Add-Content $out
($psqlMsg | Out-String).TrimEnd() | Add-Content $out
& docker cp "${pg}:/tmp/audit_cpcv.csv" $csv 2>&1 | Out-Null
Remove-Item $sqlPath -ErrorAction SilentlyContinue

if (-not (Test-Path $csv)) {
    "ABORT: CSV not produced - check psql output above" | Add-Content $out
    Write-Host "audit aborted: CSV missing" -ForegroundColor Red
    exit 2
}

$rows = Import-Csv $csv
"" | Add-Content $out
"## Candidates examined: $($rows.Count) (cap=50)" | Add-Content $out

# --- 2. One docker logs call for the 24h window, all in memory ---
"## Pulling brain-worker logs (last 24h) for handler-log scan" | Add-Content $out
$logsRaw = & docker logs --since 24h $bw 2>&1
$logsString = ($logsRaw | Out-String)
$logsLines = $logsString -split "`r?`n"
$cpcvLines = $logsLines | Where-Object { $_ -match '\[brain_work:cpcv_gate\]' }
"   brain-worker log line count (24h):   $($logsLines.Count)" | Add-Content $out
"   [brain_work:cpcv_gate] lines (24h):  $($cpcvLines.Count)" | Add-Content $out

# Try to estimate log window span (first and last line timestamps if present).
$firstTs = ($logsLines | Where-Object { $_ -match '^\S+T\S+' } | Select-Object -First 1)
$lastTs  = ($logsLines | Where-Object { $_ -match '^\S+T\S+' } | Select-Object -Last 1)
"   first log line (head):  $firstTs" | Add-Content $out
"   last  log line (tail):  $lastTs"  | Add-Content $out

# --- 3. Classify each row ---
$classified = @()
foreach ($r in $rows) {
    $pid_  = [int]$r.pid
    $estat = ($r.event_status + "").Trim().ToLower()
    $eid   = ($r.event_id + "").Trim()

    $hasEvent      = -not [string]::IsNullOrWhiteSpace($eid)
    $eventDone     = $hasEvent -and ($estat -eq 'done')
    $eventPending  = $hasEvent -and ($estat -in @('pending','processing','retry_wait'))
    $eventDead     = $hasEvent -and ($estat -eq 'dead')

    # Handler log evidence - pattern_id token OR ev_id token.
    $patternToken = "pattern_id=$pid_"
    $evToken      = if ($hasEvent) { "ev_id=$eid" } else { $null }
    $matchedLogs  = $cpcvLines | Where-Object {
        ($_ -match [regex]::Escape($patternToken)) -or
        ($evToken -and ($_ -match [regex]::Escape($evToken)))
    }
    $handlerLogged = ($matchedLogs.Count -gt 0)

    $cls = "unknown"
    if (-not $hasEvent) {
        $cls = "event_missing"
    } elseif ($eventPending) {
        $cls = "event_pending_or_retry"
    } elseif ($eventDead) {
        $cls = "event_dead"
    } elseif ($eventDone -and (-not $handlerLogged)) {
        $cls = "event_done_but_no_handler_log"
    } elseif ($eventDone -and $handlerLogged) {
        # Handler logged something for this pattern/event but persist is still NULL.
        # This is the critical bucket - the persist path silently dropped the verdict.
        $cls = "handler_logged_but_no_persist"
    }

    $classified += [PSCustomObject]@{
        pid                = $pid_
        name               = $r.name
        ptr_rows           = [int]$r.ptr_rows
        lifecycle_stage    = $r.lifecycle_stage
        promotion_status   = $r.promotion_status
        last_backtest_at   = $r.last_backtest_at
        event_id           = $eid
        event_status       = $estat
        event_attempts     = $r.event_attempts
        event_created_at   = $r.event_created_at
        event_processed_at = $r.event_processed_at
        event_last_error   = $r.event_last_error
        handler_log_hits   = $matchedLogs.Count
        handler_log_sample = if ($matchedLogs.Count -gt 0) { ($matchedLogs | Select-Object -First 1) } else { "" }
        classification     = $cls
    }
}

# --- 4. Summary table ---
$total = $classified.Count
$buckets = @(
    'event_missing',
    'event_pending_or_retry',
    'event_dead',
    'event_done_but_no_handler_log',
    'handler_logged_but_no_persist',
    'unknown'
)

"" | Add-Content $out
"## SUMMARY ($total of 275 candidate patterns audited; cap=50)" | Add-Content $out
"" | Add-Content $out
"| classification                  | count | pct    |" | Add-Content $out
"|---------------------------------|-------|--------|" | Add-Content $out
foreach ($b in $buckets) {
    $c = ($classified | Where-Object { $_.classification -eq $b }).Count
    $pct = if ($total -gt 0) { [math]::Round(100.0 * $c / $total, 1) } else { 0.0 }
    "| {0,-31} | {1,5} | {2,5}% |" -f $b, $c, $pct | Add-Content $out
}
"| {0,-31} | {1,5} | {2,5}% |" -f "TOTAL", $total, 100.0 | Add-Content $out

# --- 5. Top 10 examples per non-zero bucket ---
"" | Add-Content $out
"## TOP 10 EXAMPLES per non-empty bucket" | Add-Content $out
foreach ($b in $buckets) {
    $rowsB = @($classified | Where-Object { $_.classification -eq $b })
    if ($rowsB.Count -eq 0) { continue }
    "" | Add-Content $out
    "### [$b] count=$($rowsB.Count)" | Add-Content $out
    $rowsB | Select-Object -First 10 pid, ptr_rows, lifecycle_stage, event_id, event_status, event_attempts, event_created_at, event_processed_at |
        Format-Table -AutoSize | Out-String | Add-Content $out
}

# --- 6. Full classified roster ---
"" | Add-Content $out
"## ALL $total CLASSIFIED ROWS" | Add-Content $out
$classified |
    Select-Object pid, ptr_rows, classification, event_id, event_status, event_attempts, lifecycle_stage, event_last_error |
    Format-Table -AutoSize -Wrap | Out-String | Add-Content $out

# --- 7. End marker ---
"" | Add-Content $out
"# end  finish=$((Get-Date).ToUniversalTime().ToString('o'))" | Add-Content $out
Write-Host "audit complete: $out"
