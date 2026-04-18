-- Restore scan_patterns + trading_breakout_alerts from the Apr 7 pre_migrate
-- snapshot (staged in the sibling DB `chili_recover`) into the live `chili`
-- database. Re-link current open trades → restored alerts by ticker + recency.
--
-- Safety:
--   * scan_patterns: insert only rows whose name does NOT already exist in live.
--   * trading_breakout_alerts: insert only rows whose (ticker, alerted_at)
--     pair is not already present in live.
--   * trading_trades: only UPDATE rows where scan_pattern_id IS NULL and
--     related_alert_id IS NULL (never overwrite an existing assignment).
--
-- Re-run is idempotent thanks to the NOT EXISTS guards above.

BEGIN;

-- ── 1. scan_patterns ────────────────────────────────────────────────

-- Snapshot of backup scan_patterns (collapsing backup's own name duplicates).
CREATE TEMP TABLE _src_patterns ON COMMIT DROP AS
SELECT DISTINCT ON (name) *
FROM dblink(
    'dbname=chili_recover user=chili',
    $$SELECT
        id, name, description, rules_json, origin, asset_class, timeframe,
        confidence, evidence_count, win_rate, avg_return_pct,
        backtest_count, score_boost, min_base_score, active, parent_id,
        exit_config, variant_label, generation, ticker_scope, scope_tickers,
        trade_count, backtest_priority, last_backtest_at,
        created_at, updated_at, promotion_status,
        oos_win_rate, oos_avg_return_pct, oos_trade_count,
        backtest_spread_used, backtest_commission_used, oos_evaluated_at,
        bench_walk_forward_json, hypothesis_family, oos_validation_json,
        queue_tier, paper_book_json, lifecycle_stage, lifecycle_changed_at,
        user_id
      FROM scan_patterns$$
) AS t(
    id integer, name varchar, description text, rules_json jsonb,
    origin varchar, asset_class varchar, timeframe varchar,
    confidence double precision, evidence_count integer,
    win_rate double precision, avg_return_pct double precision,
    backtest_count integer, score_boost double precision,
    min_base_score double precision, active boolean, parent_id integer,
    exit_config jsonb, variant_label varchar, generation integer,
    ticker_scope varchar, scope_tickers text, trade_count integer,
    backtest_priority integer, last_backtest_at timestamp,
    created_at timestamp, updated_at timestamp, promotion_status varchar,
    oos_win_rate double precision, oos_avg_return_pct double precision,
    oos_trade_count integer, backtest_spread_used double precision,
    backtest_commission_used double precision, oos_evaluated_at timestamp,
    bench_walk_forward_json jsonb, hypothesis_family varchar,
    oos_validation_json jsonb, queue_tier varchar, paper_book_json jsonb,
    lifecycle_stage varchar, lifecycle_changed_at timestamp,
    user_id integer
)
ORDER BY name, id DESC;

-- Insert patterns whose name is not already in live.
INSERT INTO scan_patterns (
    name, description, rules_json, origin, asset_class, timeframe,
    confidence, evidence_count, win_rate, avg_return_pct,
    backtest_count, score_boost, min_base_score, active, parent_id,
    exit_config, variant_label, generation, ticker_scope, scope_tickers,
    trade_count, backtest_priority, last_backtest_at,
    created_at, updated_at, promotion_status,
    oos_win_rate, oos_avg_return_pct, oos_trade_count,
    backtest_spread_used, backtest_commission_used, oos_evaluated_at,
    bench_walk_forward_json, hypothesis_family, oos_validation_json,
    queue_tier, paper_book_json, lifecycle_stage, lifecycle_changed_at,
    user_id
)
SELECT
    s.name, s.description, s.rules_json, s.origin, s.asset_class, s.timeframe,
    s.confidence, s.evidence_count, s.win_rate, s.avg_return_pct,
    s.backtest_count, s.score_boost, s.min_base_score, s.active, NULL,  -- parent_id omitted (old FK)
    s.exit_config, s.variant_label, s.generation, s.ticker_scope, s.scope_tickers,
    s.trade_count, s.backtest_priority, s.last_backtest_at,
    s.created_at, s.updated_at, s.promotion_status,
    s.oos_win_rate, s.oos_avg_return_pct, s.oos_trade_count,
    s.backtest_spread_used, s.backtest_commission_used, s.oos_evaluated_at,
    s.bench_walk_forward_json, s.hypothesis_family, s.oos_validation_json,
    s.queue_tier, s.paper_book_json, s.lifecycle_stage, s.lifecycle_changed_at,
    s.user_id
FROM _src_patterns s
WHERE NOT EXISTS (
    SELECT 1 FROM scan_patterns p WHERE p.name = s.name
);

-- Build old_id → new_id map (covers BOTH name collisions AND just-inserted rows).
-- DISTINCT ON (old_id) guards against any duplicate old ids (shouldn't happen,
-- but keeps the script defensive).
CREATE TEMP TABLE _pattern_id_map ON COMMIT DROP AS
SELECT DISTINCT ON (s.id)
    s.id  AS old_id,
    p.id  AS new_id,
    p.name
FROM _src_patterns s
JOIN scan_patterns p ON p.name = s.name;

SELECT 'patterns_mapped'    AS label, COUNT(*) AS n FROM _pattern_id_map;
SELECT 'patterns_live_total' AS label, COUNT(*) AS n FROM scan_patterns;

-- ── 2. trading_breakout_alerts ──────────────────────────────────────

CREATE TEMP TABLE _src_alerts ON COMMIT DROP AS
SELECT *
FROM dblink(
    'dbname=chili_recover user=chili',
    $$SELECT
        id, ticker, alert_tier, score_at_alert, price_at_alert,
        target_price, stop_loss, timeframe, scan_pattern_id,
        asset_type, sector, regime_at_alert, news_sentiment_at_alert,
        indicator_snapshot, signals_snapshot, alerted_at, outcome,
        breakout_occurred, price_1h, price_4h, price_24h,
        max_gain_pct, max_drawdown_pct, time_to_peak_hours, time_to_stop_hours,
        price_at_peak, optimal_exit_pct, outcome_checked_at, outcome_notes,
        user_id, entry_price
      FROM trading_breakout_alerts$$
) AS t(
    id integer, ticker varchar, alert_tier varchar,
    score_at_alert double precision, price_at_alert double precision,
    target_price double precision, stop_loss double precision,
    timeframe varchar, scan_pattern_id integer,
    asset_type varchar, sector varchar, regime_at_alert varchar,
    news_sentiment_at_alert double precision,
    indicator_snapshot jsonb, signals_snapshot jsonb,
    alerted_at timestamp, outcome varchar, breakout_occurred boolean,
    price_1h double precision, price_4h double precision,
    price_24h double precision,
    max_gain_pct double precision, max_drawdown_pct double precision,
    time_to_peak_hours double precision, time_to_stop_hours double precision,
    price_at_peak double precision, optimal_exit_pct double precision,
    outcome_checked_at timestamp, outcome_notes text,
    user_id integer, entry_price double precision
);

-- Insert alerts that aren't already in live (by ticker + alerted_at), remapping
-- scan_pattern_id via the map; scan_cycle_id / related_insight_id are NULLed
-- because those foreign keys refer to rows that no longer exist in live.
INSERT INTO trading_breakout_alerts (
    ticker, alert_tier, score_at_alert, price_at_alert,
    target_price, stop_loss, timeframe, scan_pattern_id, scan_cycle_id,
    asset_type, sector, regime_at_alert, news_sentiment_at_alert,
    indicator_snapshot, signals_snapshot, alerted_at, outcome,
    breakout_occurred, price_1h, price_4h, price_24h,
    max_gain_pct, max_drawdown_pct, time_to_peak_hours, time_to_stop_hours,
    price_at_peak, optimal_exit_pct, outcome_checked_at, outcome_notes,
    user_id, entry_price, related_insight_id
)
SELECT
    s.ticker, s.alert_tier, s.score_at_alert, s.price_at_alert,
    s.target_price, s.stop_loss, s.timeframe, m.new_id, NULL,
    s.asset_type, s.sector, s.regime_at_alert, s.news_sentiment_at_alert,
    s.indicator_snapshot, s.signals_snapshot, s.alerted_at, s.outcome,
    s.breakout_occurred, s.price_1h, s.price_4h, s.price_24h,
    s.max_gain_pct, s.max_drawdown_pct, s.time_to_peak_hours, s.time_to_stop_hours,
    s.price_at_peak, s.optimal_exit_pct, s.outcome_checked_at, s.outcome_notes,
    s.user_id, s.entry_price, NULL
FROM _src_alerts s
LEFT JOIN _pattern_id_map m ON m.old_id = s.scan_pattern_id
WHERE NOT EXISTS (
    SELECT 1 FROM trading_breakout_alerts a
    WHERE a.ticker = s.ticker AND a.alerted_at = s.alerted_at
);

SELECT 'alerts_live_total' AS label, COUNT(*) AS n FROM trading_breakout_alerts;

-- ── 3. Link current open trades → restored alerts/patterns ──────────
-- For each open trade with no alert link, pick the restored alert for the
-- same ticker with the most recent alerted_at (tie-break by score_at_alert)
-- within a 60-day window. Any tier (not just pattern_imminent).

WITH candidate AS (
    SELECT DISTINCT ON (UPPER(a.ticker))
        UPPER(a.ticker) AS ticker_u,
        a.id            AS alert_id,
        a.scan_pattern_id
    FROM trading_breakout_alerts a
    WHERE a.alerted_at >= NOW() - INTERVAL '60 days'
    ORDER BY UPPER(a.ticker), a.alerted_at DESC, COALESCE(a.score_at_alert, 0) DESC
)
UPDATE trading_trades t
SET related_alert_id = c.alert_id,
    scan_pattern_id  = COALESCE(t.scan_pattern_id, c.scan_pattern_id)
FROM candidate c
WHERE t.status = 'open'
  AND t.related_alert_id IS NULL
  AND UPPER(t.ticker) = c.ticker_u;

SELECT 'trades_linked' AS label,
       (SELECT COUNT(*) FROM trading_trades
         WHERE status='open' AND related_alert_id IS NOT NULL) AS linked_open,
       (SELECT COUNT(*) FROM trading_trades
         WHERE status='open' AND scan_pattern_id IS NOT NULL) AS patterned_open,
       (SELECT COUNT(*) FROM trading_trades
         WHERE status='open') AS total_open;

COMMIT;
