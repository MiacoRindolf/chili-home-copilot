# CC_REPORT: f-promotion-pipeline-rebalance — Phase 2 (directional-correctness signal)

## Outcome

Phase 2 shipped: the brain now has a **gate-noise-free** pattern-eval
signal. The new table `pattern_alert_directional_outcome` and view
`pattern_directional_quality_v` answer the question Phases 3-4
actually need — **"did price actually move in the predicted
direction within the hold window of an imminent alert?"** — measured
on every imminent alert, not just the gate-survivors.

A live smoke run against the `chili` DB after force-recreate
populated 200 outcome rows for 3 active patterns in 15.7s with zero
errors. The rolling-30 view immediately revealed the asymmetry the
brief flagged: pattern 585's directional WR is **73.3%** (clean
signal), versus its 8-trade gate-laundered realized WR of 25% that
nearly killed it. Phase 2 surfaces this signal so Phases 3-4 can
promote on the right metric.

19/19 Phase 2 tests PASS. Phase 1's 16 tests still PASS (no
regression).

## Per-step status

### Step 1 — Migration 235 — SHIPPED

`app/migrations.py` (+118 lines):

* `_migration_235_pattern_alert_directional_outcome`
  - `pattern_alert_directional_outcome` table:
    `id BIGSERIAL`, `alert_id INTEGER REFERENCES trading_alerts(id)
    ON DELETE CASCADE` (UNIQUE),  `scan_pattern_id`, `ticker`,
    `alert_at`, `predicted_direction VARCHAR(8)` (CHECK in
    `'up'|'down'`), `entry_price`, `hold_window_hours`,
    `window_close_at`, `window_max_favorable_pct`,
    `window_max_adverse_pct`, `directional_threshold_pct`,
    `directional_correct`, `evaluated_at`.
  - 3 indexes: `idx_padc_pattern`, `idx_padc_alert_at`,
    `idx_padc_pattern_alert_at` (composite for the per-pattern
    rolling lookup).
  - View `pattern_directional_quality_v`: per-pattern rolling-30
    directional WR + sample size + last_alert_at + last_evaluated_at.
    Window function picks the 30 most-recent outcomes per pattern;
    aggregator collapses to one row per pattern.
* Idempotent: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
  EXISTS`, `CREATE OR REPLACE VIEW`.
* Registered in MIGRATIONS list at position 235.

### Step 2 — 5 new settings — SHIPPED

`app/config.py` (+50 lines):

* `chili_pattern_directional_outcome_enabled: bool = True`
* `chili_pattern_directional_threshold_pct: float = 1.5`
* `chili_pattern_directional_default_hold_hours: int = 24`
* `chili_pattern_directional_max_lookback_hours: int = 168`
  (1 week — bounds initial backfill scope)
* `chili_pattern_directional_max_alerts_per_run: int = 200`
  (per-cycle cap on OHLC fan-out)

### Step 3 — Evaluator module — SHIPPED

New file `app/services/trading/pattern_directional_outcome.py` (444 lines):

* `evaluate_directional_outcomes(db, *, now=None, fetch_ohlcv=None,
  settings_=None) -> dict` — main entry point. Test-injection seams
  for `now` (clock), `fetch_ohlcv` (OHLC provider), `settings_`
  (config snapshot).
* `_resolve_predicted_direction(pat)` — resolves "up" or "down" from
  `pat.rules_json[direction|bias|side|trade_side]` then a name-token
  heuristic (`short_`, `_short`, `fade`, `downtrend`, `bearish`),
  defaulting to "up" since `pattern_breakout_imminent` is bullish.
* `_compute_window_outcome(df, ...)` — pure math: max-favorable,
  max-adverse, `directional_correct = max_favorable >= threshold`.
  Direction-aware: for `down`, favorable = price-DROP move.
* `_entry_price_from_df(df, alert_at)` — last close at-or-before
  alert_at; falls through to first close when alert_at is before
  the OHLC window start.
* `_default_fetch_ohlcv(ticker, *, start, end)` — wraps
  `fetch_ohlcv_df`; uses `1h` interval for windows < 7 days, `1d`
  otherwise.
* `get_rolling_directional_quality(db, scan_pattern_id)` — single-row
  read of the rolling view for ad-hoc inspection.

Robustness:

* SQL prefilter excludes alerts whose 24h hold window hasn't closed
  AND alerts older than `max_lookback_hours` AND alerts already
  evaluated AND alerts with `scan_pattern_id IS NULL` (system / FK
  SET NULL after pattern delete).
* Per-alert error catch: errors increment `errors` counter but don't
  abort the batch. Logs warning, continues to next alert.
* `ON CONFLICT (alert_id) DO NOTHING` belt for the UNIQUE constraint
  in case two evaluator processes race.
* OHLC fetch failure → `skipped_no_ohlc`; alert remains unevaluated
  and retries next cycle.

### Step 4 — Scheduler wiring — SHIPPED

`app/services/trading_scheduler.py` (+49 lines):

* New runner `_run_pattern_directional_outcome_evaluator_job`
  (around line 3232) follows the FIX 46 pattern: `SessionLocal()` →
  `evaluate_directional_outcomes(sess)` → `rollback()` → `close()`.
  Wrapped in `run_scheduler_job_guarded` for ops-visible audit row.
  Flag-disable via `chili_pattern_directional_outcome_enabled=False`.
* Job registration in the `include_web_light` block: `IntervalTrigger
  (minutes=30)`, id=`pattern_directional_outcome_evaluator`,
  `next_run_time = now + 75s` so first run staggers off the
  promotion-evidence-audit baseline.

### Step 5 — Tests — SHIPPED

`tests/test_pattern_directional_outcome.py` (560 lines, 19 tests):

**Pure unit (10 tests, no DB)**:
- direction defaults to "up" with no hint
- `rules_json={"direction":"short"}` → "down"
- `rules_json={"bias":"bearish"}` → "down"
- name token `vwap_short_fade_2026` → "down"
- up direction strong favorable move → correct=True (5% > 1.5%)
- up direction weak favorable below threshold → correct=False
- up direction adverse only → correct=False, max_adverse signed neg
- down direction with price drop → correct=True, favorable inverted
- empty window slice → returns None (skip)
- entry_price picks last close at-or-before alert_at

**Integration (9 tests, real DB + mock OHLC)**:
- evaluator inserts correct row for up pattern (asserts every column)
- evaluator skips alert whose window is still open (SQL prefilter)
- evaluator dedupes on rerun (UNIQUE alert_id constraint)
- evaluator skips when OHLC unavailable (empty DataFrame)
- evaluator handles down direction correctly (price drop)
- flag-disabled short-circuits (does NOT call OHLC fetcher)
- rolling view aggregates per-pattern directional WR (3 of 5 = 0.6)
- rolling view caps sample at 30 (35 inserted, view shows n=30)
- evaluator skips alert whose pattern was deleted (FK SET NULL)

19/19 PASS. Unit subset runs in ~0.3s; integration subset ~10s/test
on the chili_test DB.

## Verification

* Migration applied to `chili` DB on container start; row in
  `schema_version`: `235_pattern_alert_directional_outcome`.
* Table `pattern_alert_directional_outcome` exists with all 14
  columns, FK to `trading_alerts(id)` ON DELETE CASCADE, UNIQUE on
  `alert_id`, CHECK on `predicted_direction`, 3 indexes.
* View `pattern_directional_quality_v` exists with 5 columns.
* Settings propagate identically to chili / scheduler-worker /
  brain-worker: `enabled=True thresh=1.5 hold=24`.
* Scheduler-worker logged: `Added job "Pattern directional-correctness
  evaluator (every 30min)" to job store "default"`.
