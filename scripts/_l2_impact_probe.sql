-- L2 impact probe for 2026-06-11 (read-only)
-- 1. Of today's equity viability evals, how many had a FRESH (<=15s) L2 row available at eval time?
WITH evals AS (
  SELECT id, symbol, updated_at, viability_score, live_eligible,
         (explain_json::text ILIKE '%imbalance against long%') AS warned
  FROM momentum_symbol_viability
  WHERE updated_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD'
), cov AS (
  SELECT e.*, EXISTS (
    SELECT 1 FROM iqfeed_depth_snapshots s
    WHERE s.symbol = e.symbol
      AND s.observed_at BETWEEN e.updated_at - interval '15 seconds' AND e.updated_at
  ) AS l2_fresh
  FROM evals e
)
SELECT 'evals_total' AS k, count(*)::text AS v FROM cov
UNION ALL SELECT 'evals_with_fresh_L2', count(*)::text FROM cov WHERE l2_fresh
UNION ALL SELECT 'evals_warned_total', count(*)::text FROM cov WHERE warned
UNION ALL SELECT 'evals_warned_with_L2', count(*)::text FROM cov WHERE warned AND l2_fresh
UNION ALL SELECT 'warned_live_eligible_anyway', count(*)::text FROM cov WHERE warned AND live_eligible
UNION ALL SELECT 'warned_not_eligible', count(*)::text FROM cov WHERE warned AND NOT live_eligible;

-- 2. Today's L2 snapshot threshold crossings (what the scorer would see)
SELECT 'snap_boost_gt0.12' AS k, count(*)::text FROM iqfeed_depth_snapshots
 WHERE observed_at >= '2026-06-11'::date AND imbalance5 > 0.12
UNION ALL SELECT 'snap_penalty_lt-0.18', count(*)::text FROM iqfeed_depth_snapshots
 WHERE observed_at >= '2026-06-11'::date AND imbalance5 < -0.18
UNION ALL SELECT 'snap_neutral', count(*)::text FROM iqfeed_depth_snapshots
 WHERE observed_at >= '2026-06-11'::date AND imbalance5 BETWEEN -0.18 AND 0.12;

-- 3. Today's momentum automation decisions/entries (equity)
SELECT 'outcomes_today_equity' AS k, count(*)::text FROM momentum_automation_outcomes
 WHERE created_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD';
