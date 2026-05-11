# Deeper probe for the three architect questions:
# 1. Empirical distribution of pattern metrics (for adaptive CPCV redesign)
# 2. Composite quality score: what TRIGGERS its computation today?
# 3. Backtest pipeline + UI staleness (mined_at, oos_evaluated_at, backtest activity)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-drought-probe-3-out.txt"
"# dispatch-drought-probe-3 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$tmp = [System.IO.Path]::GetTempFileName()
@"
\echo === 1A. Empirical distribution of CPCV metrics across patterns (for adaptive thresholds) ===
SELECT
  COUNT(*) AS n_total,
  COUNT(cpcv_dsr) AS n_with_dsr,
  COUNT(cpcv_pbo) AS n_with_pbo,
  COUNT(cpcv_median_sharpe) AS n_with_sharpe,
  COUNT(cpcv_n_paths) AS n_with_paths,
  ROUND(AVG(cpcv_dsr)::numeric, 4) AS avg_dsr,
  ROUND(AVG(cpcv_pbo)::numeric, 4) AS avg_pbo,
  ROUND(AVG(cpcv_median_sharpe)::numeric, 4) AS avg_sharpe,
  ROUND(AVG(cpcv_n_paths)::numeric, 2) AS avg_n_paths
  FROM scan_patterns
 WHERE active = true;

\echo === 1B. Percentile distribution of each CPCV metric (target the actual population, not hardcoded) ===
SELECT
  'cpcv_dsr' AS metric,
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY cpcv_dsr)::numeric, 4) AS p50,
  ROUND(percentile_cont(0.60) WITHIN GROUP (ORDER BY cpcv_dsr)::numeric, 4) AS p60,
  ROUND(percentile_cont(0.70) WITHIN GROUP (ORDER BY cpcv_dsr)::numeric, 4) AS p70,
  ROUND(percentile_cont(0.80) WITHIN GROUP (ORDER BY cpcv_dsr)::numeric, 4) AS p80
  FROM scan_patterns WHERE active=true AND cpcv_dsr IS NOT NULL
UNION ALL SELECT 'cpcv_pbo',
  ROUND(percentile_cont(0.20) WITHIN GROUP (ORDER BY cpcv_pbo)::numeric, 4),
  ROUND(percentile_cont(0.30) WITHIN GROUP (ORDER BY cpcv_pbo)::numeric, 4),
  ROUND(percentile_cont(0.40) WITHIN GROUP (ORDER BY cpcv_pbo)::numeric, 4),
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY cpcv_pbo)::numeric, 4)
  FROM scan_patterns WHERE active=true AND cpcv_pbo IS NOT NULL
UNION ALL SELECT 'cpcv_median_sharpe',
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 4),
  ROUND(percentile_cont(0.60) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 4),
  ROUND(percentile_cont(0.70) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 4),
  ROUND(percentile_cont(0.80) WITHIN GROUP (ORDER BY cpcv_median_sharpe)::numeric, 4)
  FROM scan_patterns WHERE active=true AND cpcv_median_sharpe IS NOT NULL
UNION ALL SELECT 'trade_count',
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY trade_count)::numeric, 0),
  ROUND(percentile_cont(0.60) WITHIN GROUP (ORDER BY trade_count)::numeric, 0),
  ROUND(percentile_cont(0.70) WITHIN GROUP (ORDER BY trade_count)::numeric, 0),
  ROUND(percentile_cont(0.80) WITHIN GROUP (ORDER BY trade_count)::numeric, 0)
  FROM scan_patterns WHERE active=true AND trade_count IS NOT NULL;

\echo === 1C. The 24 EV-passing-but-not-promoted patterns: WHY did CPCV reject them? ===
SELECT id, name, lifecycle_stage,
       trade_count, ROUND(win_rate::numeric,3) AS wr, ROUND(avg_return_pct::numeric,3) AS avg_ret,
       cpcv_dsr, cpcv_pbo, cpcv_median_sharpe, cpcv_n_paths,
       CASE
         WHEN cpcv_dsr IS NULL THEN 'no_cpcv'
         WHEN cpcv_dsr < 0.95 THEN 'dsr_<0.95'
         WHEN cpcv_pbo > 0.2 THEN 'pbo_>0.2'
         WHEN cpcv_median_sharpe < 0.5 THEN 'sharpe_<0.5'
         WHEN cpcv_n_paths < 20 THEN 'paths_<20'
         WHEN trade_count < 15 THEN 'trades_<15'
         ELSE 'unknown'
       END AS cpcv_block_reason
  FROM scan_patterns
 WHERE active=true
   AND trade_count >= 5
   AND win_rate > 0
   AND avg_return_pct > 0
   AND lifecycle_stage NOT IN ('promoted','live','shadow_promoted')
 ORDER BY avg_return_pct DESC NULLS LAST
 LIMIT 25;

