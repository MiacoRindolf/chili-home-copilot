# CC_REPORT: f8b-verification-soak-2

## Verdict — STILL INCONCLUSIVE: 28 min vs 24h target

**Run timing:** Operator invoked at 2026-05-03 16:57 UTC, **~28 min after F8b deploy** (16:29:20 UTC). The brief's target was 2026-05-04 16:30 UTC — **~23.5h short**. The 18-minute gap from the prior `f8b-verification-soak` run (16:38 UTC) is too small to produce new closed exits given the 47-min average pullback hold time.

Per the brief's pre-window provision: bumped per-ticker minimum to n=30. Both BTC and SOL post-deploy n is 0. Decision tree fires "insufficient — recommend f8b-verification-soak-3" branch for both tickers.

**No allowlist/strategy change recommended in this run.**

## Pinned facts

| Item | Value |
|---|---|
| F8b deploy (effective) | 2026-05-03 **16:29:20 UTC** |
| Prior soak run | 2026-05-03 16:38 UTC (10 min after deploy) |
| Current run | 2026-05-03 **16:57:19 UTC** (~28 min after deploy) |
| Target re-run time | 2026-05-04 16:30 UTC (~23.5h from now) |
| Pre-window provision | per-ticker minimum n=30 |
| Avg pullback hold | ~47 minutes |

## What changed since the prior soak run

| Metric | f8b-verification-soak (16:38) | This run (16:57) | Δ |
|---|---|---|---|
| Post-deploy distinct exits | 0 | **0** | unchanged |
| Open paper_fills (post-deploy cohort) | 14 (JOIN-inflated count was wrong) → **2 actual** (1 BTC, 1 SOL) | **2** (still open) | unchanged |
| Cumulative distinct exits (full history) | 49 | 49 | unchanged |
| Verdict-grade decay cells (n≥30) | 6 | **6** | unchanged |
| Allowlist false rejects | 0 | **0** | unchanged |
| `db_errors` | 0 | **0** | unchanged |
| Validation cells with val_n>0 | 13 | 13 | unchanged |

**Nothing strategically new in 18 minutes.** The verdict is structurally identical.

### Important corrections to the prior run's count of catchup-batch fills

The prior soak report said "14 catchup paper_fills (8 BTC + 6 SOL) from the snapshot-replay catchup at 16:29:33 are still open." That count came from a JOIN-inflated query. The actual post-deploy paper_fill count is **2** (1 BTC, 1 SOL). The "8 BTC + 6 SOL" was the same JOIN-cardinality bug the runbook documents.

Verified via `SELECT ... LEFT JOIN fast_exits x ON x.entry_execution_id = e.id WHERE x.id IS NULL` (canonical-id lookup):

```
ticker   | id    | decided_at                 | era         | open_for
SOL-USD  | 10353 | 2026-05-03 16:29:34.638016 | POST-deploy | 00:28:29
BTC-USD  | 10336 | 2026-05-03 16:29:33.017474 | POST-deploy | 00:28:31
SOL-USD  | 10216 | 2026-05-03 16:08:57.571209 | pre-deploy  | 00:49:06
ETH-USD  | 10207 | 2026-05-03 16:08:56.107146 | pre-deploy  | 00:49:08
BTC-USD  | 10198 | 2026-05-03 16:08:55.622580 | pre-deploy  | 00:49:08
BTC-USD  | 9409  | 2026-05-03 13:21:54.543378 | pre-deploy  | 03:36:09
```

**6 total open paper_fills, 2 of which are post-deploy.** The pre-deploy ETH paper_fill (id=10207) is interesting: F8b's allowlist blocks NEW ETH but doesn't kill existing positions. That's correct behavior — the gate filters at decision time, not retroactively.

## Cluster-correlation analysis

Per the brief: if catchup-batch fills all close in the same direction, treat aggregate as ONE data point.

