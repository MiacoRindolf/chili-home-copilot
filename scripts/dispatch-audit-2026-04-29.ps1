# Comprehensive trading-brain audit probe — 2026-04-29.
# Dumps live-state probes across 12 audit domains to scripts/audit-2026-04-29-output.txt.
# Usage:  .\scripts\dispatch-audit-2026-04-29.ps1
# After run, read scripts/audit-2026-04-29-output.txt directly.

$out = "scripts/audit-2026-04-29-output.txt"
$start = Get-Date
"# CHILI trading-brain audit probe $start" | Out-File $out -Encoding utf8

function Section {
    param([string]$Title, [scriptblock]$Body)
    "" | Add-Content $out
    "===== $Title =====" | Add-Content $out
    try {
        $r = & $Body 2>&1
        if ($r) { $r | Out-String | Add-Content $out }
    } catch {
        "ERROR: $_" | Add-Content $out
    }
}

function PG {
    param([string]$Sql)
    docker compose exec -T postgres psql -U chili -d chili -P pager=off -c $Sql 2>&1
}

# ---------------- Domain 1: containers / health ----------------
Section "Containers" { docker ps --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}" }

Section "DB version + last 3 migrations applied" {
    PG "SELECT version();"
    PG "SELECT id, name, applied_at FROM schema_migrations ORDER BY id DESC LIMIT 5;"
}

Section "pg_stat_activity by application_name (idle-in-tx + total)" {
    PG @"
SELECT
  COALESCE(application_name,'(unknown)') AS app,
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_in_tx,
  COUNT(*) FILTER (WHERE state='active') AS active,
  ROUND(EXTRACT(EPOCH FROM MAX(NOW() - xact_start))) AS max_xact_age_s
FROM pg_stat_activity
WHERE datname='chili'
GROUP BY application_name
ORDER BY total DESC;
"@
}

# ---------------- Domain 2: table freshness across snapshot tables ----------------
Section "Snapshot-table freshness" {
    PG @"
WITH t AS (
  SELECT 'regime_snapshot' AS tbl UNION ALL
  SELECT 'trading_ticker_regime_snapshots' UNION ALL
  SELECT 'trading_breadth_relstr_snapshots' UNION ALL
  SELECT 'trading_cross_asset_snapshots' UNION ALL
  SELECT 'trading_vol_dispersion_snapshots' UNION ALL
  SELECT 'trading_intraday_session_snapshots' UNION ALL
  SELECT 'trading_macro_regime_snapshots' UNION ALL
  SELECT 'trading_pattern_regime_performance_daily' UNION ALL
  SELECT 'trading_pattern_trades' UNION ALL
  SELECT 'trading_alerts' UNION ALL
  SELECT 'trading_trades' UNION ALL
  SELECT 'trading_autotrader_runs' UNION ALL
  SELECT 'brain_batch_jobs' UNION ALL
  SELECT 'trading_learning_events' UNION ALL
  SELECT 'scan_patterns' UNION ALL
  SELECT 'trading_snapshots'
)
SELECT * FROM t;
"@
    "--- per-table counts + last timestamp (timestamp column auto-discovered) ---" | Out-String
    $tables = @(
        @{n='regime_snapshot'; ts='created_at'},
        @{n='trading_ticker_regime_snapshots'; ts='snapshot_ts'},
        @{n='trading_breadth_relstr_snapshots'; ts='snapshot_ts'},
        @{n='trading_cross_asset_snapshots'; ts='snapshot_ts'},
        @{n='trading_vol_dispersion_snapshots'; ts='snapshot_ts'},
        @{n='trading_intraday_session_snapshots'; ts='snapshot_ts'},
        @{n='trading_macro_regime_snapshots'; ts='snapshot_ts'},
        @{n='trading_pattern_regime_performance_daily'; ts='as_of_date'},
        @{n='trading_pattern_trades'; ts='as_of_ts'},
        @{n='trading_alerts'; ts='created_at'},
        @{n='trading_trades'; ts='created_at'},
        @{n='trading_autotrader_runs'; ts='created_at'},
        @{n='brain_batch_jobs'; ts='started_at'},
        @{n='trading_learning_events'; ts='occurred_at'},
        @{n='scan_patterns'; ts='updated_at'},
        @{n='trading_snapshots'; ts='created_at'}
    )
    foreach ($t in $tables) {
        $sql = "SELECT '$($t.n)' AS tbl, COUNT(*) AS rows, MAX($($t.ts))::text AS last_ts FROM $($t.n);"
        PG $sql
    }
}

