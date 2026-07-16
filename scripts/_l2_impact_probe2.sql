-- clock sanity: are both tables on UTC-naive timestamps?
SELECT 'now_utc' AS k, to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS') AS v
UNION ALL SELECT 'max_viability_updated_at', to_char(max(updated_at), 'YYYY-MM-DD HH24:MI:SS') FROM momentum_symbol_viability
UNION ALL SELECT 'max_snapshot_observed_at', to_char(max(observed_at), 'YYYY-MM-DD HH24:MI:SS') FROM iqfeed_depth_snapshots;

-- symbol overlap: eval symbols vs bridge-tracked symbols today
WITH es AS (
  SELECT DISTINCT symbol FROM momentum_symbol_viability
  WHERE updated_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD'
), ss AS (
  SELECT DISTINCT symbol FROM iqfeed_depth_snapshots WHERE observed_at >= '2026-06-11'::date
)
SELECT 'eval_symbols' AS k, count(*)::text FROM es
UNION ALL SELECT 'bridge_symbols', count(*)::text FROM ss
UNION ALL SELECT 'overlap', count(*)::text FROM es JOIN ss USING (symbol);

-- today's equity outcomes: mode x terminal_state
SELECT 'outcome ' || mode || ' / ' || coalesce(terminal_state,'?') || ' / ' || coalesce(outcome_class,'?') AS k,
       count(*)::text || ' (pnl ' || coalesce(round(sum(realized_pnl_usd)::numeric,2)::text,'-') || ')' AS v
FROM momentum_automation_outcomes
WHERE created_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD'
GROUP BY 1 ORDER BY 1;

-- did decision-moment snapshots carry L2 / book_imbalance?
SELECT 'readiness_has_book_imbalance' AS k, count(*)::text AS v
FROM momentum_automation_outcomes
WHERE created_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD'
  AND readiness_snapshot_json::text LIKE '%book_imbalance%'
UNION ALL
SELECT 'readiness_has_iqfeed_or_l2', count(*)::text
FROM momentum_automation_outcomes
WHERE created_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD'
  AND (readiness_snapshot_json::text ILIKE '%iqfeed%' OR readiness_snapshot_json::text ILIKE '%depth%'
       OR admission_snapshot_json::text ILIKE '%iqfeed%' OR admission_snapshot_json::text ILIKE '%depth%');

-- sample one outcome's readiness snapshot keys
SELECT 'sample_readiness_keys' AS k, (SELECT string_agg(key, ',') FROM jsonb_object_keys(readiness_snapshot_json) AS key) AS v
FROM momentum_automation_outcomes
WHERE created_at >= '2026-06-11'::date AND symbol NOT LIKE '%-USD' AND readiness_snapshot_json IS NOT NULL
ORDER BY created_at DESC LIMIT 1;