The catchup batch is 2 fills (BTC + SOL), not 14. The cluster correlation concern is much smaller than the brief anticipated — but until they close I can't compute the correlation directly. **When they close in the next ~20 min, the report's interpretation should still treat them as the cluster they are** (entered at near-identical timestamps from the same snapshot-replay event).

## Three-eval comparison table

| Metric | F8a-eval-rerun-2 (15:35) | F8b counterfactual | f8b-verification-soak (16:38) | f8b-verification-soak-2 (16:57) |
|---|---|---|---|---|
| Cumulative distinct exits | 43 | (n/a) | 49 | 49 |
| BTC avg_ret_bps | +5.66 (n=8) | **−0.75** at d=5s | +3.65 (n=9) | **+3.65 (n=9)** unchanged |
| SOL avg_ret_bps | +3.34 (n=13) | +3.47 at d=25s | +1.58 (n=14) | **+1.58 (n=14)** unchanged |
| Post-deploy distinct exits | n/a | n/a | 0 | **0** |
| Verdict-grade decay cells | 0 | n/a | 6 (first crossings) | 6 (4 at h≥5s) |
| Allowlist false rejects | n/a | n/a | 0 | 0 |

## Verdict-grade decay cells — newly relevant detail

```
ticker   | bucket | horizon_s | n  | mean_bps | stderr_bps | mean ± 2σ
BTC-USD  | high   |        1  | 32 |    -0.02 |       0.05 | (-0.12, +0.08)  spans zero
BTC-USD  | high   |        5  | 31 |    -0.11 |       0.09 | (-0.29, +0.07)  spans zero
DOGE-USD | high   |        1  | 36 |    -0.68 |       0.11 | (-0.90, -0.46)  ENTIRELY NEGATIVE
DOGE-USD | high   |        5  | 32 |    -1.04 |       0.21 | (-1.46, -0.62)  ENTIRELY NEGATIVE
ETH-USD  | high   |        1  | 30 |    +0.29 |       0.12 | (+0.05, +0.53)  ENTIRELY POSITIVE
ETH-USD  | high   |        5  | 33 |    -0.13 |       0.21 | (-0.55, +0.29)  spans zero
```

**DOGE high h=5 is now verdict-grade AND statistically-negative.** The F6.5 negative_edge gate is firing on DOGE high pullback alerts (visible in the post-deploy reject distribution: `DOGE-USD | negative_edge:negative_edge | 3`). **The brain self-pruned without operator action.**

**ETH high h=1** is verdict-grade-positive (mean=+0.29, mean−2σ=+0.05) — strange given ETH realized P/L is −7.28 bps. Most likely: h=1 is the fire moment so the +0.29 reflects micro-tick-level noise, not strategic edge. The brief's convention rules h=1 not falsifying.

**No verdict-grade cell at horizons ≥ 5s has both BTC or SOL specifically.** BTC h=5 spans zero (n=31, ±0.18 bps); SOL hasn't crossed n=30 at h=5 yet. F6.5's calibrated gates therefore haven't started auto-using BTC/SOL signal data.

## Validation residuals — unchanged since prior run

13 cells with val_n>0; same as prior soak. No new validations have landed in the 18-min window (avg validations rate is ~10/hour). DOGE post-fix-only cells (high h=1800, h=3600) still showing residuals 5.66-6.72 bps (vs pre-fix 34-40 bps). The C-fix-vindication is durable but not adding new evidence.

## SOL pre-F8b vs post-F8b

```
WITH pullback_eids AS (
  SELECT e.id, e.decided_at FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long' AND e.ticker='SOL-USD'
)
SELECT
  CASE WHEN p.decided_at < '2026-05-03 16:29:20' THEN 'pre-F8b (30s)' ELSE 'post-F8b (25s)' END AS era,
  COUNT(DISTINCT p.id) AS exits, ...
FROM fast_exits x JOIN pullback_eids p ON p.id = x.entry_execution_id
GROUP BY era;
```

