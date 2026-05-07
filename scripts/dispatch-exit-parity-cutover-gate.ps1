# Exit-engine parity CUTOVER GATE: composite verdict on whether the
# canonical engine is safe to flip to authoritative mode.
#
# This is the single-query gate that consumes the v2 metric columns
# (action_class, label_match, exit_price_drift_bps) and returns one
# of {INSUFFICIENT_DATA, FAIL_BIAS_SIGNIFICANT, FAIL_TRACKING_ERROR_HIGH,
# FAIL_ASYMMETRIC_AGGRESSIVE, PASS} per source.
#
# Threshold constants (well-known quant defaults, NOT magic numbers):
#   T_STAT_CRITICAL = 1.96  -- 95% CI z-score, two-sided. Standard
#                              significance threshold.
#   TE_MAX_BPS      = 10.0  -- ~1bp/% -- looser than typical execution
#                              tracking error, tight enough to detect
#                              material engine drift.
#   ASYM_LOW        = 0.4   -- balanced asymmetric-close share lower
#                              bound; below means legacy is more
#                              aggressive than canonical.
#   ASYM_HIGH       = 0.6   -- upper bound; above means canonical is
#                              more aggressive than legacy.
#   MIN_SAMPLE_N    = 1000  -- per-source minimum both_close rows
#                              before any verdict is rendered.
#
# Per-source verdict precedence (first match wins):
#   1. INSUFFICIENT_DATA      if both_close_n < MIN_SAMPLE_N
#   2. FAIL_BIAS_SIGNIFICANT  if |t_stat| > T_STAT_CRITICAL
#   3. FAIL_TRACKING_ERROR_HIGH if te_bps > TE_MAX_BPS
#   4. FAIL_ASYMMETRIC_AGGRESSIVE if share outside [ASYM_LOW, ASYM_HIGH]
#   5. PASS
#
# Output: scripts/dispatch-exit-parity-cutover-gate-output.txt

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-exit-parity-cutover-gate-output.txt"
"# exit-parity cutover gate $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"---" | Add-Content $out

$sql = @"
WITH stats AS (
    SELECT
        source,
        COUNT(*) AS both_close_n,
        AVG(exit_price_drift_bps)                                AS bias_bps,
        STDDEV(exit_price_drift_bps)                             AS te_bps,
        AVG(exit_price_drift_bps)
            / NULLIF(STDDEV(exit_price_drift_bps)
                     / SQRT(COUNT(*)::float), 0)                 AS t_stat,
        COUNT(*) FILTER (WHERE label_match = FALSE)              AS label_mismatches
    FROM trading_exit_parity_log
    WHERE created_at >= NOW() - INTERVAL '24 hours'
      AND action_class = 'both_close'
      AND exit_price_drift_bps IS NOT NULL
    GROUP BY source
),
asym AS (
    SELECT
        source,
        COUNT(*) FILTER (WHERE action_class = 'canonical_only_close')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE action_class
                IN ('canonical_only_close', 'legacy_only_close')), 0)
            AS canonical_aggressive_share,
        COUNT(*) FILTER (WHERE action_class
            IN ('canonical_only_close', 'legacy_only_close')) AS asym_n
    FROM trading_exit_parity_log
    WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
    GROUP BY source
)
SELECT
    s.source,
    s.both_close_n,
    ROUND(s.bias_bps::numeric, 4)         AS bias_bps,
    ROUND(s.te_bps::numeric, 4)           AS te_bps,
    ROUND(s.t_stat::numeric, 4)           AS t_stat,
    s.label_mismatches,
    a.asym_n,
    ROUND(a.canonical_aggressive_share::numeric, 4) AS canonical_aggressive_share,
    CASE
        WHEN s.both_close_n < 1000                            THEN 'INSUFFICIENT_DATA'
        WHEN ABS(s.t_stat) > 1.96                             THEN 'FAIL_BIAS_SIGNIFICANT'
        WHEN s.te_bps > 10                                    THEN 'FAIL_TRACKING_ERROR_HIGH'
        WHEN a.canonical_aggressive_share < 0.4
          OR a.canonical_aggressive_share > 0.6               THEN 'FAIL_ASYMMETRIC_AGGRESSIVE'
        ELSE 'PASS'
    END AS verdict
FROM stats s
LEFT JOIN asym a USING (source);
"@

try {
    $env:PGPASSWORD = "chili"
    $pgOut = docker compose exec -T postgres psql -U chili -d chili -P pager=off -c $sql 2>&1
    $pgOut | Add-Content $out
} catch {
    "ERROR: $_" | Add-Content $out
}

"`n---" | Add-Content $out
"# end of exit-parity cutover gate" | Add-Content $out
Write-Output "Wrote $out"
