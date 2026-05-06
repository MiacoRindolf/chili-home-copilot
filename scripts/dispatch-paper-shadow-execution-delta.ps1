# Paper-shadow execution-alpha-drag probe
#
# Pairs each closed paper-shadow trade with its corresponding live Trade
# (joined via paper_shadow_of_alert_id <-> related_alert_id) and reports
# the realized P/L delta. Positive delta_bps = shadow did better than
# live (slippage hurt live execution). Negative = shadow did worse.
#
# Output: scripts/dispatch-paper-shadow-execution-delta-output.txt

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-paper-shadow-execution-delta-output.txt"
"# paper-shadow execution-delta $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"---" | Add-Content $out

function Run-Section {
    param([string]$Header, [string]$Sql)
    "" | Add-Content $out
    "## $Header" | Add-Content $out
    "" | Add-Content $out
    try {
        $env:PGPASSWORD = "chili"
        $pgOut = docker compose exec -T postgres psql -U chili -d chili -P pager=off -c $Sql 2>&1
        $pgOut | Add-Content $out
    } catch {
        "ERROR: $_" | Add-Content $out
    }
}

Run-Section "1. Shadow row counts (last 7 days)" @"
SELECT
    COUNT(*) AS shadow_total,
    COUNT(*) FILTER (WHERE status = 'open') AS shadow_open,
    COUNT(*) FILTER (WHERE status = 'closed') AS shadow_closed,
    COUNT(*) FILTER (WHERE status = 'expired') AS shadow_expired
FROM trading_paper_trades
WHERE paper_shadow_of_alert_id IS NOT NULL
  AND COALESCE(exit_date, entry_date) >= NOW() - INTERVAL '7 days';
"@

Run-Section "2. Live-vs-shadow paired closes (last 7 days)" @"
WITH pairs AS (
    SELECT
        pt.paper_shadow_of_alert_id AS alert_id,
        pt.id AS shadow_id,
        pt.scan_pattern_id,
        pt.ticker,
        pt.entry_price AS shadow_entry_price,
        pt.exit_price AS shadow_exit_price,
        pt.pnl AS shadow_pnl,
        pt.exit_reason AS shadow_exit_reason,
        t.id AS live_id,
        t.entry_price AS live_entry_price,
        t.exit_price AS live_exit_price,
        t.pnl AS live_pnl,
        t.exit_reason AS live_exit_reason
    FROM trading_paper_trades pt
    LEFT JOIN trading_trades t
        ON t.related_alert_id = pt.paper_shadow_of_alert_id
       AND t.broker_source = 'robinhood'
       AND t.management_scope = 'auto_trader_v1'
    WHERE pt.paper_shadow_of_alert_id IS NOT NULL
      AND pt.status = 'closed'
      AND pt.exit_date >= NOW() - INTERVAL '7 days'
)
SELECT
    p.alert_id,
    p.scan_pattern_id,
    p.ticker,
    ROUND(p.shadow_pnl::numeric, 2) AS shadow_pnl,
    ROUND(p.live_pnl::numeric, 2) AS live_pnl,
    ROUND(
        (COALESCE(p.shadow_pnl, 0) - COALESCE(p.live_pnl, 0))::numeric
        / NULLIF(p.shadow_entry_price, 0)::numeric * 10000.0,
        2
    ) AS delta_bps,
    p.live_exit_reason,
    p.shadow_exit_reason
FROM pairs p
ORDER BY ABS(COALESCE(p.shadow_pnl, 0) - COALESCE(p.live_pnl, 0)) DESC
LIMIT 30;
"@

