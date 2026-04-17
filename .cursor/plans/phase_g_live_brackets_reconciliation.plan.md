---
status: completed_shadow_ready
title: Phase G - Live brackets + stop reconciliation (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Objective

Introduce the substrate and observability layer that moves stop / target
enforcement from purely client-side polling (`stop_engine.evaluate_all`)
toward server-side bracket orders at the broker, and add a continuous
reconciliation job that detects drift between local trade state and the
broker's own view (orphaned stops, missed fills, cancelled children,
quantity mismatches).

Like Phases A / C / D / F, this phase ships strictly in **shadow mode**:

* Bracket **intents** are computed and persisted every time a live trade
  opens, with the stop/target the `stop_engine` would have enforced.
* Reconciliation reads real broker orders + positions and compares them
  to local `Trade` and bracket-intent rows, logging drift to a parity
  table.
* **No new broker orders are placed** in this phase. The authoritative
  cutover (actually submitting stop / bracket orders to Robinhood /
  Coinbase / future venues) is deferred to a follow-on G.2 phase that
  reuses this substrate.

This keeps Phase G risk-bounded: worst case is one new periodic read
from brokers plus two new write-only tables. No capital can be lost by
Phase G on its own.

## Why now

* `stop_engine.evaluate_all` is the only line of defense on stop / target
  today. If the chili / brain-worker container is down, the network is
  cut, quotes go stale, or `market_data.fetch_quote` fails fast, an open
  position has **no** broker-side stop. A gap through the stop is
  realized only on the next poll, often minutes later.
