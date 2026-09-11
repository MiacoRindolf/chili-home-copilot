-- [58] accel-reversal near-high give-back band — verification queries (READ-ONLY).
--
-- The band that decides gate 3 of ``tape_accel_reversal_exit`` is DERIVED
-- (``paper_execution.ACCEL_REVERSAL_GIVEBACK_BAND_R`` = 0.393 R = the p90 of the give-back at
-- the real accel rollover while above entry, n = 27 rollovers / 78 live Alpaca legs / 14 d to
-- 2026-09-10; re-derived 2026-09-11 on 84 legs, n = 43, same p90) and REPORTED on every
-- ``live_tape_accel_reversal_exit`` receipt as ``giveback_r`` / ``giveback_band_r`` /
-- ``giveback_band_px`` / ``binding``, alongside the gate-1 binding (``arm_r`` / ``arm_frac`` /
-- ``reward_risk`` — 78 % of the rows are decided there), the print window that decided gate 2
-- (``tape_window_prints``) and the measured HWM-sampling gap (``hwm_sampling_gap_r``).
--
-- Run against the LIVE db, read-only:
--   docker exec -i chili-home-copilot-postgres-1 psql -U chili -d chili \
--     -f - < project_ws/AgentOps/timeshare/58_accel_reversal_giveback_band_verify.sql
--
-- Every statement is bounded by ``ts >= now() - interval '14 days'`` and touches only
-- ``trading_automation_events`` (never ``iqfeed_trade_ticks``). These queries VERIFY the
-- deployment and re-measure the refusals; they cannot re-derive the quantile, which is
-- defined on the tick tape. The DERIVATION itself is a committed, runnable script:
--   conda run -n chili-env python \n--     project_ws/AgentOps/timeshare/derive_giveback_band_58_helper_units.py --days 14
-- (read-only, symbol+time bounded per leg, detects the rollover with the SAME
-- ``entry_gates._signed_tape_features`` and the SAME print window the live gate reads).

SET statement_timeout='20s';

-- ── 1. DEPLOYMENT CHECK (the release gate for this change) ──────────────────────────────
-- THE GATE IS ``band_bound``, NOT ``key_present``. The helper initialises all of the [58]
-- keys to None and returns EARLY on not_long / no_tape / bad_input / non_finite /
-- non_positive_price / bad_risk_dist — BEFORE the band is resolved — so those rows emit the
-- key as JSON *null*, and `payload_json ? 'giveback_band_r'` is TRUE for a null
-- (`SELECT ('{"a": null}'::jsonb ? 'a')` -> t). 37 of the 446 receipts in the measured window
-- are `no_tape`: a night whose only watched names have no equity tape would light up
-- `key_present` while the band had never decided anything at all. The release gate is the
-- count of receipts where the band actually BOUND a decision — i.e. the reasons that are
-- resolved at or after gate 3.
-- Before the merge + lane restart: band_bound = 0. On the first live tick after it, band_bound
-- must be > 0 AND a_binding must be the derivation string (possibly suffixed with the one-tick
-- floor), never 'env override' on its own — an 'env override' means
-- CHILI_MOMENTUM_EXIT_ACCEL_REVERSAL_GIVEBACK_FRAC is set in the lane's environment and is
-- overriding the derived default: find it and remove it.
SELECT count(*) FILTER (
         WHERE payload_json->>'giveback_band_r' IS NOT NULL
           AND payload_json->>'reason' IN ('fired','gave_back_too_much','ratchet_no_raise')
       )                                                               AS band_bound,
       count(*) FILTER (WHERE payload_json->>'giveback_band_r' IS NOT NULL)
                                                                       AS band_resolved,
       count(*) FILTER (WHERE payload_json ? 'giveback_band_r')         AS key_present,
       count(*)                                                        AS total,
       count(DISTINCT payload_json->>'binding') FILTER (
         WHERE payload_json->>'binding' IS NOT NULL)                   AS distinct_bindings,
       min(payload_json->>'binding') FILTER (
         WHERE payload_json->>'binding' IS NOT NULL)                   AS a_binding,
       max((payload_json->>'giveback_band_r')::numeric)                AS max_band_seen,
       -- the gate-1 binding + the print window + the measured HWM sampling gap must also
       -- appear, or the receipt is still unreadable on the 78 % `below_arm` path.
       count(*) FILTER (WHERE payload_json->>'arm_r' IS NOT NULL)      AS with_arm_r,
       count(*) FILTER (WHERE payload_json->>'tape_window_prints' IS NOT NULL)
                                                                       AS with_window_prints,
       count(*) FILTER (WHERE payload_json->>'hwm_sampling_gap_r' IS NOT NULL)
                                                                       AS with_sampling_gap
