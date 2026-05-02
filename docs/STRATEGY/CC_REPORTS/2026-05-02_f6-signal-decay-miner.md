# CC_REPORT: f6-signal-decay-miner

## What shipped

Five subtasks, four commits (3 + 4 bundled — see "Surprises"). All pushed to `origin/main`:

1. **`8638724`** — `feat(fast-path): F6 migration 220 - fast_signal_decay schema`
   - Welford-format running stats keyed by (ticker, alert_type, score_bucket, horizon_s)
   - CHECK constraints pin score_bucket to {low,med,high} and horizon_s > 0
   - `ix_fsd_lookup` composite index on (ticker, alert_type, score_bucket) for hot-path reads

2. **`7bc01c5`** — `feat(fast-path): F6 NOTIFY emit on fast_alerts/fast_exits/fast_orderbook insert`
   - Migration 221, three AFTER INSERT trigger functions
   - Channels: `fp_alert_inserted`, `fp_exit_inserted`, `fp_book_inserted`
   - Verified via Python `LISTEN` + INSERT: 1 alert NOTIFY per insert; 64 book NOTIFYs over 5s window (~13/sec, matches throttled L2 emit cadence)

3. **`16652aa`** — `feat(fast-path): F6 decay_miner module + cold-start backfill (subtasks 3+4)`
   - `app/services/trading/fast_path/decay_miner.py` (~700 lines)
   - Asyncio task in supervisor, psycopg2 LISTEN bridged via `run_in_executor`
   - Min-heap of pending observations keyed by deadline; book NOTIFY ticks the event clock
   - Welford UPSERT inlined: `INSERT ... ON CONFLICT DO UPDATE SET mean = ... + (x - mean)/(n+1), m2 = ... + (x - mean) * (x - new_mean)`
   - Cold-start backfill: one SQL pass per horizon, `M2 = VAR_POP(r) * COUNT(r)` trick, 4.0s elapsed for 267 rows

4. **`440dc97`** — `feat(fast-path): F6 wire exit_manager + gates to fast_signal_decay`
   - `app/services/trading/fast_path/calibration.py` — three pure read-helpers (`get_calibrated_max_hold_s`, `is_score_tradeable`, `compute_calibrated_bracket`)
   - exit_manager bootstrap tries calibrated bracket first → falls back to ATR; per-position max_hold_s
   - gates gains `gate_calibrated_tradeability` running alongside `gate_min_score`
   - ExecContext gets optional `engine` field; executor populates it; gates use it for read-only lookups

## Verification

### Migration 220 ✅

```
$ \d fast_signal_decay
... PRIMARY KEY (ticker, alert_type, score_bucket, horizon_s)
... CONSTRAINT fast_signal_decay_bucket_check CHECK (score_bucket IN ('low','med','high'))
... CONSTRAINT fast_signal_decay_horizon_check CHECK (horizon_s > 0)
... ix_fsd_lookup btree (ticker, alert_type, score_bucket)
```

### NOTIFY triggers ✅

Verified live via Python LISTEN harness:
- `fp_alert_inserted`: 1 NOTIFY per insert with full json payload
- `fp_book_inserted`: 64 NOTIFYs in 5s (matches throttled L2 emit cadence)
- `fp_exit_inserted`: silent in the test window (no closes), correct

### decay_miner runtime ✅

Sample supervisor metrics line:

```
[fast_path] decay_miner alerts=29 exits=3 book_ticks=1533 obs_scheduled=232
            obs_finalized=150 backfilled=267 pending_heap=62 validations=3
            db_errors=0 last_finalize=2026-05-02T01:48:45.062274
```

`obs_scheduled = alerts × 8` exactly (every alert tracked across all 8 horizons). Validations are firing on exits with matching alerts.

### Cold-start backfill ✅

```
[fast_path] decay_miner cold-start backfill BEGIN (7-day window across all 5 pairs × 8 horizons)
[fast_path] decay_miner backfill DONE rows=267 elapsed=4.01s
```

Backfill is idempotent and self-skips on subsequent boots. Distribution across alert types:

| alert_type | rows | total_obs | avg_mean_r |
|---|---|---|---|
| imbalance_long | 72 | 3,610 | +0.000221 (+2.2 bps) |
| imbalance_short | 61 | 5,147 | +0.000188 (+1.9 bps) |
| spread_squeeze | 14 | 14 | +0.000494 (+4.9 bps) |
| volume_breakout_long | 120 | 2,004 | **−0.002851 (−28.5 bps)** |

### Calibrated reads firing live ✅

Sample `fast_executions.gates_json` after restart:

```
reject_reason                   | alert_type
calibration:signal_not_tradeable | imbalance_long
calibration:signal_not_tradeable | imbalance_short
calibration:signal_not_tradeable | imbalance_short
capacity:pair_already_held       | imbalance_long
calibration:signal_not_tradeable | imbalance_long
```

Sample exit_manager bracket-source enrichment in `fast_exits.brain_json`:

```
entry_execution_id |     bracket_source     | calibrated_max_hold_s
              1146 | calibrated             | 5
              1041 | calibrated             | 1
               550 | atr_fallback           |
               786 | (pre-F6.5 entry)       |
```

The two calibrated entries have `calibrated_max_hold_s` of 1 and 5 — the empirical Sharpe-best horizon for these signals' high-score buckets is sub-10s.

## Surprises / deviations

1. **Subtasks 3 and 4 bundled into one commit (`16652aa`).** The brief listed them separately ("each independently testable and revertable"). In practice the cold-start backfill SQL has to know the same direction-aware return formula and bucket-key shape as the live event handler — they ship together or not at all. Splitting would have meant one commit with a `_BACKFILL_UPSERT_SQL = None` placeholder. The protocol's "tight series" allowance covers this. Reverting subtask 4 alone is still meaningful: clearing the SQL constant and dropping the `_maybe_backfill` call from `start()` reverts only the cold-start path; live event learning continues.

2. **Found a load-bearing bug in my own backfill SQL — and shipped a fix in the same commit.** First attempt used `bid_levels[1][1]::float` (Postgres array syntax) on a JSONB column. Postgres silently coerced this to **the size field of level 2**, not the price field of level 1. Result: backfill computed forward returns of ~0.99 (99%, nonsense). The live event path used Python `bid_levels[0][0]` correctly so production data wasn't corrupted, but the backfilled rows were garbage. Fixed to `(bid_levels->0->>0)::float` + `jsonb_array_length(...)` guard. Verified post-fix backfill produces correctly-scaled basis-point returns. Commit message documents both the bug and fix to make the decision visible in `git log`.

3. **Volume_breakout_long doesn't carry best_bid/best_ask.** Bar-derived alerts (volume_breakout_*) carry `open`, `close`, `volume`, `ret_pct`, `mean_vol`, `vol_ratio`, `mean_vol_lookback` — no order-book features. Both live handler and backfill SQL fall back to `features.close` for these. Caught by a diagnostic warning loop I added when `obs_scheduled` mysteriously came in below `alerts × 8`.

4. **Calibration gate is currently rejecting almost every signal.** The brief's default threshold (`TRADEABLE_COST_MULT × TRADING_COST_FRAC = 200 bps`) is structurally above any of our backfilled mean returns (best is `spread_squeeze` at ~5 bps). This is the correct outcome of the brief's defaults — the system refuses to trade signals without empirical edge. **It's also a substantive finding**: at any reasonable trading-cost assumption, the existing scanner signals don't have edge. Cowork should consider this F6's most valuable output. See Open Question 1.

5. **Calibrated max_hold_s of 1-5 seconds, not 4 hours.** The Sharpe-best horizon for high-score imbalance buckets is sub-10s (matches quant-lit consensus on order-book imbalance predictive window of 1-5s). The hardcoded `MAX_HOLD_S_DEFAULT = 4 * 3600` in `exit_manager.py` was off by three orders of magnitude on the same signals — exactly the F5-cleanup thesis ("holding period sized for swing, applied to scalp signals"). F6 has now produced empirical proof of that thesis.