# ---------------- Domain 3: pattern lifecycle distribution ----------------
Section "scan_patterns lifecycle distribution" {
    PG @"
SELECT lifecycle_stage, promotion_status, COUNT(*) AS n
FROM scan_patterns
GROUP BY 1,2
ORDER BY 1,2;
"@
    PG @"
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE win_rate IS NULL) AS wr_null,
  COUNT(*) FILTER (WHERE win_rate < 0 OR win_rate > 1) AS wr_oor,
  COUNT(*) FILTER (WHERE win_rate = 'NaN'::numeric) AS wr_nan,
  COUNT(*) FILTER (WHERE avg_return_pct IS NULL) AS arp_null,
  COUNT(*) FILTER (WHERE avg_return_pct = 'NaN'::numeric) AS arp_nan,
  MIN(win_rate) AS wr_min, MAX(win_rate) AS wr_max,
  MIN(avg_return_pct) AS arp_min, MAX(avg_return_pct) AS arp_max
FROM scan_patterns;
"@
}

Section "Promoted patterns (lifecycle_stage='promoted')" {
    PG @"
SELECT id, name, win_rate, avg_return_pct, trade_count, lifecycle_stage, promotion_status, updated_at::date
FROM scan_patterns
WHERE lifecycle_stage='promoted'
ORDER BY id
LIMIT 30;
"@
}

# ---------------- Domain 4: data hygiene on pattern_trades ----------------
Section "trading_pattern_trades hygiene" {
    PG @"
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE outcome_return_pct IS NULL) AS r_null,
  COUNT(*) FILTER (WHERE outcome_return_pct = 'NaN'::numeric) AS r_nan,
  MIN(outcome_return_pct) AS r_min,
  MAX(outcome_return_pct) AS r_max,
  AVG(outcome_return_pct) AS r_avg,
  STDDEV(outcome_return_pct) AS r_std
FROM trading_pattern_trades;
"@
    PG @"
-- detect percent-vs-fraction confusion. If most returns are |x|>1.5 it's percent;
-- if |x|<0.5 it's fraction.  Mixed magnitudes => bug.
SELECT
  width_bucket(outcome_return_pct, -1, 1, 10) AS bucket_neg1_to_1,
  COUNT(*)
FROM trading_pattern_trades
WHERE outcome_return_pct IS NOT NULL
GROUP BY 1
ORDER BY 1;
"@
    PG @"
SELECT
  COUNT(*) FILTER (WHERE ABS(outcome_return_pct) > 5) AS mag_gt_5,
  COUNT(*) FILTER (WHERE ABS(outcome_return_pct) BETWEEN 1 AND 5) AS mag_1_to_5,
  COUNT(*) FILTER (WHERE ABS(outcome_return_pct) <= 1) AS mag_le_1
FROM trading_pattern_trades;
"@
}

# ---------------- Domain 5: regime ledger coverage ----------------
Section "trading_pattern_regime_performance_daily by dim/mode" {
    PG @"
SELECT regime_dim, mode, COUNT(*) AS n, MAX(as_of_date) AS latest, MIN(as_of_date) AS earliest
FROM trading_pattern_regime_performance_daily
GROUP BY 1,2 ORDER BY 1,2;
"@
}

# ---------------- Domain 6: autotrader funnel last 24h ----------------
Section "trading_autotrader_runs decisions last 24h" {
    PG @"
SELECT decision, reason, COUNT(*) AS n
FROM trading_autotrader_runs
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 60;
"@
}

Section "trading_alerts last 24h by alert_type" {
    PG @"
SELECT alert_type, COUNT(*) AS n
FROM trading_alerts
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 2 DESC LIMIT 30;
"@
}

Section "trading_trades open + last 24h by status" {
    PG @"
SELECT status, COUNT(*) AS n,
       COUNT(*) FILTER (WHERE broker_order_id IS NULL) AS null_oid,
       COUNT(*) FILTER (WHERE entry_price = 0 OR entry_price IS NULL) AS bad_entry
FROM trading_trades
GROUP BY 1 ORDER BY 1;
"@
    PG @"
SELECT exit_reason, COUNT(*) AS n
FROM trading_trades
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 2 DESC LIMIT 30;
"@
    PG @"
SELECT id, ticker, status, entry_price, broker_order_id IS NULL AS null_oid, exit_reason, created_at
FROM trading_trades
WHERE status='open'
ORDER BY id DESC LIMIT 20;
"@
}

# ---------------- Domain 7: brain_batch_jobs heartbeat ----------------
Section "brain_batch_jobs last successful per job_type (24h window)" {
    PG @"
SELECT job_type,
       COUNT(*) AS runs_24h,
       MAX(started_at) AS last_start,
       MAX(finished_at) AS last_finish,
       COUNT(*) FILTER (WHERE status='ok') AS ok,
       COUNT(*) FILTER (WHERE status='error') AS err,
       COUNT(*) FILTER (WHERE status='running') AS running
FROM brain_batch_jobs
WHERE started_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1;
"@
    PG @"
-- jobs that haven't run in >6h
SELECT job_type, MAX(started_at) AS last_start, NOW() - MAX(started_at) AS staleness
FROM brain_batch_jobs
GROUP BY 1
HAVING NOW() - MAX(started_at) > INTERVAL '6 hours'
ORDER BY 2 ASC;
"@
}