* The broker can cancel / modify our children without our knowledge
  (e.g. Robinhood's session expiry cancels working orders). Today we
  have no visibility into this until a user notices something off.
* Phase H (canonical PositionSizer + portfolio optimizer) assumes a
  reliable stop exists. Without bracket + reconciliation, sizing that
  assumes a hard stop at `entry - k*ATR` over-estimates realized edge.

## Scope (allowed changes)

### 1. Schema (migration `133_live_brackets_reconciliation`)

* `trading_bracket_intents` — one row per live trade bracket intent:

  ```
  id BIGSERIAL PRIMARY KEY
  trade_id INT NOT NULL REFERENCES trading_trades(id) ON DELETE CASCADE
  user_id INT NULL
  ticker TEXT NOT NULL
  direction TEXT NOT NULL              -- 'long' | 'short'
  quantity DOUBLE PRECISION NOT NULL
  entry_price DOUBLE PRECISION NOT NULL
  stop_price DOUBLE PRECISION NULL
  target_price DOUBLE PRECISION NULL
  stop_model TEXT NULL                 -- mirrors trade.stop_model
  pattern_id INT NULL                  -- scan_pattern_id at emit time
  regime TEXT NULL                     -- regime_composite at emit time
  intent_state TEXT NOT NULL           -- 'intent' | 'shadow_logged' | 'authoritative_submitted' | 'authoritative_cancelled' | 'reconciled'
  shadow_mode BOOL NOT NULL DEFAULT TRUE
  broker_source TEXT NULL              -- venue we'd have sent to
  broker_stop_order_id TEXT NULL       -- filled only if authoritative
  broker_target_order_id TEXT NULL     -- filled only if authoritative
  last_observed_at TIMESTAMPTZ NULL
  last_diff_reason TEXT NULL
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
  UNIQUE (trade_id)
  INDEX ix_bracket_intents_ticker_state (ticker, intent_state)
  INDEX ix_bracket_intents_updated_at (updated_at)
  ```

* `trading_bracket_reconciliation_log` — one row per reconciliation
  sweep discrepancy (append-only):

  ```
  id BIGSERIAL PRIMARY KEY
  sweep_id TEXT NOT NULL                   -- UUID for the sweep run
  trade_id INT NULL REFERENCES trading_trades(id) ON DELETE SET NULL
  bracket_intent_id BIGINT NULL REFERENCES trading_bracket_intents(id) ON DELETE SET NULL
  ticker TEXT NULL
  broker_source TEXT NULL
  kind TEXT NOT NULL                       -- 'agree' | 'orphan_stop' | 'missing_stop' | 'qty_drift' | 'state_drift' | 'price_drift' | 'broker_down' | 'unreconciled'
  severity TEXT NOT NULL                   -- 'info' | 'warn' | 'error'
  local_payload JSONB NOT NULL DEFAULT '{}'::jsonb
  broker_payload JSONB NOT NULL DEFAULT '{}'::jsonb
  delta_payload JSONB NOT NULL DEFAULT '{}'::jsonb
  mode TEXT NOT NULL                       -- 'shadow' | 'compare' | 'authoritative'
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX ix_bracket_reconciliation_sweep (sweep_id)
  INDEX ix_bracket_reconciliation_trade (trade_id)
  INDEX ix_bracket_reconciliation_kind_ts (kind, observed_at)
  ```

### 2. ORM models (`app/models/trading.py`)

* `BracketIntent`
* `BracketReconciliationLog`

Append at the bottom of the existing `trading.py`; no changes to
existing model classes.

### 3. Pure logic modules

* `app/services/trading/bracket_intent.py`

  * `BracketIntentInput` dataclass (trade snapshot fields + brain
    context + regime).
  * `compute_bracket_intent(trade_input)` - pure function that calls
    the existing `stop_engine` internals to produce
    `(stop_price, target_price, stop_model, reasoning)`. **No DB, no
    broker calls.** This is the single canonical place we decide
    *what* the bracket should be.

* `app/services/trading/bracket_reconciler.py`

  * `ReconciliationInput` dataclass.
  * `classify_discrepancy(local, broker)` - pure function returning
    a `(kind, severity, delta_payload)` tuple. Exhaustive match over
    the enum of kinds.

### 4. DB-aware builders / writers

* `app/services/trading/bracket_intent_writer.py`

  * `upsert_bracket_intent(db, trade, bracket_intent)` - idempotent
    upsert keyed on `trade_id`; writes `intent_state='shadow_logged'`
    when `brain_live_brackets_mode == 'shadow'`.
  * `mark_reconciled(db, intent_id, reason)`.

* `app/services/trading/bracket_reconciliation_service.py`

  * `run_reconciliation_sweep(db, *, user_id=None) -> dict` - reads
    open `Trade` rows, reads broker open orders + positions via
    existing `broker_manager` (Robinhood + Coinbase only for this
    phase; no new venue work), classifies each, writes
    `BracketReconciliationLog` rows, returns summary shape:

    ```
    {
      "sweep_id": str,
      "mode": "shadow" | "compare" | "authoritative",
      "trades_scanned": int,
      "brackets_checked": int,
      "agree": int,
      "orphan_stop": int,
      "missing_stop": int,
      "qty_drift": int,
      "state_drift": int,
      "price_drift": int,
      "broker_down": int,
      "unreconciled": int,
      "took_ms": float,
    }
    ```

  * The sweep **must not submit, cancel, or modify any broker order**
    in this phase. If `brain_live_brackets_mode` is ever read as
    `authoritative` inside this service, it must refuse with a clear
    error.

### 5. Ops log modules (one-line structured emission)

* `app/trading_brain/infrastructure/bracket_intent_ops_log.py`

  * `CHILI_BRACKET_INTENT_OPS_PREFIX = "[bracket_intent_ops]"`
  * `format_bracket_intent_ops_line(...)` producing stable key=value
    tokens (`event=intent_write`, `mode=shadow`, `trade_id=`,
    `ticker=`, `stop=`, `target=`, `broker_source=`).

* `app/trading_brain/infrastructure/bracket_reconciliation_ops_log.py`

  * `CHILI_BRACKET_RECONCILIATION_OPS_PREFIX = "[bracket_reconciliation_ops]"`
  * `format_bracket_reconciliation_ops_line(...)` - one line per
    sweep **summary**, plus one line per non-`agree` entry.

### 6. Wiring (shadow only)

* **Bracket intent emitter** - hook into the existing trade-open code
  path that already sets `trade.stop_loss` / `trade.take_profit` for
  live (non-paper) trades. Candidates (to be confirmed in execute
  step): `app/services/trading/scanner.py`, `app/services/broker_service.py`
  `sync_orders_to_db`, or `app/services/trading/stop_engine.py` on the
  first `_compute_initial_stop` call. Whichever path owns writing the
  initial stop to the `Trade` row is the only call-site that must emit
  a bracket intent. **Exactly one emitter.**

* **Reconciliation job** - new `APScheduler` job in
  `app/services/trading_scheduler.py`, default interval `60s` when
  `brain_live_brackets_mode != 'off'`, gated by the same mode check
  that protects the sweep against accidentally running authoritative.

### 7. Config flags (`app/config.py`)

* `brain_live_brackets_mode: Literal['off','shadow','compare','authoritative'] = 'off'`
* `brain_live_brackets_ops_log_enabled: bool = True`
* `brain_live_brackets_reconciliation_interval_s: int = 60`
* `brain_live_brackets_price_drift_bps: float = 25.0`  # tolerance on
  stop/target price drift before flagging `price_drift`
* `brain_live_brackets_qty_drift_abs: float = 1e-6`     # tolerance on
  quantity drift

### 8. Diagnostics endpoints (`app/routers/trading_sub/ai.py`)

* `GET /api/trading/brain/bracket-intent/diagnostics` - summary of
  last 24h of intents grouped by `intent_state` + `broker_source`.
* `GET /api/trading/brain/bracket-reconciliation/diagnostics` - last
  N sweeps (default 20), with total counts per `kind`, last
  `sweep_id`, last `observed_at`, and the current mode.

Both endpoints return a **frozen shape** (keys + ordering) so release
blockers can assert on them.

### 9. Release blocker scripts

* `scripts/check_live_brackets_release_blocker.ps1` - fails (exit 1)
  if any log line contains `[bracket_intent_ops] event=intent_write mode=authoritative`
  **or** if a bracket intent has been in `intent_state='intent'` for
  more than 10 minutes (unreconciled substrate). Passes (exit 0) in
  pure shadow.
* `scripts/check_bracket_reconciliation_release_blocker.ps1` - fails
  if any line contains `[bracket_reconciliation_ops] event=cancel`
  or `event=submit` in authoritative mode; passes in shadow.

### 10. Tests

* `tests/test_bracket_intent_compute.py` - pure unit tests on
  `compute_bracket_intent`: long vs short, ATR present / missing,
  regime lifecycle factor applied, determinism (same input =>
  identical stop/target).
* `tests/test_bracket_reconciler_classify.py` - every `kind` branch
  of `classify_discrepancy`.
* `tests/test_bracket_intent_writer.py` - DB: upsert idempotency,
  `mark_reconciled`, never overwrites `authoritative_submitted`
  state in shadow mode.
* `tests/test_bracket_reconciliation_service.py` - DB + mock broker:
  synthetic `Trade` + synthetic broker open-orders list produces
  the expected mix of kinds and writes rows to
  `trading_bracket_reconciliation_log`.
* `tests/test_scan_status_brain_runtime.py` - regression: the frozen
  `scan_status` brain_runtime contract stays unchanged (no new keys
  leaking into `brain_runtime`).

### 11. Docker soak

* `scripts/phase_g_soak.py` - inside `chili` container:

  1. Migration `133` applied (`schema_version` has it).
  2. `BRAIN_LIVE_BRACKETS_MODE=shadow` visible in `settings`.
  3. A synthetic live `Trade` emits exactly one `BracketIntent`
     row with `intent_state='shadow_logged'`.
  4. Running the reconciliation service with a mocked broker client
     writes reconciliation rows and returns the frozen summary
     shape.
  5. Diagnostics endpoints return the frozen shape.
  6. Running the sweep twice is idempotent in the sense that
     `agree` counts match and no new intent rows are created for
     unchanged trades.
  7. Forcing `BRAIN_LIVE_BRACKETS_MODE=authoritative` via env
     override inside the service raises and does **not** submit.

### 12. Docs

* `docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md` - mandatory rollout
  ladder `off -> shadow -> compare -> authoritative`, rollback
  procedure, release blockers, and explicit note that authoritative
  requires a follow-on phase (G.2) with its own freeze.

## Forbidden changes (in this phase)

* **Placing any new order (stop, stop-limit, OCO, bracket, trailing)
  at any broker.** Full stop.
* **Cancelling or modifying any existing broker order.** The
  reconciler is **read-only** against the broker.
* Editing the stop/target math in `stop_engine.py`. Bracket intent
  must reuse existing computation.
* Changing the venue adapter protocol (`venue/protocol.py`) to add
  stop primitives. That is G.2's job.
* Editing paper-trading exit logic (Phase B's exit_evaluator is
  authoritative and stays untouched).
* Changing the frozen `scan_status` brain_runtime output shape.
* Editing any Phase A ledger code paths.
* Widening to intercom / chat / UI toast notifications for
  reconciliation events. This phase is observability-only.

## Dependency order (execute in this sequence)

1. Migration `133_live_brackets_reconciliation` + ORM models.
2. Pure modules `bracket_intent.py` + `bracket_reconciler.py` with
   their unit tests.
3. Config flags.
4. Ops log modules.
5. DB writers (`bracket_intent_writer.py`) + their tests.
6. Reconciliation service with a mock broker client + its tests.
7. Exactly one emitter call-site for bracket intents.
8. APScheduler job wired, guarded by `brain_live_brackets_mode`.
9. Diagnostics endpoints.
10. Release blocker scripts.
11. Docker soak `phase_g_soak.py`.
12. Regression: full relevant test run + frozen contract test.
13. `.env` flipped to `BRAIN_LIVE_BRACKETS_MODE=shadow` and
    containers recreated.
14. Docs + closeout.

## Verification gates

* All new unit + DB tests pass.
* `tests/test_scan_status_brain_runtime.py` still green (frozen
  contract untouched).
* Soak script exits 0 with all checks OK.
* Release blocker scripts return exit 0 on synthetic shadow log
  lines and exit 1 on synthetic authoritative log lines.
* `docker compose logs chili --since 30m` shows
  `[bracket_intent_ops] event=intent_write mode=shadow` when a
  live trade emits an intent, and shows
  `[bracket_reconciliation_ops] event=sweep_summary mode=shadow`
  once per scheduler interval.
* No `event=submit` / `event=cancel` lines anywhere.
* Settings dump (`settings.brain_live_brackets_mode`) reads
  `'shadow'` inside the running container.

## Rollback criteria

If the reconciliation sweep produces a sustained `broker_down` rate
above 20% over a rolling 30-minute window, or if
`unreconciled` appears for more than 15 minutes on any single
bracket intent, flip `BRAIN_LIVE_BRACKETS_MODE=off` and recreate
containers. No code rollback is required because shadow never
writes to the broker.

## Non-goals

* Actually placing server-side stops at Robinhood or Coinbase.
  That is Phase G.2.
* Adding stop / stop-limit / OCO primitives to the venue adapter
  protocol. Phase G.2.
* Changing `stop_engine` state-machine behavior, cooldowns, or
  auto-exec. Untouched.
* Moving `paper_trading` to use bracket intents. Untouched.
* Reconciling fills against the economic ledger (Phase A covers
  that on its own path).

## Definition of done

Shadow substrate is running, reconciliation is emitting the frozen
summary once per minute, all tests green, release blockers verified,
diagnostics endpoints return the frozen shape, and the plan closeout
documents the known gaps that Phase G.2 must pick up (authoritative
broker-side placement of brackets, venue adapter protocol
extension, TCA reconciliation against bracket children, multi-leg
OCO semantics).

Phase G ships observability + the data substrate for the future
authoritative cutover, and nothing else.

## Closeout (shipped, verified)

Phase G shipped in shadow mode. Summary of what landed and how it was
verified - no broker writes introduced.

### Shipped

* Migration `133_live_brackets_reconciliation` (PostgreSQL):
  `trading_bracket_intents` + `trading_bracket_reconciliation_log`.
* ORM models `BracketIntent`, `BracketReconciliationLog`
  (`app/models/trading.py`).
* Pure modules (no DB, no broker):
  `app/services/trading/bracket_intent.py::compute_bracket_intent`,
  `app/services/trading/bracket_reconciler.py::classify_discrepancy`.
* DB writers / service (shadow-safe):
  `bracket_intent_writer.py` (idempotent upsert +
  `mark_reconciled` + `bracket_intent_summary`),
  `bracket_reconciliation_service.py` (`run_reconciliation_sweep`
  refuses authoritative, `bracket_reconciliation_summary` for
  diagnostics, `broker_manager_view_fn` = real read-only broker
  position provider).
* Ops logs: `app/trading_brain/infrastructure/bracket_intent_ops_log.py`
  + `bracket_reconciliation_ops_log.py` (stable `event=...` prefixes).
* Emitter call-site: `stop_engine._maybe_emit_bracket_intent` invoked
  exactly once per `evaluate_all` iteration after
  `_apply_stop_to_trade`, gated by `broker_source` (paper trades
  skipped).
* APScheduler job `bracket_reconciliation` registered in
  `trading_scheduler.py` when `brain_live_brackets_mode != off`,
  default interval 60s, refuses to run in `authoritative`.
* Diagnostics endpoints (frozen shape):
  `GET /api/trading/brain/bracket-intent/diagnostics` and
  `GET /api/trading/brain/bracket-reconciliation/diagnostics`.
* Config flags in `app/config.py` + `.env`:
  `BRAIN_LIVE_BRACKETS_MODE=shadow`,
  `BRAIN_LIVE_BRACKETS_OPS_LOG_ENABLED=true`,
  `BRAIN_LIVE_BRACKETS_RECONCILIATION_INTERVAL_S=60`,
  `BRAIN_LIVE_BRACKETS_PRICE_DRIFT_BPS=25.0`,
  `BRAIN_LIVE_BRACKETS_QTY_DRIFT_ABS=0.000001`.
* Release blocker scripts:
  `scripts/check_live_brackets_release_blocker.ps1`,
  `scripts/check_bracket_reconciliation_release_blocker.ps1` with
  diagnostics-JSON gates and smoke-verified synthetic pass/fail.
* Docker soak: `scripts/phase_g_soak.py` - all 20 in-container
  checks pass (migration, shadow-mode settings, emitter
  idempotency, agree sweep, diagnostics shape, authoritative
  refusal).
* Docs: `docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md`.

### Tests (pytest)

* `tests/test_bracket_intent_compute.py` - 15 pure-unit tests (pass).
* `tests/test_bracket_reconciler_classify.py` - 16 classifier tests
  covering every kind branch (pass).
* `tests/test_bracket_intent_writer.py` - 8 DB integration tests
  (pass).
* `tests/test_bracket_reconciliation_service.py` - 9 DB + mock broker
  tests (pass).
* `tests/test_scan_status_brain_runtime.py` - 2/2 pass (frozen
  contract not extended by Phase G).

### Runtime evidence

* `.env` flipped to `BRAIN_LIVE_BRACKETS_MODE=shadow`; chili +
  brain-worker + scheduler-worker recreated via
  `docker compose up -d --force-recreate`.
* `BRAIN_LIVE_BRACKETS_MODE=shadow` visible inside the chili
  container.
* Soak inside chili container: `phase_g_soak.py` SUCCESS.
* Live sweep from `scheduler-worker`: APScheduler registered
  "Bracket reconciliation sweep (every 60s; mode=shadow)" job and
  emitted `[bracket_reconciliation_ops] event=sweep_summary
  mode=shadow ... trades_scanned=102 agree_count=70 qty_drift=32
  broker_down=0`, confirming the real `broker_manager_view_fn` wires
  through and the sweep surfaces true reconciliation findings
  without any broker writes.
* Release blocker scripts exit 0 against live
  `docker compose logs scheduler-worker --since 5m`.

### Known gaps (deferred to Phase G.2)

* Venue adapter protocol extension for stop / stop-limit / OCO /
  bracket primitives (Robinhood + Coinbase + Kraken).
* Authoritative writer path that submits, cancels, and modifies
  bracket children, guarded by governance kill-switch + Phase I
  risk dial.
* Reading broker open child orders (not just positions) - Phase G
  reads `get_combined_positions` only; Phase G.2 needs a real
  orders-side reader per venue.
* TCA reconciliation against bracket children (fills, partials,
  replacements) and feedback into Phase F venue-truth telemetry.
* Multi-leg OCO semantics (cancel-on-fill of sibling legs).
* Per-broker bracket policy (Robinhood lacks native OCO; Coinbase
  supports stop orders but not brackets natively).

### Self-critique

* The emitter relies on `stop_engine.evaluate_all` producing an
  alert-bearing result; a trade that never trips `STOP_TIGHTENED` /
  `BREAKEVEN_REACHED` / similar events will not seed a bracket
  intent. In practice `_apply_stop_to_trade` runs on initial-stop
  computation because that's where `alert_event=STOP_TIGHTENED` is
  set, but this coupling should be re-examined in Phase G.2 when
  we need a bracket on every live position regardless of alert
  event.
* `broker_manager_view_fn` only reports position quantity; without
  a per-venue stop-orders reader every open position with a
  bracket intent will classify as `missing_stop` in Phase G. That
  is truthful for shadow (we really do not have a broker-side
  stop), but the `missing_stop` count should be expected to be
  large and is not a regression signal on its own. Phase G.2 must
  wire the orders reader before treating `missing_stop` as a
  rollback trigger.
* Phase G does not move authoritative state on its own. We
  explicitly enforce this by raising in the reconciliation service
  when `mode=authoritative` is set. The only way to bypass is to
  merge Phase G.2.

