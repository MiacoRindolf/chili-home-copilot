# NEXT_TASK: f6-signal-decay-miner

STATUS: DONE

## Goal

Build a live, event-driven brain node that learns the empirical decay curve of each fast-path signal type per pair, and produces calibrated `max_hold_s` / stop / target / score-quality values that the exit_manager and gates read instead of magic numbers. Three things must be true at the end:

1. **A `fast_signal_decay` table exists** with running statistics per `(ticker, alert_type, score_bucket, horizon_s)` — empirical forward-return distribution at each measurement horizon.
2. **The decay miner runs as an asyncio task in the fast-data-worker supervisor** — listens to `fast_alerts` and `fast_exits` insertions via Postgres LISTEN/NOTIFY, schedules forward-return observations, finalizes them when the natural event flow (book emits / time progression) crosses the deadline, validates against realized exits.
3. **`max_hold_s`, score thresholds, and bracket geometry are no longer hardcoded.** The exit_manager and gates read calibrated values from `fast_signal_decay`. When the table is empty (cold start), they fall back to the existing constants — but the constants stop being load-bearing as soon as the miner has produced ≥N samples per bucket.

This is the user's "no magic numbers, let chili learn it" principle applied to the most expensive numbers in the system. It's also the user's "event-driven, no cycles" principle applied to the brain.

## Why now

We have:
- 3 native realized exits, all stop_hits, spread across BTC/DOGE/ETH (the "DOGE-only" thesis is broken — see `COWORK_REVIEWS/2026-05-02_autopilot-trades-history.md`)
- ~hundreds of `fast_alerts` rows accumulated over ~24h
- ~100k+ `fast_orderbook` rows of trajectory data
- Average holding time 30–46 min, while the entry signal (order-book imbalance) has a 1–5 *second* predictive horizon by quant-lit consensus
- Bracket geometry (ATR-based stops, R-multiple targets) sized for swing-trade timeframes, applied to scalp signals

The structural mismatch is the load-bearing problem. F6 produces the empirical truth that lets us replace hardcoded thresholds with calibrated ones. F7 (Kelly sizing) and the eventual live-mode authorization both depend on F6's outputs.

## Architectural commitments (non-negotiable for this task)

- **Event-driven, not cycle-based.** No `while True: sleep(N)` / no scheduled tasks / no periodic recompute. The miner reacts to events (alert inserted, exit inserted, book inserted, time progressing via the natural event clock). This aligns with the swing-path's Phase 2 event-handlers direction (`reference_phase2_event_handlers.md`).
- **Postgres LISTEN/NOTIFY** is the IPC. The pattern already exists in `app/services/trading/price_bus.py` and `app/services/code_dispatch/notifier.py`. Reuse, don't reinvent.
- **Incremental updates only.** The `fast_signal_decay` table updates Welford-style (running mean + variance) on each new observation. Never recomputes from scratch except at cold-start backfill.
- **Cold-start backfill is one-shot, then never again.** First boot mines the existing `fast_alerts` history against `fast_orderbook`. After that, only NOTIFY-driven updates.
- **Reads are zero-cost in the hot path.** exit_manager and gates do a single index-seek per decision; they never scan the table.

## Scope — five subtasks, multiple commits

This task is meaningfully larger than the prior ones. Split as five commits, each independently testable and revertable.

### 1. Migration 220 — `fast_signal_decay` schema

```sql
CREATE TABLE fast_signal_decay (
  ticker         VARCHAR(32) NOT NULL,
  alert_type     VARCHAR(48) NOT NULL,
  score_bucket   VARCHAR(8)  NOT NULL,   -- 'low' / 'med' / 'high', boundaries TBD by you
  horizon_s      INTEGER     NOT NULL,   -- 1, 5, 30, 60, 300, 1800, 3600, 14400
  sample_count   BIGINT      NOT NULL DEFAULT 0,
  mean_return    DOUBLE PRECISION NOT NULL DEFAULT 0,  -- forward return as fraction (0.001 = 0.1%)
  m2_return      DOUBLE PRECISION NOT NULL DEFAULT 0,  -- Welford's M2 for variance
  realized_validation_count BIGINT NOT NULL DEFAULT 0, -- # of times this bucket was validated against a fast_exits row
  realized_validation_residual DOUBLE PRECISION NOT NULL DEFAULT 0, -- mean abs error vs. the predicted distribution
  last_updated   TIMESTAMP   NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, alert_type, score_bucket, horizon_s)
);
CREATE INDEX ix_fsd_lookup ON fast_signal_decay (ticker, alert_type, score_bucket);
```