Section "Long-running brain jobs (>30min)" {
    PG @"
SELECT id, job_type, started_at, status, NOW() - started_at AS age
FROM brain_batch_jobs
WHERE status='running' AND started_at < NOW() - INTERVAL '30 minutes'
ORDER BY started_at ASC LIMIT 20;
"@
}

# ---------------- Domain 8: learning events ----------------
Section "trading_learning_events last 24h" {
    PG @"
SELECT event_type, COUNT(*) AS n,
       MIN(occurred_at) AS first, MAX(occurred_at) AS last
FROM trading_learning_events
WHERE occurred_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
"@
}

# ---------------- Domain 9: ticker normalization ----------------
Section "Ticker normalization probe" {
    PG @"
SELECT ticker, COUNT(*) AS n FROM scan_patterns
WHERE ticker LIKE '^%' OR ticker LIKE 'I:%' OR ticker LIKE 'X:%' OR ticker ~ '-USD$'
GROUP BY 1 ORDER BY 2 DESC LIMIT 40;
"@
    PG @"
SELECT ticker, COUNT(*) AS n FROM trading_pattern_trades
WHERE ticker LIKE '^%' OR ticker LIKE 'I:%' OR ticker LIKE 'X:%'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
"@
}

# ---------------- Domain 10: settings via runtime env ----------------
Section "scheduler-worker dispatch-relevant env" {
    docker compose exec -T scheduler-worker bash -c 'env | grep -E "^(MARKET_DATA_|CHILI_AUTOTRADER_|CHILI_REGIME_|CHILI_PATTERN_|CHILI_BRAIN_|BRAIN_|MASSIVE_|POLYGON_|YFINANCE_|FRED_|COINGECKO_|CHILI_DRAWDOWN_|CHILI_KILL_)" | sort'
}

Section "chili-app env (provider + flags)" {
    docker compose exec -T chili bash -c 'env | grep -E "^(MARKET_DATA_|CHILI_AUTOTRADER_|CHILI_REGIME_|CHILI_PATTERN_|CHILI_BRAIN_|BRAIN_|MASSIVE_|POLYGON_|YFINANCE_|FRED_|COINGECKO_|CHILI_DRAWDOWN_|CHILI_KILL_)" | sort'
}

# ---------------- Domain 11: kill switch + breaker state ----------------
Section "Kill switches and risk state" {
    PG "SELECT * FROM trading_risk_state ORDER BY snapshot_date DESC LIMIT 3;"
    PG "SELECT * FROM code_kill_switch_state;"
}

# ---------------- Domain 12: bracket intent shadow vs live ----------------
Section "bracket_intents distribution" {
    PG @"
SELECT
  COALESCE((SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name='trading_bracket_intents'),0) AS intent_table_present;
"@
    # The exact column might be 'mode' or 'intent_status' — try both
    PG @"
DO `$`$
DECLARE col_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM information_schema.columns
                WHERE table_name='trading_bracket_intents' AND column_name='mode')
    INTO col_exists;
  IF col_exists THEN
    RAISE NOTICE 'mode-column present';
  END IF;
END `$`$;
"@
    PG "\\d+ trading_bracket_intents"
}

# ---------------- Domain 13: rescue / restore migrations ----------------
Section "Recent migration tail (mig 180+)" {
    PG @"
SELECT id, name, applied_at FROM schema_migrations
WHERE id >= 180 ORDER BY id;
"@
}

# ---------------- Domain 14: yfinance dead-marks (in-memory, via app probe) ----------------
Section "yf_session probe (introspect dead set if any)" {
    docker compose exec -T chili python -c "from app.services.yf_session import _DEAD_TICKERS, _EMPTY_THRESHOLD, _CONSECUTIVE_EMPTY; print('threshold=', _EMPTY_THRESHOLD); print('dead_tickers=', dict(_DEAD_TICKERS)); print('consec_empty=', dict(_CONSECUTIVE_EMPTY))" 2>&1
}

# ---------------- Domain 15: scan_patterns CHECK constraint state ----------------
Section "CHECK constraints on scan_patterns" {
    PG @"
SELECT conname, pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class t ON c.conrelid = t.oid
WHERE t.relname='scan_patterns' AND c.contype='c';
"@
}

# ---------------- Domain 16: ticker_scope_autotune state ----------------
Section "ticker_scope_autotune state" {
    PG @"
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE included) AS included_n,
       COUNT(DISTINCT pattern_id) AS distinct_patterns,
       COUNT(DISTINCT ticker) AS distinct_tickers,
       MAX(updated_at) AS last_update
FROM trading_pattern_ticker_scope;
"@
}

$elapsed = ((Get-Date) - $start).TotalSeconds
"" | Add-Content $out
"===== Done in $([Math]::Round($elapsed,1))s =====" | Add-Content $out
Write-Host "done"
