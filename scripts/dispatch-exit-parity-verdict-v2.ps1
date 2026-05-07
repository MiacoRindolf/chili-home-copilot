# Exit-engine parity verdict v2: query trading_exit_parity_log using
# the migration-230 columns (action_class, label_match,
# exit_price_drift_bps, priority_winner) to answer the questions an
# algo trader actually needs answered before flipping the canonical
# engine to authoritative:
#
#   - Which engine is more aggressive at closing?
#   - When both close, do they pick the same exit price?
#   - Is there a systematic P/L bias toward one engine?
#   - Which RULE differences drive the disagreements?
#   - Is parity drifting over time, or stable?
#
# The cutover gate (separate script,
# dispatch-exit-parity-cutover-gate.ps1) consumes section 2's
# tracking-error and bias signals to produce a single PASS / FAIL
# verdict.
#
# Output: scripts/dispatch-exit-parity-verdict-v2-output.txt

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-exit-parity-verdict-v2-output.txt"
"# exit-parity verdict v2 $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"---" | Add-Content $out

function Run-Section {
    param([string]$Header, [string]$Sql, [string]$Note = "")
    "" | Add-Content $out
    "## $Header" | Add-Content $out
    if ($Note -ne "") { "   $Note" | Add-Content $out }
    "" | Add-Content $out
    try {
        $env:PGPASSWORD = "chili"
        $pgOut = docker compose exec -T postgres psql -U chili -d chili -P pager=off -c $Sql 2>&1
        $pgOut | Add-Content $out
    } catch {
        "ERROR: $_" | Add-Content $out
    }
}

Run-Section "1. Action-class population (last 24h, by source)" @"
SELECT
    source,
    action_class,
    COUNT(*) AS n,
    ROUND(100.0 * COUNT(*)::numeric / SUM(COUNT(*)) OVER (PARTITION BY source), 2) AS pct_of_source
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
GROUP BY source, action_class
ORDER BY source, n DESC;
"@

Run-Section "2. Tracking error and bias on both_close rows (last 24h, by source)" @"
SELECT
    source,
    COUNT(*) AS both_close_n,
    ROUND(AVG(exit_price_drift_bps)::numeric, 4)              AS bias_bps,
    ROUND(STDDEV(exit_price_drift_bps)::numeric, 4)           AS tracking_error_bps,
    ROUND(
        (AVG(exit_price_drift_bps) /
         NULLIF(STDDEV(exit_price_drift_bps) / SQRT(COUNT(*)::float), 0)
        )::numeric, 4
    ) AS t_statistic,
    ROUND(MIN(exit_price_drift_bps)::numeric, 4)              AS worst_drift_bps,
    ROUND(MAX(exit_price_drift_bps)::numeric, 4)              AS best_drift_bps
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND action_class = 'both_close'
  AND exit_price_drift_bps IS NOT NULL
GROUP BY source;
"@ "The single quantitative answer: are the engines P/L equivalent?"

Run-Section "3. Label-match rate on both_close rows (last 24h)" @"
SELECT
    source,
    COUNT(*) AS both_close_n,
    COUNT(*) FILTER (WHERE label_match = TRUE) AS labels_match,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE label_match = TRUE)::numeric
        / NULLIF(COUNT(*),0), 2
    ) AS labels_match_pct
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class = 'both_close'
GROUP BY source;
"@

Run-Section "4. Asymmetric-close imbalance (last 24h)" @"
SELECT
    source,
    COUNT(*) FILTER (WHERE action_class = 'canonical_only_close') AS canonical_only_n,
    COUNT(*) FILTER (WHERE action_class = 'legacy_only_close')    AS legacy_only_n,
    ROUND(
        COUNT(*) FILTER (WHERE action_class = 'canonical_only_close')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE action_class IN ('canonical_only_close','legacy_only_close')), 0)
    , 4) AS canonical_aggressive_share
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
GROUP BY source;
"@ "Ideal canonical_aggressive_share is 0.5 (balanced). Skew >=0.6 or <=0.4 means one engine is consistently more aggressive."

Run-Section "5. Priority-winner cohort breakdown (last 24h)" @"
SELECT
    source,
    priority_winner,
    COUNT(*) AS n,
    ROUND(AVG(exit_price_drift_bps)::numeric, 4) AS avg_drift_bps_for_this_winner,
    ROUND(STDDEV(exit_price_drift_bps)::numeric, 4) AS stddev_drift_bps_for_this_winner
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND action_class IN ('both_close', 'canonical_only_close', 'legacy_only_close')
  AND priority_winner IS NOT NULL
GROUP BY source, priority_winner
ORDER BY source, n DESC;
"@

Run-Section "6. Rolling tracking error: last 1h vs last 24h vs last 7d" @"
WITH windows AS (
    SELECT '1h' AS w, NOW() - INTERVAL '1 hour' AS cutoff
    UNION ALL SELECT '24h', NOW() - INTERVAL '24 hours'
    UNION ALL SELECT '7d', NOW() - INTERVAL '7 days'
)
SELECT
    w.w AS window,
    p.source,
    COUNT(*) AS n,
    ROUND(AVG(p.exit_price_drift_bps)::numeric, 4) AS bias_bps,
    ROUND(STDDEV(p.exit_price_drift_bps)::numeric, 4) AS tracking_error_bps
FROM windows w
LEFT JOIN trading_exit_parity_log p
    ON p.created_at >= w.cutoff
    AND p.action_class = 'both_close'
    AND p.exit_price_drift_bps IS NOT NULL
GROUP BY w.w, p.source
ORDER BY p.source, w.w;
"@

"`n---" | Add-Content $out
"# end of exit-parity verdict v2" | Add-Content $out
Write-Output "Wrote $out"