Score bucketing: I'd suggest **`low: <0.40`, `med: 0.40–0.65`, `high: ≥0.65`** as the default boundaries, but propose your own if you have a quant-lit reason. Keep the bucket count to 3 — finer slicing dilutes sample counts before we have enough data.

Forward-return measurement: at horizon T seconds after `fired_at`, look up the mid price from `fast_orderbook` (closest book row by `snapshot_at`), compute `(mid - entry_at_alert) / entry_at_alert` where `entry_at_alert` is the best-ask at fire time (matching what F4 would have used as fill price for a long).

For shorts (imbalance_short): the system doesn't actually short, but we still want the decay data — F8 might use it for "exit early" logic on long positions when an opposite-direction signal fires. Compute as `(entry_at_alert - mid) / entry_at_alert` (positive = signal worked).

Migration follows the existing pattern in `app/migrations.py` (function `_migration_220_fast_signal_decay`, registered in `MIGRATIONS` list, idempotent). Commit: `feat(fast-path): F6 migration 220 - fast_signal_decay schema`.

### 2. NOTIFY-on-insert wiring

Two Postgres NOTIFY emissions need to start firing:

- After a `fast_alerts` insert, emit `pg_notify('fp_alert_inserted', json_build_object('id', NEW.id, 'ticker', NEW.ticker, 'alert_type', NEW.alert_type, 'fired_at', NEW.fired_at, 'signal_score', NEW.signal_score)::text)`.
- After a `fast_exits` insert, emit `pg_notify('fp_exit_inserted', json_build_object('id', NEW.id, 'entry_execution_id', NEW.entry_execution_id, 'ticker', NEW.ticker, 'exit_reason', NEW.exit_reason, 'realized_return_pct', NEW.realized_return_pct, 'holding_period_s', NEW.holding_period_s, 'exited_at', NEW.exited_at)::text)`.