FROM trading_automation_events
WHERE event_type='live_tape_accel_reversal_exit'
  AND ts >= now() - interval '14 days';
-- measured 2026-09-10 23:5xZ (pre-merge): band_bound 0 / band_resolved 0 / key_present 0 /
-- total 446 / with_arm_r 0 / with_window_prints 0 / with_sampling_gap 0.

-- ── 1b. THE MEASURED HWM-SAMPLING GAP (approximation (a), post-merge) ───────────────────
-- `high_water_mark` is a running max of the peak BID sampled once per runner tick; the
-- derivation's H is the high of the continuous print tape. A max over a sparse sample is <=
-- the max over the tape, one-directionally, so the live gate is LOOSER than the derived 89 %.
-- Once the lane restarts, this is the size of that gap, in R, instead of an argument.
SELECT count(*)                                                                 AS n,
       round(percentile_cont(0.5) WITHIN GROUP (
         ORDER BY (payload_json->>'hwm_sampling_gap_r')::numeric)::numeric, 4)   AS p50_gap_r,
       round(percentile_cont(0.9) WITHIN GROUP (
         ORDER BY (payload_json->>'hwm_sampling_gap_r')::numeric)::numeric, 4)   AS p90_gap_r,
       max((payload_json->>'hwm_sampling_gap_r')::numeric)                       AS max_gap_r
FROM trading_automation_events
WHERE event_type='live_tape_accel_reversal_exit'
  AND ts >= now() - interval '14 days'
  AND payload_json->>'hwm_sampling_gap_r' IS NOT NULL;
-- If p90_gap_r is a material fraction of 0.393 the band must be re-derived against the print
-- high (or the gate switched to it) — that is a new measurement, not a tweak.

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


-- ── 4. THE PENNY: what the band admits in TICKS, and what the one-tick floor changes ─────
-- Both ``hwm`` and ``bid`` are exchange-quantized, so the give-back is a whole number of
-- ticks and the effective band is floor(band*risk_dist / tick) ticks. This is why the receipt
-- reports ``giveback_band_px`` (the effective distance) and not only the R value.
WITH r AS (
  SELECT id, session_id, ts, payload_json->>'reason' AS reason,
         (payload_json->>'bid')::numeric              AS bid,
         (payload_json->>'high_water_mark')::numeric  AS hwm,
         (payload_json->>'peak_r')::numeric           AS peak_r
  FROM trading_automation_events
  WHERE event_type='live_tape_accel_reversal_exit'
    AND ts >= now() - interval '14 days'
    AND (payload_json->>'peak_r') IS NOT NULL
    AND (payload_json->>'peak_r')::numeric > 0
), e AS (
  SELECT r.id, max((f.payload_json->>'avg')::numeric) AS entry_max
  FROM r JOIN trading_automation_events f
    ON f.session_id=r.session_id AND f.event_type='live_entry_filled' AND f.ts<=r.ts
  GROUP BY r.id
), q AS (
  SELECT r.*, (r.hwm-e.entry_max)/r.peak_r AS risk_dist,
         CASE WHEN r.bid < 1.0 THEN 0.0001 ELSE 0.01 END AS tick
  FROM r JOIN e ON e.id=r.id
)
SELECT count(*)                                                                AS n,
       count(*) FILTER (WHERE floor(0.393*risk_dist/tick)=floor(0.35*risk_dist/tick))
                                                                               AS same_ticks_as_035,
       count(*) FILTER (WHERE 0.393*risk_dist < tick)                          AS sub_tick_band,
       count(*) FILTER (WHERE 0.393*risk_dist < tick AND (hwm-bid) > 0 AND (hwm-bid) <= tick)
                                                                               AS floor_opens,
       count(*) FILTER (WHERE 0.393*risk_dist < tick AND (hwm-bid) > 0 AND (hwm-bid) <= tick
                          AND reason='gave_back_too_much')                     AS floor_flips_a_refusal,
       round(min(0.393*risk_dist/tick),3)                                      AS min_ticks