Run-Section "3. Aggregate execution-drag stats (last 7 days, only paired closes)" @"
WITH pairs AS (
    SELECT
        pt.paper_shadow_of_alert_id AS alert_id,
        pt.shadow_pnl AS shadow_pnl,
        pt.entry_price AS shadow_entry_price,
        t.pnl AS live_pnl
    FROM (
        SELECT id, paper_shadow_of_alert_id, pnl AS shadow_pnl, entry_price
        FROM trading_paper_trades
        WHERE paper_shadow_of_alert_id IS NOT NULL
          AND status = 'closed'
          AND exit_date >= NOW() - INTERVAL '7 days'
    ) pt
    JOIN trading_trades t
        ON t.related_alert_id = pt.paper_shadow_of_alert_id
       AND t.broker_source = 'robinhood'
       AND t.management_scope = 'auto_trader_v1'
       AND t.status = 'closed'
)
SELECT
    COUNT(*) AS n_pairs,
    ROUND(AVG(shadow_pnl - live_pnl)::numeric, 4) AS mean_pnl_delta_usd,
    ROUND(STDDEV(shadow_pnl - live_pnl)::numeric, 4) AS stddev_pnl_delta_usd,
    ROUND(
        AVG(
            (shadow_pnl - live_pnl) / NULLIF(shadow_entry_price, 0) * 10000.0
        )::numeric,
        2
    ) AS mean_delta_bps,
    ROUND(
        STDDEV(
            (shadow_pnl - live_pnl) / NULLIF(shadow_entry_price, 0) * 10000.0
        )::numeric,
        2
    ) AS stddev_delta_bps,
    -- t-statistic for "is mean delta significantly non-zero at 95% CI?"
    -- |t| > ~2 means yes; needs n_pairs >= ~30 for the approximation.
    ROUND(
        AVG(shadow_pnl - live_pnl)::numeric
        / NULLIF(STDDEV(shadow_pnl - live_pnl) / SQRT(NULLIF(COUNT(*), 0)), 0)::numeric,
        2
    ) AS t_stat
FROM pairs;
"@

Run-Section "4. Per-pattern execution-drag (last 7 days)" @"
WITH pairs AS (
    SELECT
        pt.scan_pattern_id,
        pt.pnl AS shadow_pnl,
        pt.entry_price AS shadow_entry_price,
        t.pnl AS live_pnl
    FROM trading_paper_trades pt
    JOIN trading_trades t
        ON t.related_alert_id = pt.paper_shadow_of_alert_id
       AND t.broker_source = 'robinhood'
       AND t.management_scope = 'auto_trader_v1'
       AND t.status = 'closed'
    WHERE pt.paper_shadow_of_alert_id IS NOT NULL
      AND pt.status = 'closed'
      AND pt.exit_date >= NOW() - INTERVAL '7 days'
)
SELECT
    scan_pattern_id,
    COUNT(*) AS n_pairs,
    ROUND(AVG(shadow_pnl - live_pnl)::numeric, 4) AS mean_pnl_delta_usd,
    ROUND(
        AVG(
            (shadow_pnl - live_pnl) / NULLIF(shadow_entry_price, 0) * 10000.0
        )::numeric,
        2
    ) AS mean_delta_bps
FROM pairs
GROUP BY scan_pattern_id
HAVING COUNT(*) >= 3
ORDER BY ABS(AVG(shadow_pnl - live_pnl)) DESC
LIMIT 20;
"@

Run-Section "5. Opportunity cost: shadows on blocked/skipped live decisions (last 7 days)" @"
SELECT
    pt.id AS shadow_id,
    pt.ticker,
    pt.scan_pattern_id,
    pt.entry_date,
    pt.status,
    ROUND(pt.pnl::numeric, 2) AS shadow_pnl,
    pt.exit_reason
FROM trading_paper_trades pt
LEFT JOIN trading_trades t
    ON t.related_alert_id = pt.paper_shadow_of_alert_id
   AND t.broker_source = 'robinhood'
   AND t.management_scope = 'auto_trader_v1'
WHERE pt.paper_shadow_of_alert_id IS NOT NULL
  AND pt.entry_date >= NOW() - INTERVAL '7 days'
  AND t.id IS NULL  -- shadow exists but no matching live trade row
ORDER BY pt.pnl DESC NULLS LAST
LIMIT 20;
"@

"" | Add-Content $out
"# Done. Re-run after >=24h of shadow data for stable t-stat readings." | Add-Content $out