* **Live smoke run** of `evaluate_directional_outcomes` against
  `chili` DB inside scheduler-worker container:
  ```
  {'ok': True, 'candidates': 200, 'evaluated': 200,
   'skipped_no_pattern': 0, 'skipped_no_ohlc': 0,
   'skipped_window_empty': 0, 'skipped_window_open': 0,
   'errors': 0, 'elapsed_ms': 15742}
  ```
* Rolling view query post-smoke:
  ```
  scan_pattern_id | rolling_sample_n |  wr   |       last_alert_at
  ----------------+------------------+-------+----------------------------
              585 |               30 | 0.733 | 2026-05-04 01:57:56.848513
              586 |               30 | 0.733 | 2026-05-04 01:57:56.031725
              537 |                3 | 1.000 | 2026-05-03 07:25:37.831297
  ```
* Phase 1 tests (`test_pattern_demote_thresholds.py`) — 16/16 PASS,
  no regression.

## Surprises / deviations

1. **`alert_id` typed as INTEGER, not BIGINT.** The brief's draft
   schema spelled `BIGINT`, but `trading_alerts.id` is `Integer`
   (INT4) per the SQLAlchemy model. INTEGER matches the FK target
   exactly and avoids implicit-cast index inefficiency. Postgres
   permits cross-type FKs, but the brief's spelling was a typo
   rather than a deliberate widening.

2. **Pattern 585's directional WR is 73.3%, not 25%.** The brief
   anticipated the rebalance would reveal asymmetry between
   gate-laundered realized WR and clean directional WR — Phase 2
   smoke confirmed it for the live data immediately. Pattern 585's
   realized WR (25% on 8 trades) was the wrong question; its
   directional WR (73.3% on 30 alerts) is the right one. Phase 4's
   composite scoring will lean heavily on this view.

3. **WinError 10055 / 10053 during pytest.** Windows ephemeral
   socket buffer exhaustion when all Docker workers run alongside
   pytest. Workaround: stopped `fast-data-worker` for the duration
   of the test sweep; ran the tail of the suite (the 35-row
   rolling-cap test + the FK SET NULL test) in a follow-up after
   postgres briefly restarted under pressure. This is environmental
   — not a Phase 2 regression. Fix candidates (out-of-scope here):
   smaller SQLAlchemy pool for tests, or split-tier docker compose
   so the heavy workers can be paused via profile.