Pre-F8b (30s): n=14, +1.58 bps avg, 35.7% wins.
Post-F8b (25s): **n=0** — insufficient.

The 25s vs 30s comparison can't fire until SOL post-deploy exits accumulate.

## Allowlist gate efficacy

```
ticker   | reject_reason                                       | n
AVAX-USD | pullback_ticker:pullback_ticker_not_allowed:AVAX-USD| 6
AVAX-USD | min_score:score_below_threshold                     | 1
BTC-USD  | capacity:pair_already_held                          | 5
BTC-USD  | min_score:score_below_threshold                     | 2
DOGE-USD | negative_edge:negative_edge                         | 3
DOGE-USD | min_score:score_below_threshold                     | 1
DOGE-USD | pullback_ticker:pullback_ticker_not_allowed:DOGE-USD| 1
ETH-USD  | pullback_ticker:pullback_ticker_not_allowed:ETH-USD | 8
SOL-USD  | capacity:pair_already_held                          | 4
SOL-USD  | min_score:score_below_threshold                     | 1
```

Allowlist false rejects: **0**. Working as designed.

DOGE high alerts now triggering F6.5's `negative_edge` gate (3 of the 5 DOGE rejects); the allowlist gate is no longer the primary filter for DOGE high — calibration is. Architectural validation.

## Decision-tree outcome

| Ticker | Post-deploy n | Branch |
|---|---|---|
| BTC-USD | 0 | "insufficient — recommend f8b-verification-soak-3" |
| SOL-USD | 0 | "insufficient — recommend f8b-verification-soak-3" |

Combined: same as prior run — **inconclusive — more soak**.

## Surprises / deviations

1. **The operator ran this task 23.5h early** (relative to the brief's 2026-05-04 16:30 UTC target). The brief explicitly accommodates this case via the pre-window provision. The 18-minute gap from the prior run is too narrow to produce new closed exits.

2. **The prior soak's "14 catchup paper_fills" count was JOIN-inflated.** Actual count: 2 (1 BTC + 1 SOL). The JOIN-on-`fast_alerts` pattern (instead of `IN (SELECT id ...)`) bit again. Documented for future runs; the runbook (`docs/RUNBOOKS/fast_alerts-microsecond-dup.md`) covers this. **The cluster-correlation concern is much smaller than anticipated** — n=2 is barely a "cluster."

3. **DOGE high h=5 is verdict-grade-negative** (mean=−1.04, mean+2σ=−0.62, n=32). Combined with the already-verdict-grade DOGE high h=1 cell, the F6.5 negative_edge gate now has structural backing on TWO horizons for DOGE. **The brain has effectively pre-empted the allowlist for DOGE high pullback** — even if the allowlist were removed, the calibrated gate would still block DOGE high. The allowlist remains useful as a backstop for med/low buckets that haven't crossed n=30 yet.

4. **ETH high h=1 verdict-grade-positive (+0.29 bps)** is a curiosity. Either h=1 isn't strategically meaningful (per the brief's convention) and this is microstructure noise, or there's something at h=1 specifically that doesn't transmit to longer horizons. Not actionable today; flag for any future investigation if it persists.

5. **18-min gap = 0 new strategic info.** Same verdict as the prior run with one extra catchup paper_fill no longer being JOIN-inflated. The honest report-write here is "essentially nothing changed; come back at the briefed target."

## Open questions for Cowork

1. **Why was this re-run scheduled now?** The brief's target was 24h post-deploy (2026-05-04 16:30 UTC); both the prior run and this one fired ~23.5-24h early. If the operator intends faster cadence, the brief's pre-window provision (n≥30) should be relaxed to n≥10 for "directional reading" runs, OR the operator should wait the briefed window. As is, two consecutive "inconclusive" reports communicate the same thing.

2. **The 2 catchup post-deploy paper_fills** will close in the next ~15-30 min (around 17:15 UTC). They'll produce 2 distinct exits — still well under n=30. A run at ~17:30 UTC could capture them but n=2 is still inconclusive.

