-- [58] accel-reversal near-high give-back band — verification queries (READ-ONLY).
--
-- The band that decides gate 3 of ``tape_accel_reversal_exit`` is DERIVED
-- (``paper_execution.ACCEL_REVERSAL_GIVEBACK_BAND_R`` = 0.393 R = the p90 of the give-back at
-- the real accel rollover while above entry, n = 27 rollovers / 78 live Alpaca legs / 14 d to
-- 2026-09-10) and REPORTED on every ``live_tape_accel_reversal_exit`` receipt as
-- ``giveback_r`` / ``giveback_band_r`` / ``binding``.
--
-- Run against the LIVE db, read-only:
--   docker exec -i chili-home-copilot-postgres-1 psql -U chili -d chili \
--     -f - < project_ws/AgentOps/timeshare/58_accel_reversal_giveback_band_verify.sql
--
-- Every statement is bounded by ``ts >= now() - interval '14 days'`` and touches only
-- ``trading_automation_events`` (never ``iqfeed_trade_ticks``).

SET statement_timeout='20s';

-- ── 1. DEPLOYMENT CHECK (the release gate for this change) ──────────────────────────────
-- Before the merge + lane restart: with_band = 0. On the FIRST live tick after it, with_band
-- must be > 0 and the binding must be the derivation string, never 'env override' (an
-- 'env override' here means CHILI_MOMENTUM_EXIT_ACCEL_REVERSAL_GIVEBACK_FRAC is set in the
-- lane's environment and is overriding the derived default — find it and remove it).
SELECT count(*) FILTER (WHERE payload_json ? 'giveback_band_r')        AS with_band,
       count(*)                                                        AS total,
       count(DISTINCT payload_json->>'binding') FILTER (WHERE payload_json ? 'binding')
                                                                       AS distinct_bindings,
       min(payload_json->>'binding')                                   AS a_binding,
       max((payload_json->>'giveback_band_r')::numeric)                AS max_band_seen
FROM trading_automation_events
WHERE event_type='live_tape_accel_reversal_exit'
  AND ts >= now() - interval '14 days';
-- measured 2026-09-10 23:5xZ (pre-merge): with_band 0 / total 446.

-- ── 2. FIRE CENSUS (what the gate actually does) ────────────────────────────────────────
SELECT coalesce(payload_json->>'reason',
                CASE WHEN (payload_json->>'fired')='true' THEN 'fired' END,
                'other')                       AS outcome,
       count(*)                                AS receipts,
       count(DISTINCT session_id)              AS sessions
FROM trading_automation_events
WHERE event_type='live_tape_accel_reversal_exit'
  AND ts >= now() - interval '14 days'
GROUP BY 1
ORDER BY 2 DESC;
-- measured 2026-09-10 23:5xZ: below_arm 348/23 · still_accelerating 40/5 · no_tape 37/2 ·
-- fired 15/7 · gave_back_too_much 5/4 · ratchet_no_raise 1/1  (= 446).

-- ── 3. DOES THE WIDENING 0.35 → 0.393 FLIP ANY LIVE REFUSAL? ────────────────────────────
-- risk_dist is recovered from the receipt itself (peak_r = (hwm − entry)/risk_dist), and the
-- position avg ``entry`` is BRACKETED by the entry fills that preceded the tick, so the
-- give-back is reported as a range — no blend assumption is needed. Once the receipts carry
-- ``giveback_r`` this reconstruction is obsolete: read the key directly.
WITH r AS (
  SELECT id, session_id, ts,
         (payload_json->>'bid')::numeric              AS bid,
         (payload_json->>'high_water_mark')::numeric  AS hwm,
         (payload_json->>'peak_r')::numeric           AS peak_r
  FROM trading_automation_events
  WHERE event_type='live_tape_accel_reversal_exit'
    AND ts >= now() - interval '14 days'
    AND payload_json->>'reason'='gave_back_too_much'
), e AS (
  SELECT r.id,
         min((f.payload_json->>'avg')::numeric) AS entry_min,
         max((f.payload_json->>'avg')::numeric) AS entry_max
  FROM r
  JOIN trading_automation_events f
    ON f.session_id = r.session_id
   AND f.event_type = 'live_entry_filled'
   AND f.ts <= r.ts
  GROUP BY r.id
)
SELECT r.session_id, r.ts::timestamp(0) AS ts, r.bid, r.hwm, r.peak_r,
       e.entry_min, e.entry_max,
       round((r.hwm-r.bid)/((r.hwm-e.entry_min)/r.peak_r), 4) AS giveback_r_low,
       round((r.hwm-r.bid)/((r.hwm-e.entry_max)/r.peak_r), 4) AS giveback_r_high,
       CASE WHEN (r.hwm-r.bid)/((r.hwm-e.entry_min)/r.peak_r) > 0.393
            THEN 'still refused at 0.393' ELSE 'WOULD FLIP' END   AS verdict_new_band,
       CASE WHEN (r.hwm-r.bid)/((r.hwm-e.entry_min)/r.peak_r) > 0.35
            THEN 'refused at 0.35' ELSE 'fired at 0.35' END       AS verdict_old_band
FROM r JOIN e ON e.id = r.id
ORDER BY r.ts;
-- measured 2026-09-10 23:5xZ: 5 rows, giveback_r_low 0.3987 / 0.3980 / 0.3980 / 1.2080 /
-- 0.7889 — ALL 'still refused at 0.393'. The widening changes no live decision in the window;
-- it only opens the 0.35–0.393 R sliver, which no live refusal occupied (tightest margin
-- 0.005 R, session 20774 on 2026-09-09).
