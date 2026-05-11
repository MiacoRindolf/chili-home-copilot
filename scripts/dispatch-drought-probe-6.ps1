# Verify the PatternTradeRow gap hypothesis (correct table name: trading_pattern_trades)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-drought-probe-6-out.txt"
"# dispatch-drought-probe-6 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$tmp = [System.IO.Path]::GetTempFileName()
@"
\echo === 6A. PatternTradeRow population per pattern - the CPCV gate consumes THIS ===
SELECT
  COUNT(DISTINCT scan_pattern_id) AS patterns_with_ptr_rows,
  COUNT(*) AS total_ptr_rows,
  ROUND(AVG(per_pat)::numeric, 1) AS avg_ptr_per_pat,
  MAX(per_pat) AS max_ptr_per_pat,
  MIN(per_pat) AS min_ptr_per_pat
FROM (
  SELECT scan_pattern_id, COUNT(*) AS per_pat
    FROM trading_pattern_trades
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) z;

\echo === 6B. Distribution of PTR-row counts per pattern (CPCV gate min=30) ===
SELECT
  CASE
    WHEN per_pat = 0 THEN 'a_zero'
    WHEN per_pat < 5 THEN 'b_1_to_4'
    WHEN per_pat < 15 THEN 'c_5_to_14'
    WHEN per_pat < 30 THEN 'd_15_to_29_below_gate'
    WHEN per_pat < 100 THEN 'e_30_to_99_gate_ok'
    ELSE 'f_100plus'
  END AS bucket,
  COUNT(*) AS n_patterns
FROM (
  SELECT scan_pattern_id, COUNT(*) AS per_pat
    FROM trading_pattern_trades
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) z
GROUP BY 1 ORDER BY 1;

\echo === 6C. patterns with high trade_count but no PTR rows / few PTR rows ===
SELECT sp.id, sp.name, sp.lifecycle_stage, sp.trade_count AS trades_table,
       COALESCE(ptr.n, 0) AS ptr_rows,
       sp.cpcv_n_paths,
       ROUND(sp.deflated_sharpe::numeric, 3) AS dsr,
       ROUND(sp.pbo::numeric, 3) AS pbo
  FROM scan_patterns sp
  LEFT JOIN (
    SELECT scan_pattern_id, COUNT(*) AS n
      FROM trading_pattern_trades
     WHERE outcome_return_pct IS NOT NULL
     GROUP BY scan_pattern_id
  ) ptr ON ptr.scan_pattern_id = sp.id
 WHERE sp.active = true
   AND sp.trade_count >= 100
 ORDER BY sp.trade_count DESC
 LIMIT 20;

\echo === 6D. trading_pattern_trades recent write activity (last 14d) ===
SELECT DATE_TRUNC('day', as_of_ts) AS day,
       COUNT(*) AS n_rows,
       COUNT(DISTINCT scan_pattern_id) AS n_patterns
  FROM trading_pattern_trades
 WHERE as_of_ts > NOW() - INTERVAL '14 days'
 GROUP BY 1 ORDER BY 1 DESC;

\echo === 6E. The 3 promoted patterns: their PTR row counts + CPCV ===
SELECT sp.id, sp.name, sp.lifecycle_stage,
       sp.trade_count, COALESCE(ptr.n, 0) AS ptr_rows,
       sp.cpcv_n_paths,
       ROUND(sp.deflated_sharpe::numeric, 3) AS dsr,
       ROUND(sp.pbo::numeric, 3) AS pbo,
       ROUND(sp.cpcv_median_sharpe::numeric, 3) AS med_sharpe
  FROM scan_patterns sp
  LEFT JOIN (
    SELECT scan_pattern_id, COUNT(*) AS n
      FROM trading_pattern_trades
     WHERE outcome_return_pct IS NOT NULL
     GROUP BY scan_pattern_id
  ) ptr ON ptr.scan_pattern_id = sp.id
 WHERE sp.lifecycle_stage IN ('promoted','shadow_promoted','live');

\echo === 6F. EV-passing patterns: how many cross the PTR>=30 threshold? ===
SELECT
  CASE WHEN COALESCE(ptr.n,0) >= 30 THEN 'a_has_30plus_ptr' ELSE 'b_below_gate_floor' END AS bucket,
  COUNT(*) AS n
FROM scan_patterns sp
LEFT JOIN (
  SELECT scan_pattern_id, COUNT(*) AS n
    FROM trading_pattern_trades
   WHERE outcome_return_pct IS NOT NULL
   GROUP BY scan_pattern_id
) ptr ON ptr.scan_pattern_id = sp.id
WHERE sp.active = true
  AND sp.trade_count >= 5
  AND sp.win_rate > 0
  AND sp.avg_return_pct > 0
  AND sp.lifecycle_stage NOT IN ('promoted','live','shadow_promoted','retired')
GROUP BY 1 ORDER BY 1;

\echo === 6G. Empirical CPCV metric distribution (the 19 patterns that DO have data) ===
SELECT
  'cpcv_n_paths' AS metric,
  ROUND(MIN(cpcv_n_paths)::numeric, 2) AS minv,
  ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY cpcv_n_paths)::numeric, 2) AS p25,
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY cpcv_n_paths)::numeric, 2) AS p50,
  ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY cpcv_n_paths)::numeric, 2) AS p75,
  ROUND(MAX(cpcv_n_paths)::numeric, 2) AS maxv,
  COUNT(*) AS n_with_value
  FROM scan_patterns WHERE active=true AND cpcv_n_paths IS NOT NULL
UNION ALL
SELECT 'deflated_sharpe',
  ROUND(MIN(deflated_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY deflated_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY deflated_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY deflated_sharpe)::numeric, 3),
  ROUND(MAX(deflated_sharpe)::numeric, 3),
  COUNT(*)
  FROM scan_patterns WHERE active=true AND deflated_sharpe IS NOT NULL
UNION ALL
SELECT 'pbo',
  ROUND(MIN(pbo)::numeric, 3),
  ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY pbo)::numeric, 3),
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY pbo)::numeric, 3),
  ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY pbo)::numeric, 3),
  ROUND(MAX(pbo)::numeric, 3),
  COUNT(*)
  FROM scan_patterns WHERE active=true AND pbo IS NOT NULL
UNION ALL
SELECT 'cpcv_median_sharpe',
  ROUND(MIN(cpcv_median_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 3),
  ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 3),
  ROUND(MAX(cpcv_median_sharpe)::numeric, 3),
  COUNT(*)
  FROM scan_patterns WHERE active=true AND cpcv_median_sharpe IS NOT NULL;
"@ | Out-File $tmp -Encoding ascii
& docker cp $tmp chili-home-copilot-postgres-1:/tmp/qdrought6.sql 2>&1 | Out-Null
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qdrought6.sql 2>&1 |
    Out-String | Add-Content $out
Remove-Item $tmp -ErrorAction SilentlyContinue
"" | Add-Content $out

"# end" | Add-Content $out
Write-Host "done"