3. **Realistic next checkpoint:** Catchup paper_fills will close in ~15-30 min, producing 2 exits. Organic post-deploy alerts since the deploy: I observed earlier that the BTC capacity gate fires (capacity:pair_already_held = 5 since deploy), meaning organic BTC pullback alerts ARE arriving but they're being blocked by the existing open BTC fill. Once that BTC fill closes, the next BTC alert can fill → another paper_fill → another close ~47 min later. The system's natural cadence at the calibrated 5s delay produces ~2-3 BTC distinct exits per hour. Reaching n=20 requires ~7-10 hours minimum.

4. **DOGE high h=5 verdict-grade-negative** confirms that the calibration framework works. This is positive infrastructure news: as data accumulates on negative-edge signals, the brain auto-blocks them without operator action.

## Recommendation for next NEXT_TASK

**Path A — wait for the briefed 24h soak target.**

One-line: *Re-execute `f8b-verification-soak-3` at 2026-05-04 16:30 UTC with 24h+ post-deploy data; apply the decision tree at n≥20 per ticker.*

This is the only defensible recommendation. The current data is structurally identical to the prior run; firing a third "inconclusive" report 18 min later wouldn't change anything. The 2 catchup paper_fills will close around 17:15 UTC, organic alert+fill+exit cycles will accumulate over the next 24h, and at the briefed target time we'll have ~15-25 BTC and ~12-18 SOL post-deploy distinct exits to actually run the decision tree on.

**Path B — intermediate run at ~22:00 UTC tonight (5h post-deploy)**

If the operator wants directional reading sooner. By then:
- BTC post-deploy n likely 6-10 distinct exits.
- SOL post-deploy n likely 4-7 distinct exits.

Still sub-threshold (n<20) per the briefed minimum, but enough to see direction. Inferior to waiting until 16:30 UTC tomorrow.

## Verbatim verification SQL — for next review

```sql
-- 1. Distinct realized P/L on post-deploy cohort (use IN-subquery, NOT JOIN)
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND e.decided_at > '2026-05-03 16:29:20'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0) AS wins,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / NULLIF(COUNT(*),0))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
       ROUND(AVG(x.holding_period_s)::numeric, 0) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;

-- 2. Open paper_fills with era classification
SELECT e.ticker, e.id, e.decided_at,
       CASE WHEN e.decided_at > '2026-05-03 16:29:20' THEN 'POST-deploy' ELSE 'pre-deploy' END AS era,
       (NOW() - e.decided_at) AS open_for
FROM fast_executions e
LEFT JOIN fast_exits x ON x.entry_execution_id = e.id
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decision='paper_fill' AND x.id IS NULL
ORDER BY e.decided_at DESC;

-- 3. Allowlist false-reject canary
SELECT COUNT(*) AS false_rejects
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.ticker IN ('BTC-USD', 'SOL-USD')
  AND e.reject_reason LIKE 'pullback_ticker%';
-- Expected: 0.

-- 4. Verdict-grade decay cells with statistical bounds
SELECT ticker, score_bucket, horizon_s, sample_count,
       ROUND(mean_return::numeric * 10000, 2) AS mean_bps,
       ROUND((CASE WHEN sample_count > 1
                   THEN SQRT(m2_return/(sample_count-1))/SQRT(sample_count)
                   ELSE NULL END)::numeric * 10000, 2) AS stderr_bps,
       ROUND((mean_return - 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS lower_2sigma_bps,
       ROUND((mean_return + 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS upper_2sigma_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long' AND sample_count >= 30
ORDER BY ticker, horizon_s;
```

## What's next

**Recommended: f8b-verification-soak-3 at 2026-05-04 16:30 UTC** (full 24h soak, briefed target).

F8a soak continues uninterrupted on `fast-data-worker`. `models/trading.py` and `.env.example` remain untouched.
