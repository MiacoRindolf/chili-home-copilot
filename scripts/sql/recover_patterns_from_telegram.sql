-- Recover pattern assignments for open positions using evidence from the
-- Telegram chili-trading-alerts chat export (Apr 2 – Apr 15, 2026).
--
-- Source: scripts/parse_telegram_imminent.py → imminent_alerts_from_telegram.json
-- 168 IMMINENT PATTERN messages total, 19 distinct patterns, 92 tickers.
--
-- Of our 30 open trades, 10 tickers have a recoverable IMMINENT alert:
--   Pattern exists in live DB (Falling Wedge Breakout + Trend Reclaim, id=537):
--     ABM, AFJK, INTC, SOFX   (+ ACHC, PFSI already linked)
--   Pattern missing from live DB — creating stubs first:
--     ACHR, TLS  -> demand_zone_reversal_long
--     AIXI, BULX -> ADX_Expansion_Breakout_25
--
-- The other 4 missing patterns (bullish_bear_trap_reclaim_support,
-- breakout_pullback_retest_hold_ema20, Range_breakout_multi_retest_confirmation,
-- BullFlag_PullbackBreakout) are also recreated for completeness even though
-- no currently-open position references them — the user may need them later.

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Create stubs for the 6 patterns that appeared in Telegram but were wiped.
--    origin = 'recovered_from_telegram_export' so the user can tell them apart
--    and decide whether to re-evolve full rules later.
-- -----------------------------------------------------------------------------

INSERT INTO scan_patterns (
    name, description, rules_json, origin, asset_class, timeframe,
    confidence, evidence_count, backtest_count, score_boost, min_base_score,
    active, generation, ticker_scope, trade_count, backtest_priority,
    created_at, updated_at, promotion_status, oos_validation_json,
    queue_tier, paper_book_json, lifecycle_stage, regime_affinity_json,
    user_id
)
SELECT
    name,
    description,
    '{}'::jsonb AS rules_json,
    'recovered_from_telegram_export' AS origin,
    asset_class,
    '1h' AS timeframe,
    0.5 AS confidence,
    0 AS evidence_count,
    0 AS backtest_count,
    0.0 AS score_boost,
    0.0 AS min_base_score,
    FALSE AS active,  -- inactive so scanners don't run on empty rules
    0 AS generation,
    'all' AS ticker_scope,
    0 AS trade_count,
    5 AS backtest_priority,
    NOW() AS created_at,
    NOW() AS updated_at,
    'legacy' AS promotion_status,
    '{}'::jsonb AS oos_validation_json,
    'full' AS queue_tier,
    '{}'::jsonb AS paper_book_json,
    'candidate' AS lifecycle_stage,
    '{}'::jsonb AS regime_affinity_json,
    NULL AS user_id
FROM (VALUES
    ('demand_zone_reversal_long',
     'Recovered from Telegram export (Apr 2026). Bullish reversal setup after a pullback into a demand zone, looking for momentum confirmation (RSI/price vs EMAs). Rules wiped — stub only.',
     'stock'),
    ('ADX_Expansion_Breakout_25',
     'Recovered from Telegram export (Apr 2026). Trend-strength breakout: enter when ADX confirms a strong trend and price breaks out above a nearby resistance level. Rules wiped — stub only.',
     'stock'),
    ('bullish_bear_trap_reclaim_support',
     'Recovered from Telegram export (Apr 2026). Price briefly breaks below a key support/resistance level (triggering shorts) but quickly reclaims it. Rules wiped — stub only.',
     'stock'),
    ('breakout_pullback_retest_hold_ema20',
     'Recovered from Telegram export (Apr 2026). Breakout followed by a pullback that holds EMA20 as support. Rules wiped — stub only.',
     'stock'),
    ('Range_breakout_multi_retest_confirmation',
     'Recovered from Telegram export (Apr 2026). Range breakout confirmed by multiple successful retests of the breakout level. Rules wiped — stub only.',
     'stock'),
    ('BullFlag_PullbackBreakout',
     'Recovered from Telegram export (Apr 2026). Continuation setup after a strong impulse move: price consolidates/pulls back, then breaks out with momentum and volume. Rules wiped — stub only.',
     'stock')
) AS src(name, description, asset_class)
WHERE NOT EXISTS (
    SELECT 1 FROM scan_patterns p WHERE p.name = src.name
);

-- -----------------------------------------------------------------------------
-- 2. Link open trades to their recovered pattern (based on latest Telegram
--    IMMINENT alert per ticker). Only update trades whose scan_pattern_id is
--    currently NULL — never overwrite an existing link.
-- -----------------------------------------------------------------------------

-- 2a. Falling Wedge Breakout + Trend Reclaim (already exists: id=537)
UPDATE trading_trades t
SET scan_pattern_id = (SELECT id FROM scan_patterns WHERE name = 'Falling Wedge Breakout + Trend Reclaim' LIMIT 1)
WHERE t.user_id = 13
  AND t.status = 'open'
  AND t.scan_pattern_id IS NULL
  AND t.ticker IN ('ABM', 'AFJK', 'INTC', 'SOFX');

-- 2b. demand_zone_reversal_long (newly created stub)
UPDATE trading_trades t
SET scan_pattern_id = (SELECT id FROM scan_patterns WHERE name = 'demand_zone_reversal_long' LIMIT 1)
WHERE t.user_id = 13
  AND t.status = 'open'
  AND t.scan_pattern_id IS NULL
  AND t.ticker IN ('ACHR', 'TLS');

-- 2c. ADX_Expansion_Breakout_25 (newly created stub)
UPDATE trading_trades t
SET scan_pattern_id = (SELECT id FROM scan_patterns WHERE name = 'ADX_Expansion_Breakout_25' LIMIT 1)
WHERE t.user_id = 13
  AND t.status = 'open'
  AND t.scan_pattern_id IS NULL
  AND t.ticker IN ('AIXI', 'BULX');

-- -----------------------------------------------------------------------------
-- 3. Verification
-- -----------------------------------------------------------------------------

SELECT 'PATTERN STUBS CREATED' AS section;
SELECT id, name, origin, active, lifecycle_stage
FROM scan_patterns
WHERE origin = 'recovered_from_telegram_export'
ORDER BY name;

SELECT 'OPEN TRADE → PATTERN LINKS (USER 13)' AS section;
SELECT
    t.ticker,
    t.scan_pattern_id,
    sp.name AS pattern_name,
    t.related_alert_id
FROM trading_trades t
LEFT JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
WHERE t.user_id = 13 AND t.status = 'open'
ORDER BY (t.scan_pattern_id IS NULL), t.ticker;

COMMIT;