\echo === 2A. composite quality score: schema + what columns exist? ===
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_name='scan_patterns' AND (column_name LIKE '%quality%' OR column_name LIKE '%composite%' OR column_name LIKE '%score%')
 ORDER BY column_name;

\echo === 2B. The 2 patterns WITH a composite score: who are they + when computed? ===
SELECT id, name, lifecycle_stage, quality_composite_score, trade_count, win_rate, avg_return_pct
  FROM scan_patterns
 WHERE quality_composite_score IS NOT NULL
 ORDER BY quality_composite_score DESC;

\echo === 3A. Backtest pipeline: distribution of last-evaluation timestamps ===
SELECT
  CASE
    WHEN oos_evaluated_at IS NULL THEN 'never'
    WHEN oos_evaluated_at > NOW() - INTERVAL '1 day' THEN 'last_1d'
    WHEN oos_evaluated_at > NOW() - INTERVAL '7 days' THEN 'last_7d'
    WHEN oos_evaluated_at > NOW() - INTERVAL '30 days' THEN 'last_30d'
    ELSE 'older'
  END AS bucket,
  COUNT(*) AS n
  FROM scan_patterns
 WHERE active=true
 GROUP BY 1
 ORDER BY 1;

\echo === 3B. mined_at distribution (when were active patterns last mined?) ===
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_name='scan_patterns'
   AND (column_name LIKE '%mined%' OR column_name LIKE '%discovered%' OR column_name LIKE '%created%' OR column_name LIKE '%updated%')
 ORDER BY column_name;

\echo === 3C. Recent (last 7d) scan_patterns updated_at distribution ===
SELECT DATE_TRUNC('day', updated_at) AS day, COUNT(*) AS n
  FROM scan_patterns
 WHERE active=true
   AND updated_at > NOW() - INTERVAL '14 days'
 GROUP BY 1
 ORDER BY 1 DESC;

\echo === 3D. trading_backtest_param_sets recent volume (last 24h) ===
SELECT DATE_TRUNC('hour', created_at) AS hour, COUNT(*) AS n
  FROM trading_backtest_param_sets
 WHERE created_at > NOW() - INTERVAL '24 hours'
 GROUP BY 1
 ORDER BY 1 DESC
 LIMIT 25;

\echo === 3E. UI runtime-tab anomaly: patterns with trades but no recent backtest ===
SELECT id, name, lifecycle_stage, trade_count, oos_evaluated_at,
       ROUND(EXTRACT(epoch FROM (NOW() - oos_evaluated_at))/86400.0::numeric, 1) AS days_since_oos_eval
  FROM scan_patterns
 WHERE active=true
   AND trade_count > 0
   AND (oos_evaluated_at IS NULL OR oos_evaluated_at < NOW() - INTERVAL '7 days')
 ORDER BY trade_count DESC NULLS LAST
 LIMIT 15;

\echo === 3F. promotion-pipeline event handlers — are they actually firing? ===
SELECT table_name FROM information_schema.tables
 WHERE table_schema='public'
   AND (table_name LIKE '%brain_work%' OR table_name LIKE '%event_ledger%' OR table_name LIKE '%handler_log%')
 ORDER BY table_name;
"@ | Out-File $tmp -Encoding ascii
& docker cp $tmp chili-home-copilot-postgres-1:/tmp/qdrought3.sql 2>&1 | Out-Null
& docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -f /tmp/qdrought3.sql 2>&1 |
    Out-String | Add-Content $out
Remove-Item $tmp -ErrorAction SilentlyContinue
"" | Add-Content $out

"## brain-worker mining + backtest cycle activity last 1h" | Add-Content $out
$blog = & docker logs --since 60m chili-home-copilot-brain-worker-1 2>&1
$mining = $blog | Select-String -Pattern "mine|backtest|fast_backtest|cpcv|composite|score_refresh|cohort" -CaseSensitive:$false
"  match count: $($mining.Count) (last 25)" | Add-Content $out
$mining | Select-Object -Last 25 | Out-String | Add-Content $out

"# end" | Add-Content $out
Write-Host "done"