**Decision: trigger-based or application-level NOTIFY?** Both work. I'd suggest **trigger-based** because (a) it survives any future code path that writes the table, (b) it's atomic with the insert (no race window where the row exists but the NOTIFY hasn't fired). Implement as `AFTER INSERT` triggers in migration 220 (or a new migration 221 if you prefer to keep schema and trigger separate).

Verify by running `psql ... LISTEN fp_alert_inserted` in one session, inserting a row in another, and confirming the notification arrives. Commit: `feat(fast-path): F6 NOTIFY emit on fast_alerts/fast_exits insert`.

### 3. `decay_miner.py` — the brain node

New module `app/services/trading/fast_path/decay_miner.py`. Asyncio task that:

- Connects to Postgres via psycopg2 (or asyncpg if cleaner — the rest of the fast_path uses sqlalchemy synchronously; pick whichever doesn't fight the existing pattern). LISTENs on both channels.
- Maintains an in-memory **pending observations heap** (`heapq` keyed by deadline). When an alert insert NOTIFY arrives, the miner pushes 8 pending observations onto the heap (one per horizon: 1s, 5s, 30s, 60s, 300s, 1800s, 3600s, 14400s).
- The "wake clock" is the natural event flow — every NOTIFY (including book inserts; see below) triggers a heap-head check. If the head's deadline ≤ now, finalize that observation (look up book mid, compute forward return, Welford-update the row).
- Listens on a **third channel** `fp_book_inserted` that fires sparingly — once per emitted book per ticker (~4/sec/ticker after throttling). Use this as the event clock instead of an explicit timer. Document the cadence trade-off: book emits are at ~250ms granularity which is sufficient for our shortest 1s horizon.
- For exits NOTIFY: look up the `(ticker, alert_type, score_bucket)` of the entry alert, compute residual `|realized_return_pct/100 - mean_return_at_holding_horizon|`, increment `realized_validation_count` and update `realized_validation_residual` (running mean of absolute error).
- Memory bound: the pending observation heap caps at `max_pending_obs` (default 50000 — at ~10 alerts/min × 8 horizons × longest 4hr lookback this is ~28k typical). Above the cap, drop the longest-horizon observations first (they have the smallest predictive value at fast-lane timescales anyway).
- Diagnostic counters: `alerts_received`, `exits_received`, `obs_finalized_per_horizon`, `obs_dropped_overcap`, `db_errors`. Exposed via `stats()` for the supervisor metrics line.
- Pure-Python; no broker imports; unit-testable in isolation given a mock NOTIFY source.

Commit: `feat(fast-path): F6 decay_miner module - event-driven signal decay learning`.

### 4. Cold-start backfill

One-shot async function called once at supervisor boot if `fast_signal_decay` is empty (or has fewer than N rows; pick a sensible threshold). Batch-mines existing `fast_alerts` against `fast_orderbook` trajectories using SQL, populates the table directly. After backfill completes, the miner switches to live event-driven mode.

Backfill query sketch (pseudocode):
```sql
WITH alerts AS (SELECT ... FROM fast_alerts WHERE fired_at > NOW() - INTERVAL '7 days'),
     books_at_horizon AS (
       SELECT a.id AS alert_id, h.horizon_s,
              (SELECT bid_levels FROM fast_orderbook WHERE ticker=a.ticker AND snapshot_at >= a.fired_at + (h.horizon_s||' seconds')::interval ORDER BY snapshot_at ASC LIMIT 1)
              ...
     )
INSERT INTO fast_signal_decay
SELECT ticker, alert_type, score_bucket, horizon_s,
       COUNT(*), AVG(forward_return), ... -- variance computed
FROM ... ON CONFLICT (...) DO UPDATE SET ...;
```

The exact SQL is your judgment call — aim for one query per (alert_type, horizon) pair or one big roll-up CTE; whichever is cleaner against actual data shape. **Don't** load the alert and book tables fully into Python; the database is faster.

Bound: backfill should complete in <60s on the current dataset (hundreds of alerts × 8 horizons against 100k books). If it takes longer, it's a query-shape problem, not a brain problem.

Commit: `feat(fast-path): F6 cold-start backfill from fast_alerts history`.

### 5. Wire exit_manager + gates to read calibrated values

Once `fast_signal_decay` has data, three magic numbers go away:

- **`max_hold_s`** in exit_manager — replaced by a per-(ticker, alert_type, score_bucket) lookup. Choose the horizon with the highest mean_return / stdev (Sharpe-like) ratio above some minimum sample threshold (e.g., `sample_count >= 30`). If insufficient samples, fall back to the current constant.
- **`MIN_SIGNAL_SCORE`** in gates — derived dynamically. Logic: a score bucket is "tradeable" if `mean_return_at_optimal_horizon > 2 * trading_cost` (account for spread + slippage). The minimum score is the lowest bucket boundary that meets this bar.
- **Bracket geometry** in `stop_engine.compute_initial_bracket` — when called from fast_path, scale stop_pct and target_pct by the empirical stdev of returns at the chosen horizon, not by ATR(14). Keep ATR fallback for cold-start.

Critical: each call site MUST gracefully fall back to the old constant when `fast_signal_decay` has insufficient data. Do not make F6 a hard dependency. The cleanest pattern: a helper `get_calibrated_max_hold_s(ticker, alert_type, score) -> int` that returns the calibrated value or `None` (caller defaults to the constant).

Commit: `feat(fast-path): F6 wire exit_manager + gates to fast_signal_decay`.

## Brain integration (reuse, don't rewrite)

- **`app/services/trading/price_bus.py`** + `app/services/code_dispatch/notifier.py` — reference implementations of LISTEN/NOTIFY pattern in this codebase. Read these first; mirror their connection/listening pattern.
- **`app/services/trading/learning_cycle_steps/`** — for reference only on what the swing-path miner does. **Do not** invoke any of these in event-driven F6 — they're cycle-based and we're explicitly NOT doing that.
- **`app/services/trading/fast_path/supervisor.py`** — boots the new `decay_miner` task alongside ws_client, db_writer, exit_manager. Same lifecycle (start, stop, healthz integration).
- **`app/services/trading/stop_engine.py`** — `compute_initial_bracket()` is the seam where calibrated stop/target geometry replaces ATR-based defaults.
- **`app/migrations.py`** — migration 220 follows existing pattern.

## Constraints / do not touch

- **Live-placement safety belts.** Eight layers, `_place_coinbase_order_live`, etc. Same as always.
- **Default mode stays paper.** F6 changes how the strategy decides exits, not whether it goes live.
- **Cycle-based architecture.** Anywhere you're tempted to write `while True: ... sleep(60)` — STOP. F6 is event-driven by definition.
- **Hardcoded thresholds outside the calibration boundary.** You're replacing `max_hold_s`, `MIN_SIGNAL_SCORE`, and bracket geometry. Don't introduce *new* magic numbers in their place — every threshold inside `decay_miner` should be either (a) derived from the data itself, or (b) a justified design parameter (sample-count thresholds, bucket boundaries) flagged in Open Questions.
- **`models/trading.py` and `.env.example` working-tree changes.** Continue to leave them alone. Not your problem.

## Out of scope

- F7 (Kelly sizing via `position_sizer_model.compute_proposal()`) — separate next task.
- Watchdog task (now genuinely useful for the new decay_miner asyncio task — defer for hardening pass).
- Correlation gate.
- Switching the executor + exit_manager from polling to LISTEN/NOTIFY — they should follow F6's pattern eventually but doing it in this task bloats the scope.
- Any UI surface for `fast_signal_decay`. Internal-only for now; a "what has chili learned?" view is a future task.
- Backtest replay framework. We're learning from live data only.
- Detecting regime changes that invalidate prior calibration. F6 builds the running stats; "the regime shifted, throw out the last 7 days" logic is a follow-up.

## Success criteria

1. Migration 220 applied; `fast_signal_decay` exists with the schema described.
2. NOTIFY emissions verified from psql (LISTEN one session, INSERT another, see notification).
3. `decay_miner` runs as a supervisor-managed asyncio task; `docker compose logs fast-data-worker` shows its metrics line each tick (e.g. `[fast_path] decay_miner alerts=23 exits=3 obs_finalized=88 backfilled=412 pending_heap=240 db_errors=0`).
4. After cold-start backfill, `SELECT COUNT(*) FROM fast_signal_decay` returns >0; rows distributed across multiple `(ticker, alert_type, horizon)` combos.
5. Live operation: insert a fresh `fast_alert` (manually via psql or by waiting), wait 60s, confirm 4 pending observations have been finalized (1s, 5s, 30s, 60s horizons) and Welford'd into the table.
6. exit_manager reads from `fast_signal_decay` for `max_hold_s`; verify by trace-logging the calibrated-vs-fallback decision once per exit.
7. Gates fall back to the old constant when calibrated value is unavailable (cold start).
8. `docs/STRATEGY/CC_REPORTS/<date>_f6-signal-decay-miner.md` written, including: a sample row from `fast_signal_decay` after backfill, a sample log line from `decay_miner`, the verbatim SQL benchmark for "is the strategy actually profitable now."

## Open questions for Cowork (surface in your report only if relevant)

1. **Score bucket boundaries.** I suggested `low: <0.40, med: 0.40–0.65, high: ≥0.65`. If the actual score distribution from existing alerts argues for different cuts (e.g., bucket-by-quantile rather than fixed thresholds), propose them.
2. **Validation residual interpretation.** I defined `realized_validation_residual` as the running mean absolute error of `realized_return_pct/100 - mean_return_at_horizon`. If you have a more useful validation metric (e.g., signed bias, Brier-like for distribution match), propose it — same column, different formula.
3. **Cold-start backfill window.** I said "7 days" (effectively all our data). If you observe the backfill is too slow against actual row counts, propose a tighter window with a follow-up to extend later.
4. **Trading cost estimate** for the score-threshold derivation in subtask 5. Coinbase Advanced Trade fee is 0.4% taker / 0.25% maker / 0.0% maker rebate at certain volume tiers, plus typical spread of 5-10 bps. I'd start with `2 * trading_cost ≈ 100 bps` as the "score bucket must beat this to be tradeable" threshold. If you have a tighter number from somewhere, use it.
5. **Should `decay_miner` write its calibrated values to a derived "advisory" view** that exit_manager / gates read from, OR should it write directly to `fast_signal_decay` and the consumers do their own argmax-over-horizons logic? My instinct: simpler if consumers query the raw table and compute their preferred function (Sharpe, mean, etc.). Open to your design.

## Rollback plan

- Migration 220: cleanly droppable; `DROP TABLE fast_signal_decay CASCADE` removes the table and triggers.
- Trigger-based NOTIFY: idempotently removable by `DROP TRIGGER`.
- `decay_miner.py`: removing the task from supervisor stops the listening; module file can be left in place.
- Calibrated reads in exit_manager + gates: each guarded by a fallback to the existing constant. Reverting the wire-up code restores the prior behavior; the table's existence is harmless.
- Critical: reverts don't affect live placement (which stays gated by the 8 safety belts regardless).
