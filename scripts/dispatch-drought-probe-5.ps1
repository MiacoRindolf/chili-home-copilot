# Verify the PatternTradeRow gap hypothesis + check who populates it
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-drought-probe-5-out.txt"
"# dispatch-drought-probe-5 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$tmp = [System.IO.Path]::GetTempFileName()
@"
\echo === 5A. PatternTradeRow population per pattern — the CPCV gate consumes THIS, not trade_count ===
SELECT
  COUNT(DISTINCT scan_pattern_id) AS patterns_with_ptr_rows,
  COUNT(*) AS total_ptr_rows,
  ROUND(AVG(per_pat)::numeric, 1) AS avg_ptr_per_pat,
  MAX(per_pat) AS max_ptr_per_pat,
  MIN(per_pat) AS min_ptr_per_pat
FROM (
  SELECT scan_pattern_id, COUNT(*) AS per_pat
    FROM trading_pattern_trade_rows
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) z;

\echo === 5B. Distribution of PTR-row counts per pattern (the CPCV gate's >=30 threshold) ===
SELECT
  CASE
    WHEN per_pat = 0 THEN '0'
    WHEN per_pat < 5 THEN '1-4'
    WHEN per_pat < 15 THEN '5-14'
    WHEN per_pat < 30 THEN '15-29 (below gate)'
    WHEN per_pat < 100 THEN '30-99 (gate OK)'
    ELSE '100+'
  END AS bucket,
  COUNT(*) AS n_patterns
FROM (
  SELECT scan_pattern_id, COUNT(*) AS per_pat
    FROM trading_pattern_trade_rows
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) z
GROUP BY 1 ORDER BY 1;

\echo === 5C. JOIN scan_patterns with PTR counts — top patterns by trade_count but PTR below 30 ===
SELECT sp.id, sp.name, sp.lifecycle_stage, sp.trade_count AS trades_table_count,
       COALESCE(ptr.n, 0) AS ptr_row_count,
       sp.cpcv_n_paths, sp.deflated_sharpe, sp.cpcv_median_sharpe
  FROM scan_patterns sp
  LEFT JOIN (
    SELECT scan_pattern_id, COUNT(*) AS n
      FROM trading_pattern_trade_rows
     WHERE outcome_return_pct IS NOT NULL
     GROUP BY scan_pattern_id
  ) ptr ON ptr.scan_pattern_id = sp.id
 WHERE sp.active = true
   AND sp.trade_count >= 100
   AND (sp.cpcv_n_paths IS NULL OR sp.cpcv_n_paths = 0)
 ORDER BY sp.trade_count DESC
 LIMIT 20;

\echo === 5D. Find who writes to trading_pattern_trade_rows — is it backtest_engine or learning_cycle? ===
SELECT
  DATE_TRUNC('day', as_of_ts) AS day,
  COUNT(*) AS n_rows,
  COUNT(DISTINCT scan_pattern_id) AS n_patterns,
  MIN(as_of_ts) AS first_ts,
  MAX(as_of_ts) AS last_ts
  FROM trading_pattern_trade_rows
 WHERE as_of_ts > NOW() - INTERVAL '14 days'
 GROUP BY 1
 ORDER BY 1 DESC;

\echo === 5E. The 3 promoted patterns: their PTR row counts and CPCV metrics ===
SELECT sp.id, sp.name, sp.lifecycle_stage, sp.trade_count,
       COALESCE(ptr.n, 0) AS ptr_row_count,
       sp.cpcv_n_paths, ROUND(sp.deflated_sharpe::numeric, 3) AS dsr,
       ROUND(sp.pbo::numeric, 3) AS pbo,
       ROUND(sp.cpcv_median_sharpe::numeric, 3) AS med_sharpe
  FROM scan_patterns sp
  LEFT JOIN (
    SELECT scan_pattern_id, COUNT(*) AS n
      FROM trading_pattern_trade_rows
     WHERE outcome_return_pct IS NOT NULL
     GROUP BY scan_pattern_id
  ) ptr ON ptr.scan_pattern_id = sp.id
 WHERE sp.lifecycle_stage IN ('promoted','shadow_promoted','live');

\echo === 5F. Backtest queue — what's the actual stuck pipeline look like? ===
SELECT lifecycle_stage, queue_tier, COUNT(*) AS n,
       MAX(last_backtest_at) AS last_bt_at
  FROM scan_patterns
 WHERE active=true
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo === 5G. EV-passing patterns: which have PTR data vs which don't ===
SELECT
  CASE WHEN COALESCE(ptr.n,0) >= 30 THEN 'has_30+_ptr_rows' ELSE 'below_gate_floor' END AS bucket,
  COUNT(*) AS n_patterns
FROM scan_patterns sp
LEFT JOIN (
  SELECT scan_pattern_id, COUNT(*) AS n
    FROM trading_pattern_trade_rows
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) ptr ON ptr.scan_pattern_id = sp.id
WHERE sp.active = true
  AND sp.trade_count >= 5
  AND sp.win_rate > 0
  AND sp.avg_return_pct > 0
  AND sp.lifecycle_stage NOT IN ('promoted','live','shadow_promoted','retired')
GROUP BY 1;

\echo === 5H. brain_work_events: what fires pattern_stats events? Look at handler emitters ===
SELECT event_type, COUNT(*) AS n, MIN(created_at) AS first_ts, MAX(created_at) AS last_ts
  FROM brain_work_events
 WHERE event_type IN ('pattern_eligible_promotion','pattern_stats_updated','pattern_demoted','pattern_promoted')
 GROUP BY event_type
 ORDER BY n DESC;
"@ | Out-File $tmp -Encoding ascii
& docker cp $tmp chili-home-copilot-postgres-1:/tmp/qdrought5.sql 2>&1 | Out-Null
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qdrought5.sql 2>&1 |
    Out-String | Add-Content $out
Remove-Item $tmp -ErrorAction SilentlyContinue
"" | Add-Content $out

"## brain-worker recent [brain_work:cpcv_gate] activity last 30m" | Add-Content $out
$blog = & docker logs --since 30m chili-home-copilot-brain-worker-1 2>&1
$cpcv = $blog | Select-String -Pattern "brain_work:cpcv_gate|brain_work:promote|brain_work:demote|brain_work:mine|brain_work:pattern_stats" -CaseSensitive:$false
"  match count: $($cpcv.Count) (last 25)" | Add-Content $out
$cpcv | Select-Object -Last 25 | Out-String | Add-Content $out

"## brain-worker handler firing summary - count by handler" | Add-Content $out
$counts = @{}
foreach ($line in $blog) {
    if ($line -match '\[brain_work:([a-z_]+)\]') {
        $name = $matches[1]
        if (-not $counts.ContainsKey($name)) { $counts[$name] = 0 }
        $counts[$name]++
    }
}
foreach ($k in ($counts.Keys | Sort-Object)) {
    "  brain_work:$k : $($counts[$k])" | Add-Content $out
}

"# end" | Add-Content $out
Write-Host "done"