FROM q WHERE risk_dist > 0;
-- measured 2026-09-11: n 369 - same_ticks_as_035 150 (41%) - sub_tick_band 42 (11%) -
-- floor_opens 10 - floor_flips_a_refusal 2 (session 20774, 09-09 09:32:05 + 09:32:17) -
-- min_ticks 0.663. The other 8 sub-tick ticks are already refused upstream (below_arm /
-- still_accelerating), so the one-tick floor changes exactly TWO decisions in 14 days.

-- ── 5. THE CLIFF -> RAMP: does the conditioned cushion change any live FIRE? ─────────────
-- Gate 3 used to refuse hard and, inside the band, always lock at base_lock_bps off the bid
-- regardless of how far inside the band the tick sat. The cushion now ramps from the base at
-- the high to the full band at the edge. A fire only survives if the candidate still RAISES
-- the stop (Invariant A), so this asks: which of the live fires would stop firing?
WITH r AS (
  SELECT id, session_id, ts,
         (payload_json->>'bid')::numeric                        AS bid,
         (payload_json->>'high_water_mark')::numeric            AS hwm,
         (payload_json->>'peak_r')::numeric                     AS peak_r,
         (payload_json->>'counterfactual_fixed_stop')::numeric  AS cur_stop
  FROM trading_automation_events
  WHERE event_type='live_tape_accel_reversal_exit'
    AND ts >= now() - interval '14 days'
    AND (payload_json->>'fired')='true'
), e AS (
  SELECT r.id, max((f.payload_json->>'avg')::numeric) AS entry_max
  FROM r JOIN trading_automation_events f
    ON f.session_id=r.session_id AND f.event_type='live_entry_filled' AND f.ts<=r.ts
  GROUP BY r.id
), q AS (
  SELECT r.*, (r.hwm-e.entry_max)/r.peak_r AS risk_dist FROM r JOIN e ON e.id=r.id
), c AS (
  SELECT q.*, greatest(0.393*risk_dist, CASE WHEN bid<1.0 THEN 0.0001 ELSE 0.01 END) AS band_px,
         bid*0.012 AS base_cushion, (hwm-bid) AS gb_px
  FROM q WHERE risk_dist > 0
)
SELECT session_id, ts::timestamp(0) AS ts, bid, hwm, round(gb_px,4) AS gb_px,
       round(gb_px/nullif(band_px,0),4)                                    AS inside_band_frac,
       round(bid-base_cushion,4)                                           AS stop_before,
       round(bid - (base_cushion + least(1.0,greatest(0.0,gb_px/nullif(band_px,0)))
                    * greatest(0.0, band_px - base_cushion)),4)            AS stop_after,
       CASE WHEN (bid - (base_cushion + least(1.0,greatest(0.0,gb_px/nullif(band_px,0)))
                    * greatest(0.0, band_px - base_cushion))) > cur_stop
            THEN 'still fires' ELSE 'becomes ratchet_no_raise' END          AS verdict
FROM c ORDER BY ts;
-- measured 2026-09-11: 15 rows, ALL 'still fires' and stop_after == stop_before to the cent —
-- every live fire in the window sat at gb_px = 0.0000 (bid == hwm), i.e. inside_band_frac = 0,
-- where the ramp IS the base cushion. The conditioning costs nothing on the measured window;
-- it only removes the one-tick cliff at the far edge.