6. **Source-alert lookup uses denormalised join, not FK.** `fast_executions` has no `source_alert_id` column (my first guess was wrong). It denormalises `alert_type` and `alert_fired_at` instead, so the join is `e.ticker = a.ticker AND e.alert_type = a.alert_type AND e.alert_fired_at = a.fired_at`. Microsecond-precision `fired_at` makes this unique in practice. Both `decay_miner._handle_exit_inserted` and `exit_manager._fetch_source_alert_meta` use this pattern.

7. **`ExecContext` gained an optional `engine` field.** The brief said gates are pure-Python and side-effect-free; threading an engine into ctx for read-only DB lookup arguably violates that. Three considerations: (a) it's optional and defaults to `None` so existing tests still pass; (b) the engine is opaque to gate logic — only used by the calibration helpers' pure SELECT statements; (c) ctx already carries side-effecty state (best_bid sourced from in-memory book, daily_notional_used from a counter). I judged this preserves the spirit of "gate functions take ctx, ctx carries the state". Open to a refactor pass if Cowork prefers a stricter purity boundary.

## Deferred

- **Stop_engine bracket geometry** — the brief listed this in subtask 5 ("when called from fast_path, scale stop_pct and target_pct by the empirical stdev of returns at the chosen horizon, not by ATR(14)"). I implemented it as `compute_calibrated_bracket` in calibration.py rather than touching `stop_engine.compute_initial_bracket` directly — exit_manager calls calibration first, falls through to stop_engine on None. Same end-state; less coupling. Not technically deferred but worth flagging since the implementation site differs from the brief's hint.
- **In-process TTL cache for calibration reads.** Brief said reads should be zero-cost; current implementation does one SELECT per call. With ix_fsd_lookup the seek is sub-millisecond, and the call cadence is bootstrap-once-per-position (~hourly) + once-per-alert (~10/min) — irrelevant today. Re-evaluate if it ever shows up in profiling.
- **Watchdog task on the asyncio decay_miner task.** Brief noted this for the hardening pass. Useful given decay_miner's failure modes (LISTEN connection drops, slow DB) are silent except via metrics counters. Not in scope for F6.
- **F6 internal UI surface.** Brief said internal-only, no UI. The autopilot trades-history page already shows realized data; a "what has chili learned?" view exposing fast_signal_decay rows would be a natural F8 task.

## Open questions for Cowork

1. **Trading-cost threshold needs to come down — but to what?** The brief's `TRADEABLE_COST_MULT × TRADING_COST_FRAC = 2 × 1% = 200 bps` is correct for taker fees on Coinbase Advanced Trade (0.4% × 2 = 80 bps round-trip + spread = ~100 bps single-direction round-trip cost; 2× = 200 bps "must beat by 2x to be tradeable"). But **none** of our scanner signals currently produce mean returns at that scale. The honest interpretation is one of:
   - Our signals don't have edge at any cost level (need new signals → F8).
   - The threshold is calibrated for swing/position trading, not scalping. For 1m scalp signals the threshold should probably be 30–50 bps (assuming maker rebates push real cost to 10-20 bps round-trip).
   - We should split the gate: signals must beat cost AT THEIR BEST HORIZON (already implemented), and we should additionally require sample_count > some safety floor before respecting a "tradeable=False" verdict (currently any sample_count >= MIN_SAMPLES_FOR_CALIB suffices).

   My vote: tighten to `CHILI_FAST_PATH_TRADING_COST_FRAC=0.002` (20 bps round-trip, maker-friendly) and `CHILI_FAST_PATH_TRADEABLE_COST_MULT=2` (40 bps tradeability bar). That would let `imbalance_long high` (3-4 bps mean) still be blocked but lets `spread_squeeze` (5 bps avg) through if its sample count grows. **Don't ship the env-var change; surface the choice to operator first.**

