# NEXT_TASK: f8a-volume-breakout-pullback-fade

STATUS: DONE

## Goal

Test the F6 finding that `volume_breakout_long` is reliably negatively predictive (mean = −28.5 bps over n=120) by emitting a complementary alert type that fires AFTER the mean reversion plays out, then learning its forward-return distribution via the existing decay miner. After this task:

1. **A new scanner alert type `volume_breakout_pullback_long` exists** that fires `DELAY_S` seconds after a `volume_breakout_long` would have, with the same `(ticker, signal_score)` lineage. Hypothesis: if price drops after the original spike, entering at the deferred timestamp captures the mean reversion as a tradeable long.
2. **Scanner emits the deferred alert event-drivenly** — no `while True: sleep(N)`. Mechanism mirrors F6's decay-miner heap pattern: when a `volume_breakout_long` fires, the scanner schedules a deferred-emit task; the natural event flow (next book emit on that ticker after `t + DELAY_S`) is the trigger.
3. **The decay miner picks up the new alert type automatically.** No miner changes needed — it already groups by `alert_type`. Within ~hours of soak, `fast_signal_decay` will have observations for `volume_breakout_pullback_long` across the same horizon ladder.
4. **The standard gate stack applies, including the F6.5 negative-edge auto-exclusion.** If the deferred alert turns out to also be negative (e.g., mean reversion overshoots), it'll auto-block. If positive, it'll start passing fills. **The system tells us whether the fade works without human tuning.**

This is the cheapest credible test of the F6 thesis. If the fade has edge, we keep going. If it doesn't (signal is symmetrically bad in both directions), we drop the experiment and move to F9 (order-book momentum). Either outcome is informative.

## Why now

F6 produced two unambiguous facts:
- `volume_breakout_long` mean forward return is −28.5 bps over hundreds of observations.
- That negative return implies price reverts after the alert. Reversion is the kind of pattern Ross Cameron-style scalpers exploit: wait for the climax, buy the dip.

We can't short on spot Coinbase. But we **can** wait through the mean reversion and enter long at the lower price — capturing the recovery if there is one. That's the cleanest tradeable interpretation of "fade." The decay miner can validate or refute the recovery thesis automatically.

This is also the smallest credible F8: one new alert type, one scheduling helper, zero migrations, zero gate changes, zero new tables. If the experiment fails, rolling back is one revert.

## Architectural commitments

- **Event-driven, not cycle-based.** Same principle as F6. The deferred emit is scheduled by the natural event flow — book emits on the same ticker after `t + DELAY_S` cross the deadline, the alert fires.
- **No new magic numbers… initially.** `DELAY_S` is the one new constant, but it's a starting point: 30 seconds, picked from F6's decay curve where the negative return appears to bottom (between the 1s and 60s horizons). After ~24h of data, we calibrate it from `fast_signal_decay` itself — the optimal delay is the horizon where the cumulative forward return on `volume_breakout_long` is most negative AND the subsequent horizon is less negative (reversion bottomed). That's a follow-up task, not this one.
- **Standard gate stack.** No new gates. The existing F6.5 negative-edge gate will block the new alert type until it accumulates enough samples (n ≥ 30) and proves edge. That's the right safety property.
- **No exit-manager changes.** This is a new entry-signal experiment, not an exit logic change.
- **No miner changes.** Decay miner is alert-type-agnostic; new types appear automatically once the scanner emits them.

## Scope — three subtasks, three commits

### 1. Scanner emits `volume_breakout_pullback_long` deferred alert

In `app/services/trading/fast_path/scanner.py`:

