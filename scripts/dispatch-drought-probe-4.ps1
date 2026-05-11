# Find where CPCV metrics actually live, plus understand backtest pipeline gap
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-drought-probe-4-out.txt"
"# dispatch-drought-probe-4 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$tmp = [System.IO.Path]::GetTempFileName()
@"
\echo === 4A. All CPCV/DSR/PBO columns ANYWHERE in DB ===
SELECT table_name, column_name, data_type FROM information_schema.columns
 WHERE table_schema='public'
   AND (column_name LIKE '%cpcv%' OR column_name LIKE '%dsr%' OR column_name LIKE '%pbo%' OR column_name LIKE '%sharpe%')
 ORDER BY table_name, column_name;

\echo === 4B. all columns on scan_patterns that look promotion-related ===
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_name='scan_patterns'
   AND (column_name LIKE '%oos%' OR column_name LIKE '%promot%' OR column_name LIKE '%eval%' OR column_name LIKE '%backtest%' OR column_name LIKE '%paths%' OR column_name LIKE '%n_trades%')
 ORDER BY column_name;

\echo === 4C. promotion-gate tables ===
SELECT table_name FROM information_schema.tables
 WHERE table_schema='public'
   AND (table_name LIKE '%promotion%' OR table_name LIKE '%cpcv%' OR table_name LIKE '%gate%' OR table_name LIKE '%cohort%')
 ORDER BY table_name;

\echo === 4D. sample row from any cpcv result table (look at top 2 patterns) ===
SELECT table_name FROM information_schema.tables
 WHERE table_schema='public' AND table_name LIKE '%cpcv%' OR table_name LIKE '%pattern_quality%';

\echo === 4E. brain_work_events: types fired in last 24h ===
SELECT event_type, COUNT(*) AS n, MIN(created_at) AS first_ts, MAX(created_at) AS last_ts
  FROM brain_work_events
 WHERE created_at > NOW() - INTERVAL '24 hours'
 GROUP BY event_type
 ORDER BY n DESC
 LIMIT 30;

\echo === 4F. brain_work_events: lifecycle of one promoted pattern (585) - has it received any events ever? ===
SELECT event_type, COUNT(*) AS n, MAX(created_at) AS last_ts
  FROM brain_work_events
 WHERE payload::text LIKE '%pattern_id%585%' OR payload::text LIKE '%"id": 585%'
 GROUP BY event_type
 ORDER BY n DESC
 LIMIT 20;

\echo === 4G. pattern 731 (10341 trades, never OOS-evaluated): why? ===
SELECT id, name, lifecycle_stage, trade_count, win_rate, avg_return_pct,
       oos_evaluated_at, created_at, updated_at,
       active
  FROM scan_patterns
 WHERE id IN (731, 732, 733, 749, 763, 1054);

\echo === 4H. Phase 2 handlers - check what handlers are loaded (from brain_worker_control) ===
SELECT * FROM brain_worker_control LIMIT 5;

\echo === 4I. CHECK: do ANY scan_patterns have ANY CPCV-related field populated? ===
SELECT column_name FROM information_schema.columns
 WHERE table_name='scan_patterns'
 ORDER BY column_name;
"@ | Out-File $tmp -Encoding ascii
& docker cp $tmp chili-home-copilot-postgres-1:/tmp/qdrought4.sql 2>&1 | Out-Null
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qdrought4.sql 2>&1 |
    Out-String | Add-Content $out
Remove-Item $tmp -ErrorAction SilentlyContinue
"" | Add-Content $out

"## handler activity in brain-worker last 30m (mine, cpcv_gate, promote, demote, pattern_stats, regime_ledger)" | Add-Content $out
$blog = & docker logs --since 30m chili-home-copilot-brain-worker-1 2>&1
$handlers = $blog | Select-String -Pattern "\[handler_(mine|cpcv_gate|promote|demote|pattern_stats|regime_ledger)\]" -CaseSensitive:$false
"  match count: $($handlers.Count) (last 30)" | Add-Content $out
$handlers | Select-Object -Last 30 | Out-String | Add-Content $out

"## handler firing summary - count by handler name" | Add-Content $out
$counts = @{}
foreach ($line in $blog) {
    if ($line -match '\[handler_([a-z_]+)\]') {
        $name = $matches[1]
        if (-not $counts.ContainsKey($name)) { $counts[$name] = 0 }
        $counts[$name]++
    }
}
foreach ($k in ($counts.Keys | Sort-Object)) {
    "  $k : $($counts[$k])" | Add-Content $out
}

"# end" | Add-Content $out
Write-Host "done"
