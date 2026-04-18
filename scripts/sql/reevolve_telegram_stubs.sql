-- Re-evolve the 6 pattern stubs recovered from Telegram export by filling
-- their empty rules_json with plausible starter conditions derived from
-- their name + Telegram description + canonical TA conventions.
--
-- Safety: stubs stay active=false, lifecycle_stage='candidate', and get
-- high backtest_priority so the brain picks them up on the next cycle.
-- mining_validation.py's ensemble promotion gate will flip them to
-- active=true only if they demonstrate real edge on OOS data; otherwise
-- they'll be retired. This avoids generating noisy signals from unvalidated
-- guessed rules.
--
-- Source for each rule set is embedded as a comment on the UPDATE.

BEGIN;

-- 1. demand_zone_reversal_long
--    "Bullish reversal after a pullback into a demand zone, looking for
--     momentum confirmation (RSI/price vs EMAs)."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "price", "op": ">", "ref": "ema_20"},
        {"indicator": "rsi_14", "op": "between", "value": [35, 60]},
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "adx", "op": ">=", "value": 18},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.1}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Bullish reversal after a pullback into a demand zone: price reclaims EMA20, RSI recovering from oversold territory, MACD histogram flipping positive, ADX confirming trend, mild volume expansion. Rules re-derived from name + TA convention after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'demand_zone_reversal_long' AND origin = 'recovered_from_telegram_export';

-- 2. ADX_Expansion_Breakout_25
--    "ADX confirms a strong trend and price breaks out above resistance."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "adx", "op": ">=", "value": 25},
        {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 1.0},
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "price", "op": ">", "ref": "ema_20"},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.2}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Trend-strength breakout: ADX >= 25 confirms strong trend, price breaking above nearby resistance with MACD histogram positive and mild volume expansion. Rules re-derived from name (ADX_25 explicit) after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'ADX_Expansion_Breakout_25' AND origin = 'recovered_from_telegram_export';

-- 3. bullish_bear_trap_reclaim_support
--    "Price briefly breaks below key support but quickly reclaims it."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "price", "op": ">", "ref": "ema_20"},
        {"indicator": "stochastic_k", "op": "between", "value": [20, 80]},
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "rsi_14", "op": "between", "value": [35, 65]},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.1}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Bullish bear-trap reclaim: price reclaims EMA20 (support regained) with stochastic recovering from oversold, MACD histogram positive, RSI in neutral-bullish band. Rules re-derived from name + TA convention after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'bullish_bear_trap_reclaim_support' AND origin = 'recovered_from_telegram_export';

-- 4. breakout_pullback_retest_hold_ema20
--    "Breakout followed by pullback that holds EMA20 as support."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "price", "op": ">", "ref": "ema_20"},
        {"indicator": "dist_from_ema_20_pct", "op": "<=", "value": 1.5},
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "adx", "op": ">=", "value": 20},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.0}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Breakout-pullback-retest: price above EMA20 but close to it (healthy pullback that held support), MACD histogram positive, ADX confirming trend continuation. Rules re-derived from name after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'breakout_pullback_retest_hold_ema20' AND origin = 'recovered_from_telegram_export';

-- 5. Range_breakout_multi_retest_confirmation
--    "Range breakout confirmed by multiple successful retests."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 0.5},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.5},
        {"indicator": "adx", "op": ">=", "value": 18},
        {"indicator": "vcp_count", "op": ">=", "value": 2},
        {"indicator": "price", "op": ">", "ref": "ema_20"}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Range breakout with multi-retest confirmation: price at/above resistance with elevated volume, ADX confirming trend, VCP count >= 2 indicating multiple consolidations / retests. Rules re-derived from name after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'Range_breakout_multi_retest_confirmation' AND origin = 'recovered_from_telegram_export';

-- 6. BullFlag_PullbackBreakout
--    "Continuation setup after strong impulse: pullback then breakout."
UPDATE scan_patterns SET
    rules_json = '{"conditions": [
        {"indicator": "price", "op": ">", "ref": "ema_20"},
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "volume_ratio", "op": ">=", "value": 1.3},
        {"indicator": "adx", "op": ">=", "value": 22},
        {"indicator": "rsi_14", "op": "between", "value": [45, 75]}
    ]}'::jsonb,
    backtest_priority = 1,
    description = 'Recovered from Telegram (Apr 2026). Bull-flag pullback breakout: continuation after strong impulse + consolidation — price above EMA20, MACD histogram positive, volume expansion, ADX confirming trend, RSI in bullish-momentum band. Rules re-derived from name after scan_patterns wipe; subject to brain validation before activation.',
    updated_at = NOW()
WHERE name = 'BullFlag_PullbackBreakout' AND origin = 'recovered_from_telegram_export';

-- Verification
SELECT id, name, active, lifecycle_stage, backtest_priority,
       jsonb_array_length(rules_json->'conditions') AS n_conditions
FROM scan_patterns
WHERE origin = 'recovered_from_telegram_export'
ORDER BY name;

COMMIT;