- Define `VOL_BREAKOUT_PULLBACK_DELAY_S = 30.0` as a module constant. Document inline that this is a starting value, calibrated from F6 evidence that the predictive horizon for mean reversion of `volume_breakout_long` falls in the 5–60s range. Future tuning should be data-driven from `fast_signal_decay`.
- When `_check_volume_breakout_long()` (or wherever the existing alert fires) emits a `volume_breakout_long` alert, ALSO schedule a deferred emission of `volume_breakout_pullback_long` for `t + DELAY_S`.
- The deferred emission carries the same `(ticker, signal_score, features)` as the original — score lineage preserved.
- Use a per-scanner asyncio heap (`heapq` keyed by deadline, mirroring decay_miner's approach) to track pending deferred emits. Cap at `MAX_PENDING_DEFERRED = 1000` to bound memory; drop oldest if exceeded (logged at WARNING).
- Hook into the scanner's existing book-emit handler — every book emit checks the heap head; deferred alerts whose deadline has passed get emitted immediately, with the alert's `fired_at` set to the current wall-clock time (NOT the original alert's time, because the WHOLE POINT is that the fire-time is the deferred-entry moment).
- The deferred alert's `features` JSONB should carry: `original_alert_id`, `original_fired_at`, `delay_s`, plus a copy of the original's relevant features (volume ratio, mean_vol, close, etc.) so the decay miner can postmortem connections. Tag the new alert with its own `alert_type = 'volume_breakout_pullback_long'`.

Verify deferred emission via psql LISTEN on `fp_alert_inserted` after a `volume_breakout_long` fires:
1. Watch for the original alert NOTIFY at t=0.
2. Watch for the deferred `volume_breakout_pullback_long` NOTIFY at t≈30s.

Commit: `feat(fast-path): F8a scanner emits volume_breakout_pullback_long deferred alert`.

### 2. Wire the new alert type into recognition surfaces

The new `alert_type` needs to appear in places that enumerate or render alert types:

- `app/services/trading/fast_path/calibration.py` — verify `is_score_tradeable` and `is_negative_edge_excluded` work transparently on the new type. They should — they're alert-type-agnostic — but confirm via a unit-style probe: call `is_negative_edge_excluded(engine, 'BTC-USD', 'volume_breakout_pullback_long', 0.55)` and verify it returns `(False, evidence_dict)` (insufficient samples → not excluded).
- `app/templates/trading/_autopilot_fast_path.html` — the alert-type column in the trades table will render the new string verbatim. Verify by visual inspection (or curl the page and grep) that `volume_breakout_pullback_long` shows up if any decision rows have it.
- `app/routers/trading_sub/fast_path_api.py` — the `/closed-trades` and `/recent-decisions` endpoints don't filter by alert_type, so they'll just include the new type. Confirm by hitting the endpoints after the first deferred alert fires.

No code changes expected here. This subtask is verification-only. Document in the CC_REPORT that no code change was needed.

Commit: skip (no changes). Note in CC_REPORT.

### 3. Verification soak — observe the decay curve form

After deploy:

1. Restart `fast-data-worker` to pick up the new scanner code.
2. Wait for at least 30 minutes of live ingestion to give the new alert type time to fire and accumulate samples.
3. Query the decay miner state:
   ```sql
   SELECT ticker, alert_type, score_bucket, horizon_s, sample_count,
          ROUND(mean_return::numeric * 10000, 2) AS mean_bps,
          ROUND(SQRT(m2_return / NULLIF(sample_count, 0))::numeric * 10000, 2) AS stdev_bps
   FROM fast_signal_decay
   WHERE alert_type = 'volume_breakout_pullback_long'
   ORDER BY ticker, score_bucket, horizon_s;
   ```
4. Compare with the same query for `alert_type = 'volume_breakout_long'`. The hypothesis is that the pullback variant's mean_return at short horizons (1s, 5s, 30s) is positive where the original was negative — that's the recovery half of the mean-reversion cycle.
5. Document the comparison in the CC_REPORT. **Don't** attempt to interpret edge significance — n will likely be too small after 30 min. The point is: did the pipeline produce the new data correctly, and what's the early shape of the curve?

If observations are landing in `fast_signal_decay` for the new alert type and the shape is plausibly different from the original — task is verified successful.

Commit: skip (no changes). Document in CC_REPORT.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/fast_path/scanner.py` — extend in place. Reuse the existing `MomentumScanner` class. Don't create a new scanner.
- `app/services/trading/fast_path/decay_miner.py` — read it for context, **do not modify**. The new alert type is picked up transparently via the LISTEN/NOTIFY channel.
- `fp_alert_inserted` NOTIFY trigger (mig 221) — existing. Already fires for any new `fast_alerts` row regardless of `alert_type`.
- `fast_signal_decay` schema (mig 220) — existing. New `(ticker, alert_type, score_bucket, horizon_s)` rows will populate as observations accumulate.
- F6.5's `gate_negative_edge_excluded` — applies transparently. Will block the new type if it shows negative edge above the n ≥ 30 threshold.

## Constraints / do not touch

- **Live-placement safety belts.** All 8 layers untouched.
- **Default mode stays paper.** No flag changes.
- **`DELAY_S = 30.0` is a starting value, not a tuned magic number.** Document it as such inline. Do NOT tune it in this task even if the data argues for a different value mid-soak. Tuning is its own follow-up after we have ≥1 day of data.
- **No changes to the existing `volume_breakout_long` alert.** It still fires; it still gets blocked by the negative-edge gate. The pullback variant is additive.
- **No bundling other F8 candidates** (order-book momentum, trade tape aggression, etc.). One experiment at a time.
- **`models/trading.py` and `.env.example` working-tree changes.** Continue to leave them alone.

## Out of scope

- Calibrating `DELAY_S` from data (follow-up task after observation).
- Adding the deferred alert as an exit signal on existing positions.
- Subscribing to Coinbase `market_trades` channel for trade-tape signals (F9 candidate).
- Order-book momentum signal (F9 candidate).
- Cross-pair lead-lag (F9 candidate).
- Time-of-day session signals (F9 candidate).
- A "blocked signals" UI card on autopilot.
- Watchdog task on decay_miner.

## Success criteria

1. `git log --oneline -3` shows one new commit (subtasks 2 and 3 are verification-only with no code changes).
2. `docker compose ps fast-data-worker` healthy after deploy.
3. After 30+ minutes of live soak, `SELECT COUNT(*) FROM fast_alerts WHERE alert_type = 'volume_breakout_pullback_long'` returns > 0.
4. The same query on `fast_signal_decay` returns > 0 rows after observations cross their first horizon (≥1 second after a deferred alert fires).
5. CC_REPORT includes:
   - Direct comparison query output: `volume_breakout_long` vs `volume_breakout_pullback_long` mean_return per (ticker, score_bucket, horizon_s) where both have rows.
   - Verbatim sample of a `volume_breakout_pullback_long` row from `fast_alerts` showing the `features` JSONB carries the original alert lineage.
   - Confirmation that calibration helpers transparently handle the new alert type (direct probe).
   - Heap depth metrics from scanner stats (verifying the deferred-emit heap is bounded).

## Open questions for Cowork (surface in your report only if relevant)

1. **`DELAY_S = 30.0` starting value.** I picked it from rough quant-lit consensus on order-book imbalance reversion timescales (1–60s). If F6 data argues for a different starting value (e.g., the original alert's `mean_return` curve hits its minimum at 5s, not 30s), propose. Don't deviate silently.
2. **`MAX_PENDING_DEFERRED = 1000` heap cap.** At ~120 vol_breakout alerts per ~24h × 1 deferred each, we generate ~5/hour. The cap is way over headroom. If you observe the heap actually growing to that size, something's wrong (probably alerts firing faster than we expect or deferred emits getting blocked).
3. **Should the deferred alert's `signal_score` be re-derived at fire time** (e.g., based on the ACTUAL price movement during the delay window) rather than copied from the original? My initial design copies the original to keep it simple. A re-derived score might be more informative but adds complexity. Defer to your design call.
4. **Do we need a fail-safe** for the case where the heap grows during a scanner restart? Currently a restart loses pending deferred emits. Acceptable trade-off for paper mode (we just miss a few alerts); worth flagging if we ever go live with this signal.

## Rollback plan

- Single-commit revert removes the deferred-emission code and the new alert type stops firing.
- `fast_alerts` rows already written remain (correctly) in the table; querying for `alert_type = 'volume_breakout_pullback_long'` after revert just returns the pre-revert subset.
- `fast_signal_decay` rows for the new type stay; queries that don't filter explicitly will still include them. Harmless — just historical data.
- No migrations to roll back, no schema changes.
- No live-placement risk: the new alert type follows the standard gate stack, including the live-mode-interlock and (until enough samples) the score/recency/spread gates plus the negative-edge gate that will block until edge proven.