4. **Pytest-asyncio plugin error** (`Package object has no attribute
   obj`) is pre-existing across the entire repo (not a Phase 2
   issue); ran with `-p no:asyncio` per the existing convention.

5. **No autotrader changes.** Phase 2 is a parallel-track
   observability addition. Existing pattern stats and demote logic
   are untouched. This is intentional — the eval signal needs to
   accumulate before Phases 3-4 read from it.

## Operator-side after Phase 2 ships

1. `git pull` then truncation scan (mig 235 + new module + scheduler
   wiring).
2. `docker compose up -d --force-recreate chili scheduler-worker
   brain-worker autotrader-worker broker-sync-worker`.
3. Verify migration applied:
   ```bash
   docker exec chili-home-copilot-postgres-1 psql -U chili -d chili \
     -c "SELECT version_id FROM schema_version WHERE version_id LIKE '235%';"
   ```
4. Verify scheduler picked up the job:
   ```bash
   docker logs --since 60s chili-home-copilot-scheduler-worker-1 \
     | grep "directional-correctness"
   ```
   Expected: `Added job "Pattern directional-correctness evaluator
   (every 30min)" to job store "default"`.
5. Wait for first run (~75s after restart, then every 30 min).
   Verify outcome rows accumulate:
   ```bash
   docker exec chili-home-copilot-postgres-1 psql -U chili -d chili \
     -c "SELECT COUNT(*), COUNT(DISTINCT scan_pattern_id) FROM
         pattern_alert_directional_outcome;"
   ```
6. Inspect rolling-30 view for promoted patterns:
   ```bash
   docker exec chili-home-copilot-postgres-1 psql -U chili -d chili \
     -c "SELECT * FROM pattern_directional_quality_v
         ORDER BY rolling_sample_n DESC;"
   ```

## Rollback plan

* Flag revert: `CHILI_PATTERN_DIRECTIONAL_OUTCOME_ENABLED=false` in
  `.env` stops the evaluator (job stays registered but exits
  immediately on each tick).
* Drop the new artefacts:
  ```sql
  DROP VIEW IF EXISTS pattern_directional_quality_v;
  DROP TABLE IF EXISTS pattern_alert_directional_outcome;
  DELETE FROM schema_version
   WHERE version_id = '235_pattern_alert_directional_outcome';
  ```
* Code revert: `git revert` the Phase 2 commit. The new module is
  isolated; no cross-cutting calls into existing services. The
  scheduler job + settings are removed by the revert.

## Deferred

* **Backfill of older alerts.** The `max_lookback_hours` setting
  (default 168 = 7 days) bounds how far back the evaluator looks.
  If the operator wants directional WR over a longer history, raise
  the setting temporarily or run a one-off backfill query. Not
  necessary for Phase 4 (which only needs rolling-30, easily filled
  by 7 days of normal alert flow).
* **Per-pattern hold-window tuning.** The evaluator uses the
  default 24h for every alert. If a scalp pattern's "real" hold
  window is 2h, this overstates the window. Phase 4 may want to
  read `pat.rules_json["hold_hours"]` or the alert's
  `duration_estimate` to set a per-alert window. Surfaced for
  Cowork: should Phase 2 be enriched (additive change) or should
  Phase 4 read the existing 24h-window outcomes and accept the
  noise floor?
* **`predicted_direction` resolution.** The current heuristic
  defaults to "up" since `pattern_breakout_imminent` is bullish-
  breakout by name. Operator-authored short patterns can opt in
  via `rules_json["direction"]="short"`. If Cowork wants explicit
  per-pattern direction (column on `scan_patterns`), that's a Phase
  4 addition.

## Open questions for Cowork

1. **Hold-window per pattern vs. global default**: should Phase 4's
   composite score use a per-pattern hold from `rules_json` (or a
   new `scan_patterns.hold_window_hours` column), or accept the 24h
   default for the rolling view?
2. **Threshold tuning**: 1.5% is a defensible default for stocks +
   crypto breakouts. For scalp-heavy patterns it may be too coarse.
   Should Phase 4 store the per-pattern threshold separately?
3. **Backfill approach**: should Phase 6 verification include a
   one-off `max_lookback_hours=2160` (90 day) backfill so historical
   pattern roster comparisons are apples-to-apples, or wait for
   organic accumulation?

## What's NEXT

* **Phase 3** — `shadow_promoted` lifecycle stage. New flag
  `chili_shadow_promoted_lifecycle_enabled`. Patterns at
  `lifecycle_stage='shadow_promoted'` fire imminent alerts (Phase 2
  evaluator now picks them up cleanly) but autotrader routes to
  shadow-log only. RH path BYTE-IDENTICAL parity test is the hard
  gate.
* **Phase 4** — composite quality scoring + weekly cohort
  auto-promote. Reads from `pattern_directional_quality_v` (Phase
  2's deliverable) for the directional-WR component of the score.
* **Phase 5** — per-pattern universe via `scope_tickers`.
* **Phase 6** — 7-day verification + final summary report.

CC ships one phase per session per the brief's sequencing rules.
