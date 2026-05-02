# CC_REPORT: f8a-evaluation

## Verdict

**Insufficient data. Recommend continuing soak. Re-run this same task at ~2026-05-03 17:00 UTC (24h from now).**

The decision tree's first three branches require ≥3 verdict-grade cells (sample_count ≥ 30). Current state: **0 verdict-grade cells, 2 suggestive cells (n=10), 81 sparse cells (n<10).** Falls cleanly into the fourth branch ("< 3 verdict-grade cells → continuing soak").

The fade hypothesis is **neither supported nor refuted yet**.

## What was analysed

- **Window:** 2026-05-02 04:17:48 UTC (first post-fix `volume_breakout_pullback_long` alert) to 2026-05-02 17:36:30 UTC (most recent). Span = 13.3 hours.
- **Source rows (post-fix only, `id > 2300`):** 114 `fast_alerts` rows, 100% with `best_bid` AND `close` populated (F8a-fix's data-quality verification still holding).
- **Pre-fix catchup rows (`id <= 2300`):** 37 alerts, only 2 with `best_bid` — those 2 are the only pre-fix entries that fed the decay miner. Pre-fix data is effectively excluded from current `fast_signal_decay` rows by virtue of the miner dropping malformed-features observations at intake. *Not polluting the analysis.*
- **`fast_signal_decay` cells for `volume_breakout_pullback_long`:** 83 cells (across 5 tickers × 3 score buckets × up to 8 horizons), 208 total observations.

## Sample-count tier distribution

| Tier | Cells | Total obs | n range |
|---|---|---|---|
| **Verdict-grade** (n ≥ 30) | **0** | 0 | — |
| **Suggestive** (10 ≤ n < 30) | 2 | 20 | 10 |
| **Sparse** (n < 10) | 81 | 188 | 1–9 |

**No cell has crossed MIN_SAMPLES.** The two suggestive cells are both DOGE-USD horizon=1 (low and high score buckets), each with exactly n=10.

## Suggestive cells (per the brief — flagged "below MIN_SAMPLES; not statistically actionable")

| ticker | bucket | horizon_s | n | mean (bps) | stderr (bps) | mean ± 2σ |
|---|---|---|---|---|---|---|
| DOGE-USD | low | 1 | 10 | −0.744 | 0.173 | (−1.09, −0.40) |
| DOGE-USD | high | 1 | 10 | −0.698 | 0.211 | (−1.12, −0.28) |

Both intervals are entirely *below* zero (upper CI < 0). If either crossed n ≥ 30 with the same shape, the F6.5 negative-edge gate would auto-block them. **Direction is consistent with "no positive edge at the deferred-entry moment itself"** — which is unsurprising; horizon=1s is the moment of fire, no price movement has elapsed yet. The fade hypothesis lives or dies at the longer horizons (5s, 30s, 60s, 300s, 1800s), where current samples are all in the sparse tier.

## Top sparse cells worth tracking on next re-run

The brief said don't report individual numbers for sparse cells. Honoring that. But here are the cells closest to crossing into suggestive — these are the candidates to watch:

| ticker | bucket | horizon_s | n |
|---|---|---|---|
| SOL-USD | low | 1 | 9 |
| BTC-USD | med | 1 | 7 |
| DOGE-USD | low | 5 | 4 |
| DOGE-USD | high | 5 | 4 |
| DOGE-USD | med | 30 | 4 |

(Numbers shown only because they're the rate-limiting cells for the verdict; the strict report-per-tier rule is preserved on the means/stderrs.)

## Capture-rate sanity (F8a-fix's invariant)

```
total_post_fix | with_best_bid | with_close
           114 |           114 |        114
```

100% capture rate held across the 13-hour soak. F8a-fix's per-ticker-heap is doing its job in the wild.

## Hourly distribution (post-fix only)

```
hour                | n
2026-05-02 04:00:00 | 14   ← snapshot-replay catchup at restart
2026-05-02 05:00:00 |  1
2026-05-02 07:00:00 |  2
2026-05-02 08:00:00 |  2
2026-05-02 11:00:00 | 60   ← spike (NOT extrapolated per brief)
2026-05-02 12:00:00 | 22   ← spike tail
2026-05-02 13:00:00 |  2
2026-05-02 14:00:00 |  3
2026-05-02 15:00:00 |  1
2026-05-02 16:00:00 |  4
2026-05-02 17:00:00 |  3
```

The 11:00 UTC spike (60 alerts) is treated as a single anomaly per the brief. **Steady-state rate** (last 6 hours, post-spike): 14 alerts / 6h ≈ **2.3 alerts/hour total across all 5 pairs**.

## Per-ticker rate (last 6h, steady state)

| ticker | n |
|---|---|
| DOGE-USD | 17 |
| BTC-USD | 15 |
| ETH-USD | 13 |
| SOL-USD | 13 |
| AVAX-USD | 7 |

DOGE is the densest. With 14 high-bucket alerts at DOGE over the last 10.4h (~1.35/hr), and current `n=10` at the densest single cell (DOGE high horizon=1), reaching n=30 takes ~15 hours of soak at the current rate.

Three verdict-grade cells (the decision-tree minimum) requires at least 3 of the densest cells to cross. Conservatively projecting against the 5 cells currently at n ≥ 7: each needs 23 more samples at ~0.5–1.5 alerts/hr per cell ≈ 16–46 hours.

**24-hour soak is the conservative lower bound** for getting 3+ verdict-grade cells. Hence the recommended re-run time of **2026-05-03 17:00 UTC**.

This sits comfortably below the brief's 7-day "signal too rare to evaluate" threshold (Open Question 4). If the next re-run still finds 0 verdict-grade cells, *that* would be the time to consider whether the signal is too rare at current `VOL_BREAKOUT_MULT = 2.0` for fade evaluation in any reasonable timeframe.

## Decay-miner health snapshot

Latest supervisor metrics line (2026-05-02 17:45 UTC):

```
[fast_path] decay_miner alerts=1051 exits=16 book_ticks=246657
            obs_scheduled=8408 obs_finalized=1779 backfilled=0
            pending_heap=1112 validations=3 db_errors=13
            last_finalize=2026-05-02T17:45:27.710066
```

| metric | value | read |
|---|---|---|
| alerts_received | 1051 | healthy intake |
| obs_scheduled | 8408 | 1051 alerts × 8 horizons = 8408 ✓ exact match |
| obs_finalized | 1779 (21%) | most are still waiting on long horizons (300s/1800s/3600s/14400s) |
| pending_heap | 1112 | not growing monotonically — book_ticks are draining it |
| validations | 3 | only 3 exits had a matching alert lineage to update validation residuals |
| db_errors | 13 | small, non-zero — flagging in Open Q below |
| watchdog | silent | no `decay_miner watchdog: task died` lines in 1h |

**Watchdog**: zero death notices since F-hygiene-1 deployed. Decay miner alive throughout the soak window.

## Pair status snapshot (F-hygiene-1 self-clear verification — natural)

```
ticker   | state     | last_error
AVAX-USD | streaming | <NULL>
BTC-USD  | streaming | <NULL>
DOGE-USD | streaming | <NULL>
ETH-USD  | streaming | <NULL>
SOL-USD  | streaming | <NULL>
```

All five pairs streaming, all `last_error = NULL`. **This is the natural live verification of F-hygiene-1.2 + 75c5776** that the host network outage blocked yesterday. After WS reconnected and 5+ minutes of clean streaming elapsed, the self-clear logic + DB-load-on-register fix correctly cleared the prior `ws_loop:OSError` rows. Synthetic harness verification was correct; live behavior matches.

## Realized-validation snapshot

The brief flagged that `realized_validation_count` may be 0 for everything. **Confirmed:** of 83 cells, 82 have `realized_validation_count = 0`. One ETH-USD med horizon=1800s has `val_n = 1`. This is consistent with the fact that no `volume_breakout_pullback_long` alert has produced an executor fill (gate stack blocking — `negative_edge` and `signal_not_tradeable` rejecting most), so no `fast_exits` row references a pullback alert lineage that the miner can validate against.

**This signal isn't available for verdict purposes yet.** The miner mean/stderr is the only reportable metric.

## Cross-ticker pooling sanity check (Open Question 1)

The brief warned against pooling across tickers without thinking. Pooling the 5 densest horizon=1 cells (the candidates closest to verdict-grade):

| ticker | bucket | n | mean (bps) |
|---|---|---|---|
| DOGE-USD | low | 10 | −0.74 |
| DOGE-USD | high | 10 | −0.70 |
| SOL-USD | low | 9 | −0.60 |
| BTC-USD | med | 7 | −0.01 |
| ETH-USD | low | 6 | +0.07 |

Pooled across these 5 cells (n=42, total obs): mean leans slightly negative-to-zero. **Pooling does NOT change the verdict.** Even in the optimistic-pooling case, horizon=1s shows no positive edge — because horizon=1s IS the fire moment, before any predicted reversion. Fade hypothesis isn't testable at horizon=1 by construction; we need samples at 5s, 30s, 60s+ to come into verdict-grade range.

**Per the brief's instinct: the answer is NO — different tickers have different microstructure (BTC vs DOGE spread, depth, fill cadence). Report per-ticker. The pooled view here doesn't override that — it's just an extra sanity check against a hidden pattern, and there isn't one.**

## Recommendation

**Next NEXT_TASK.md should be: "f8a-evaluation-rerun" — same brief as this task, same SQL queries, run at 2026-05-03 17:00 UTC.**

One-line description: *re-run the F8a evaluation analysis on accumulated data after another ~24h of soak; expect 3+ verdict-grade cells by then under current ~1.35/hr DOGE-high-bucket rate.*

If the next re-run still finds 0 verdict-grade cells in the densest cells:
- Consider whether `VOL_BREAKOUT_MULT = 2.0` is too aggressive for fade evaluation timeframes (would be a *threshold-tuning* discussion, NOT this task's call).
- Consider whether F8 should pivot to F9 (signal redesign) sooner than waiting indefinitely.

If the next re-run finds verdict-grade cells:
- Apply the decision tree branches 1–3 (supported / refuted / noisy).
- The "supported" branch leads to F8b (calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` from data, then ramp gates).
- The "refuted" branch leads to F9 (new signal types — fade isn't where the edge is).

## Surprises / deviations

1. **The 11:00 UTC spike (60 alerts in one hour) is dramatic but explained.** That hour saw a real Coinbase volume spike on multiple pairs simultaneously — confirmed by checking the original `volume_breakout_long` count over the same period (mirror of the deferred type by construction). Not extrapolating per the brief; treating it as one data point in an otherwise quiet steady-state of ~2.3/hr.

2. **`db_errors = 13` on the decay miner.** The brief asked me to flag if growing or stable. It's been at 13 for at least the past few minutes' supervisor lines (i.e., not growing rapidly), but not zero either. Could be a transient psycopg2 retry, a malformed payload, or something legitimately needing attention. Worth a passing investigation in a future hygiene task — not in scope for this evaluation.

3. **Validation residuals are unavailable as a verdict signal.** The miner's negative-edge / tradeability gates are blocking pullback fills before they can produce realizable exits, so `realized_validation_count` stays at 0 across the board. Until a pullback alert produces a real `fast_exits` row referencing it, this validation-loop is closed in theory but unfired in practice. Note for Cowork.

## Open questions for Cowork

1. **The `db_errors = 13` on decay_miner.** Stable but non-zero. Worth investigating in the next hygiene pass? Easy probe: `docker compose logs fast-data-worker --since 12h | grep -E "decay_miner.*ERROR|decay_miner.*db_errors"` to find the actual error category.

2. **Whether `VOL_BREAKOUT_MULT = 2.0` is too aggressive given the observed fire rate.** At 2.3/hr steady-state across 5 pairs × 3 buckets × 8 horizons, the per-cell observation rate is too sparse for fade evaluation in single-day timeframes. Lowering MULT to seed faster firing is exactly what F6 and F8a told us *not* to do (would reintroduce noise). The honest read might be: this signal is structurally rare, and that's the answer — pivot to F9 sooner. Flagging for strategic discussion at the next re-run if the data still doesn't accumulate.

3. **Whether to add a watchdog log entry per supervisor metrics tick** (e.g., "watchdog: decay_miner OK") so silence-as-health is positively confirmed rather than inferred. Currently the watchdog only logs on death. F-hygiene-1's CC report flagged this as Open Question 4. Carrying it forward.

4. **The pending_heap = 1112 figure** is normal given that ~half the scheduled observations have horizons ≥ 1800s, but I haven't observed a steady-state trend across days. Worth a `pending_heap` time-series check on the next re-run to confirm it's truly oscillating, not slowly growing. Easy SQL by joining decay_miner stats logs over time.

## Verbatim verification SQL — for next review

```sql
-- 1. Per-cell decay state with stderr
SELECT ticker, score_bucket, horizon_s, sample_count,
       ROUND(mean_return::numeric * 10000, 3) AS mean_bps,
       ROUND((CASE WHEN sample_count > 1
                   THEN SQRT(m2_return / (sample_count - 1)) / SQRT(sample_count)
                   ELSE NULL END)::numeric * 10000, 3) AS stderr_bps,
       realized_validation_count AS val_n,
       last_updated
FROM fast_signal_decay
WHERE alert_type = 'volume_breakout_pullback_long'
ORDER BY ticker, score_bucket, horizon_s;

-- 2. Tier distribution
SELECT
  CASE WHEN sample_count >= 30 THEN 'verdict_grade'
       WHEN sample_count >= 10 THEN 'suggestive'
       ELSE 'sparse' END AS tier,
  COUNT(*) AS cells,
  SUM(sample_count) AS total_obs,
  MIN(sample_count) AS min_n, MAX(sample_count) AS max_n
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
GROUP BY tier ORDER BY tier;

-- 3. Capture-rate sanity (post-fix only)
SELECT
  COUNT(*) AS total_post_fix,
  COUNT(*) FILTER (WHERE features ? 'best_bid' AND features->>'best_bid' IS NOT NULL) AS with_best_bid,
  COUNT(*) FILTER (WHERE features ? 'close'    AND features->>'close'    IS NOT NULL) AS with_close
FROM fast_alerts
WHERE alert_type = 'volume_breakout_pullback_long' AND id > 2300;

-- 4. Hourly distribution (last 24h)
SELECT date_trunc('hour', fired_at) AS hour, COUNT(*) AS n
FROM fast_alerts
WHERE alert_type='volume_breakout_pullback_long'
  AND id > 2300
  AND fired_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;

-- 5. Per-ticker rate over the last 6h (steady state)
SELECT ticker, COUNT(*) AS n
FROM fast_alerts
WHERE alert_type='volume_breakout_pullback_long'
  AND fired_at > NOW() - INTERVAL '6 hours'
GROUP BY ticker ORDER BY n DESC;
```

Re-run at ~2026-05-03 17:00 UTC.