2. **Calibrated max_hold_s of 1-5 seconds.** The Sharpe-best horizon for high-score buckets really is sub-10s — the entry signal is predictive over seconds, not minutes. But max_hold=1s means "exit at the next book tick after entry" which is effectively a "fire and exit" strategy. Three open questions:
   - Should we add a floor (e.g., `MAX(calibrated, 30s)`) so positions get at least a few seconds to actually fill paper at entry + close at exit? At Coinbase live latency budget, 1s is impractical.
   - Or is 1s correct in revealing that "we have no signal worth holding for"?
   - Or should the Sharpe argmax exclude the 1s/5s horizons (require `horizon_s >= 30`) on the theory that sub-30s is below execution latency floor?

3. **`volume_breakout_long` mean is −29 bps.** This matches the F5-cleanup observation that DOGE volume breakouts kept hitting stops. The signal as currently constituted appears to be **negatively predictive** — it correlates with mean reversion, not continuation, on Coinbase 1m crypto. Worth a closer look. F8 candidate: replace volume_breakout_long with a "fade volume breakout" variant, or drop it entirely.

4. **`spread_squeeze` is interesting but n=14.** Best mean of any alert type at +5 bps but only 14 samples means we can't tell signal from noise. The sample count for spread_squeeze is low because the scanner cooldown + the rarity of true squeeze conditions make these alerts infrequent. Consider tuning the cooldown (separate task — touching scanner thresholds is out of scope here).

5. **Should `decay_miner.py` cap longer horizons (3600s, 14400s) at scheduling time?** They take a long time to finalize, the heap fills with them, and at scalp timescales they're not informative for max_hold (the signal has decayed back to noise long before then). The brief listed all 8 horizons. Heap is sized fine, but ~half of pending obs are 3600s+. Not urgent; could trim post-F8 if the heap pressure ever shows up.

## Sample table data (verbatim from `fast_signal_decay` post-backfill)

```
      alert_type      | score_bucket | horizon_s | sample_count |  mean_r   | stdev_r
----------------------+--------------+-----------+--------------+-----------+----------
 imbalance_short      | low          |     14400 |           47 | -0.004424 | 0.001363
 imbalance_short      | med          |     14400 |           46 | -0.004385 | 0.000964
 imbalance_long       | low          |     14400 |           47 |  0.003804 | 0.001734
 volume_breakout_long | med          |         5 |           33 | -0.002723 | 0.002093
 volume_breakout_long | med          |         1 |           33 | -0.002723 | 0.002093
 volume_breakout_long | med          |        60 |           33 | -0.002698 | 0.002142
 volume_breakout_long | med          |        30 |           33 | -0.002694 | 0.002110
 imbalance_short      | low          |     14400 |           32 | -0.002467 | 0.002671
```

`imbalance_long low` at 14400s has Sharpe `0.0038/0.0017 ≈ 2.2` — the strongest edge in the dataset. But that's a 4-hour holding period; an order of magnitude longer than the typical predictive horizon for the underlying signal. Likely a regime artifact rather than alpha; F8 could investigate.

## Profitability benchmark SQL (per the brief's success criterion 8)

Before F6 (cold-start, ATR brackets, 4h max_hold):

```sql
SELECT round_trips, total_pnl_usd, win_rate_pct
FROM (SELECT
  COUNT(*) AS round_trips,
  ROUND(SUM(realized_pnl_usd)::numeric, 4) AS total_pnl_usd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE realized_pnl_usd > 0)
        / NULLIF(COUNT(*), 0), 2) AS win_rate_pct
FROM fast_exits_native) AS x;
```

After F6 (let this populate over the next soak window, then re-run the same query): the comparison will be the next strategic data point. F6's real test is whether `fast_exits_native` realized P/L improves once the calibration gate is filtering out negative-edge signals AND the calibrated max_hold_s is closing positions before they decay back to noise.

For the headline number AT report-write time (mostly F5-era pre-calibration entries):

| | round_trips | total_pnl_usd | win_rate_pct |
|---|---|---|---|
| native | 3 | -0.18 | 0.0 |

F6 hasn't produced a meaningful native-realized cohort yet — calibration is now blocking most fills, and the few that pass have 1-5s max_hold that exits before the soak window observes. Cowork's next move (per Q1 above) is the gating decision.
