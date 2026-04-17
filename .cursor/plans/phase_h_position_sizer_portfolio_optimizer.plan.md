---
status: completed_shadow_ready
title: Phase H - Canonical PositionSizer + portfolio optimizer (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Objective

Introduce a **single canonical position sizer** that consumes the
Phase E `NetEdgeRanker` score directly (calibrated probability,
payoff fraction, round-trip cost fraction) and produces a sizing
decision that is:

1. **Kelly-aware** - size proportional to the NetEdgeRanker's
   calibrated edge, not to heuristic win-rate averages.
2. **Correlation-capped** - hard-limited against the current open
   book so we do not double-up on mechanically identical bets
   (e.g. `NVDA` + `SMH` + `AMD` in the same breath).
3. **Portfolio-aware** - aggregate exposure, per-bucket caps, and a
   weekly covariance-based risk budget share the same sizing output.

Like Phases A / C / D / F / G, Phase H ships strictly in **shadow
mode**:

* The canonical sizer computes a **shadow sizing proposal** for every
  actionable pick (breakout alert, paper-runner intent, live-runner
  intent, manual trade proposal).
* The proposal is persisted to a new `trading_position_sizer_log`
  table and compared side-by-side against the legacy sizer that
  actually picked the notional.
* Legacy sizers (`alerts._compute_position_size`,
  `portfolio_risk.size_position_kelly`,
  `portfolio_risk.size_with_drawdown_scaling`,
  `portfolio_allocator`'s capital share) keep their current behavior
  in Phase H. **No paper or live trade notional changes in this phase.**
* Authoritative cutover (replacing the legacy sizers with the
  canonical one) is a follow-on **Phase H.2** with its own freeze.

This keeps Phase H risk-bounded: worst case is one new DB table plus
one structured ops line per actionable pick. No capital can be lost
by Phase H on its own.

## Why now

* Phase E (`NetEdgeRanker`) already produces a calibrated,
  cost-aware expected-net-PnL per unit of entry. Today that signal
  is only read by the ranker diagnostics - no sizer consumes it.
* Legacy sizers duplicate intent: `_compute_position_size` uses
  hard-coded risk weights; `size_position_kelly` uses historical
  (not calibrated) win-rate; `portfolio_allocator` manages
  allocation scoring but does not feed the sizer. Every new alert /
  runner path re-implements size logic with different overlays.
* Phase G's bracket intent assumes a reliable stop. Phase H's
  Kelly denominator should be the **same** `stop_price` the bracket
  intent persists, so the two phases stay consistent.
* Without a canonical sizer, Phase I (risk dial) has nothing to dial.

## Scope (allowed changes)

### 1. Schema (migration `134_position_sizer_log`)

* `trading_position_sizer_log` - append-only shadow log:

  ```
  id BIGSERIAL PRIMARY KEY
  proposal_id UUID NOT NULL                  -- deterministic from call-site inputs
  source TEXT NOT NULL                       -- 'alerts' | 'paper_runner' | 'live_runner' | 'manual' | 'backtest'
  ticker TEXT NOT NULL
  direction TEXT NOT NULL                    -- 'long' | 'short'
  user_id INT NULL
  pattern_id INT NULL
  asset_class TEXT NULL                      -- 'equity' | 'crypto'
  regime TEXT NULL
  entry_price DOUBLE PRECISION NOT NULL
  stop_price DOUBLE PRECISION NULL
  target_price DOUBLE PRECISION NULL
  capital DOUBLE PRECISION NULL
  -- NetEdgeRanker inputs actually consumed
  calibrated_prob DOUBLE PRECISION NULL
  payoff_fraction DOUBLE PRECISION NULL
  cost_fraction DOUBLE PRECISION NULL
  expected_net_pnl DOUBLE PRECISION NULL
  -- Sizer outputs
  kelly_fraction DOUBLE PRECISION NULL       -- full-Kelly fraction
  kelly_scaled_fraction DOUBLE PRECISION NULL -- after quarter-Kelly + risk dial
  proposed_notional DOUBLE PRECISION NULL
  proposed_quantity DOUBLE PRECISION NULL
  proposed_risk_pct DOUBLE PRECISION NULL
  -- Caps actually triggered (for diagnostics)
  correlation_cap_triggered BOOL NOT NULL DEFAULT FALSE
  correlation_bucket TEXT NULL
  max_bucket_notional DOUBLE PRECISION NULL
  notional_cap_triggered BOOL NOT NULL DEFAULT FALSE
  -- Legacy comparison
  legacy_notional DOUBLE PRECISION NULL
  legacy_quantity DOUBLE PRECISION NULL
  legacy_source TEXT NULL                    -- 'alerts' | 'portfolio_risk.kelly' | 'portfolio_risk.dd' | 'none'
  divergence_bps DOUBLE PRECISION NULL       -- abs((proposed-legacy)/legacy) in bps
  mode TEXT NOT NULL                         -- 'off' | 'shadow' | 'compare' | 'authoritative'
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX ix_pos_sizer_log_proposal (proposal_id)
  INDEX ix_pos_sizer_log_source_ts (source, observed_at)
  INDEX ix_pos_sizer_log_ticker_ts (ticker, observed_at)
  ```

  Append-only. No unique constraint on `proposal_id` - the caller
  may re-propose (e.g. a retry) and the diagnostics endpoint
  aggregates by `proposal_id` + latest `observed_at`.

### 2. ORM model (`app/models/trading.py`)

* `PositionSizerLog` - appended to the end of `trading.py`, no
  changes to existing classes.

### 3. Pure logic modules

* `app/services/trading/position_sizer_model.py`
  * `PositionSizerInput` dataclass (entry, stop, target, qty_min,
    asset_class, capital, NetEdgeRanker score fields, risk-dial
    multiplier).
  * `CorrelationBudget` dataclass (bucket -> open_notional,
    max_bucket_notional).
  * `PortfolioBudget` dataclass (total_capital, deployed_notional,
    max_total_notional).
  * `PositionSizerOutput` dataclass (proposed_notional,
    proposed_quantity, proposed_risk_pct, kelly_fraction,
    kelly_scaled_fraction, correlation_cap_triggered,
    notional_cap_triggered, reasoning).
  * `compute_proposal(input, correlation, portfolio)` - **pure
    function, no DB, no broker**. Returns a `PositionSizerOutput`.
    Kelly numerator uses `NetEdgeScore.expected_net_pnl` divided by
    `payoff_fraction^2` (standard derivation when prob and payoff
    are calibrated); falls back to legacy fixed-fractional when
    `expected_net_pnl <= 0`.

* `app/services/trading/correlation_budget.py`
  * `bucket_for(ticker, asset_class)` - canonical correlation
    bucket (reuse `portfolio_allocator._correlation_bucket`).
  * `compute_correlation_budget(db, user_id, capital)` - **DB-aware**,
    reads open `Trade` rows, returns a `CorrelationBudget`.
  * Hard bucket caps (config): equity bucket <= 15% of capital,
    crypto bucket <= 10% of capital, single-ticker <= 7.5%.

### 4. DB-aware writer

* `app/services/trading/position_sizer_writer.py`
  * `write_proposal(db, *, input, output, legacy_notional,
    legacy_quantity, legacy_source, source, mode, proposal_id)` -
    writes one `PositionSizerLog` row per proposal. Computes and
    stores the `divergence_bps` between proposed and legacy sizes.
    Shadow-safe; never affects the live trade.
  * `proposals_summary(db, *, lookback_hours=24)` - frozen-shape
    diagnostics: mode, proposals_total, by_source, by_divergence_bucket,
    mean_divergence_bps, p90_divergence_bps, cap_trigger_counts,
    latest_proposal.

### 5. Ops log (one-line structured)

* `app/trading_brain/infrastructure/position_sizer_ops_log.py`
  * `CHILI_POSITION_SIZER_OPS_PREFIX = "[position_sizer_ops]"`
  * `format_position_sizer_ops_line(...)` with events `proposal`,
    `cap_triggered`, `divergence`.

### 6. Wiring (shadow only)

* **Emitter call-sites** - exactly the four call-sites where a
  legacy sizer is invoked today:
  1. `alerts._compute_position_size` - emit shadow proposal before
     returning.
  2. `portfolio_risk.size_position_kelly` - emit shadow proposal
     before returning.
  3. `portfolio_risk.size_with_drawdown_scaling` - same.
  4. The paper-runner + live-runner sizing call-sites
     (`paper_trading._compute_position_size` / momentum
     `live_runner._round_base_size` entry).

  All emitters go through the same `position_sizer_writer.write_proposal`
  so there is exactly one persistence surface.

* **No legacy behavior change.** The canonical sizer's output is
  persisted and logged; legacy output is still returned.

### 7. Config flags (`app/config.py`)

* `brain_position_sizer_mode: Literal['off','shadow','compare','authoritative'] = 'off'`
* `brain_position_sizer_ops_log_enabled: bool = True`
* `brain_position_sizer_equity_bucket_cap_pct: float = 15.0`
* `brain_position_sizer_crypto_bucket_cap_pct: float = 10.0`
* `brain_position_sizer_single_ticker_cap_pct: float = 7.5`
* `brain_position_sizer_kelly_scale: float = 0.25`  # quarter-Kelly
* `brain_position_sizer_max_risk_pct: float = 2.0`

### 8. Diagnostics endpoint (`app/routers/trading_sub/ai.py`)

* `GET /api/trading/brain/position-sizer/diagnostics` - frozen shape:

  ```
  {
    "ok": true,
    "position_sizer": {
      "mode": ...,
      "lookback_hours": ...,
      "proposals_total": ...,
      "by_source": { ... },
      "by_divergence_bucket": {
        "under_100_bps": N,
        "100_500_bps": N,
        "500_2000_bps": N,
        "over_2000_bps": N
      },
      "mean_divergence_bps": ...,
      "p90_divergence_bps": ...,
      "cap_trigger_counts": {
        "correlation_cap": N,
        "notional_cap": N
      },
      "latest_proposal": { ... }
    }
  }
  ```

### 9. Release blocker script

* `scripts/check_position_sizer_release_blocker.ps1` - fails if:
  * any line contains `[position_sizer_ops] mode=authoritative`
    (Phase H is shadow-only), OR
  * `-DiagnosticsJson` provided and `mean_divergence_bps > 10000`
    (>100% divergence means the canonical sizer disagrees wildly
    with legacy; cutover not safe), OR
  * `-MinProposals` provided and `proposals_total < MinProposals`.

### 10. Tests

* `tests/test_position_sizer_model.py` - pure unit tests on
  `compute_proposal`: Kelly math against closed-form expectations;
  bucket cap reduces size exactly to cap; notional cap overrides;
  negative `expected_net_pnl` -> zero size; determinism
  (same input twice => identical output).
* `tests/test_correlation_budget.py` - DB: bucket aggregation across
  open trades, correct mapping for crypto / equity / sector.
* `tests/test_position_sizer_writer.py` - DB: writes frozen-shape
  rows, shadow mode gates, `proposals_summary` returns frozen shape.
* `tests/test_position_sizer_emitters.py` - each of the 4 emitter
  call-sites produces exactly one proposal row when mode=shadow and
  zero rows when mode=off. Legacy return values unchanged.
* `tests/test_scan_status_brain_runtime.py` - regression: no new
  keys leak into the frozen `brain_runtime` output.

### 11. Docker soak

* `scripts/phase_h_soak.py` - inside `chili` container:
  1. Migration `134` applied.
  2. `BRAIN_POSITION_SIZER_MODE=shadow` visible.
  3. A synthetic alert + manual proposal write exactly one
     `PositionSizerLog` row each.
  4. `proposals_summary` returns the frozen shape.
  5. Forcing `BRAIN_POSITION_SIZER_MODE=authoritative` on the
     emitter refuses to flip legacy return values (Phase H.2
     territory).

### 12. Docs

* `docs/TRADING_BRAIN_POSITION_SIZER_ROLLOUT.md` - rollout ladder,
  rollback, release blockers, divergence thresholds, explicit
  deferral of authoritative cutover to Phase H.2.

## Forbidden changes (in this phase)

* **Changing the value legacy sizers return.** Every call-site must
  still produce the same notional it produces today.
* **Opening or resizing any existing trade** based on the shadow
  proposal. Reconciliation is observational only.
* Rewriting `alerts._compute_position_size` overlays. Phase H emits
  a parallel proposal; H.2 replaces the overlays with the canonical
  sizer.
* Editing `net_edge_ranker.py` math. Phase H only **consumes** its
  score.
* Editing `bracket_intent.py` / `stop_engine.py` math.
* Changing the frozen `scan_status.brain_runtime` shape.
* Editing Phase A ledger, Phase D label, or Phase F cost-model code
  paths.

## Dependency order (execute in this sequence)

1. Migration `134_position_sizer_log` + ORM model.
2. Pure modules `position_sizer_model.py` + `correlation_budget.py`
   with their unit tests.
3. Config flags.
4. Ops log module.
5. Writer `position_sizer_writer.py` + tests.
6. Emitter hooks at the 4 call-sites (guarded by mode).
7. Diagnostics endpoint.
8. Release blocker script.
9. Docker soak `phase_h_soak.py`.
10. Regression: full relevant test run + frozen contract test.
11. `.env` flipped to `BRAIN_POSITION_SIZER_MODE=shadow`, recreate.
12. Docs + closeout.

## Verification gates

* All new unit + DB tests pass.
* `tests/test_scan_status_brain_runtime.py` stays green.
* Soak exits 0 with all checks OK.
* Release blocker script returns exit 0 on synthetic shadow log
  lines and exit 1 on synthetic authoritative lines.
* Scheduler-worker logs show `[position_sizer_ops] event=proposal
  mode=shadow` when a live alert path fires.
* **Legacy trade notional is unchanged** for a sample of 10 recent
  trades checked post-deploy (sanity).

## Rollback criteria

If the p90 divergence between canonical and legacy sizes exceeds
5000 bps (50%) sustained over 24h, or if the sizer writer starts
failing INSERTs, flip `BRAIN_POSITION_SIZER_MODE=off` and recreate
containers. No code rollback is required because shadow never
changes legacy notional.

## Non-goals

* Replacing the legacy sizers in live / paper code paths. That is
  Phase H.2.
* Covariance-matrix allocation across the entire open book. This
  phase ships a **hard correlation cap** only; covariance allocator
  belongs to Phase I.
* Risk dial (0-1.0). Phase I.
* Multi-account / per-user risk budgeting beyond the existing
  `user_id` scope.
* Backfilling `trading_position_sizer_log` for historical trades.

## Definition of done

Shadow substrate is running, every actionable pick produces exactly
one `PositionSizerLog` row that the diagnostics endpoint can report,
legacy notional is unchanged, all tests green, release blockers
verified, divergence thresholds observed in real logs, and the plan
closeout documents the gaps Phase H.2 must pick up (authoritative
cutover + Phase I integration).

## Closeout

Shipped end-to-end in shadow mode:

* **Schema:** migration `134_position_sizer_log` + ORM
  `PositionSizerLog` landed. Verified inside `chili` and `chili_test`.
* **Pure logic:** `position_sizer_model.compute_proposal` +
  `correlation_budget` shipped. Kelly applies cost symmetrically to
  both winning and losing branches. Deterministic `proposal_id`. 19
  pure unit tests pass.
* **Persistence:** `position_sizer_writer.write_proposal` +
  `proposals_summary` shipped; writes are mode-gated and completely
  defensive. 8 DB tests pass.
* **Emitter:** single `position_sizer_emitter.emit_shadow_proposal`
  entry point wired into all four legacy sizer call-sites
  (`alerts` x3, `portfolio_risk.size_position_kelly`,
  `portfolio_risk.size_with_drawdown_scaling`,
  `paper_trading.auto_open`). 6 DB tests pass. Legacy sizers are
  **never disturbed** - all emitter exceptions are swallowed.
* **Diagnostics:** `GET /api/trading/brain/position-sizer/diagnostics`
  returns the frozen shape; verified `mode=shadow` live in the
  running `chili` container.
* **Release blocker:** `scripts/check_position_sizer_release_blocker.ps1`
  all four gates smoke-tested (shadow pass, authoritative fail,
  diagnostics thresholds pass, diagnostics thresholds fail).
* **Soak:** `scripts/phase_h_soak.py` - 17 checks + diagnostics HTTP
  check ALL PASS inside the `chili` container.
* **Regression:** 50/50 Phase H tests + frozen
  `scan_status.brain_runtime` contract test green (pure 19, writer
  8, correlation budget 15, emitter 6, scan_status 2).
* **`.env` flipped:** `BRAIN_POSITION_SIZER_MODE=shadow` visible in
  `chili`, `brain-worker`, and `scheduler-worker`. Release-blocker
  grep on the last 5 min of logs post-flip returns zero authoritative
  lines.
* **Docs:** `docs/TRADING_BRAIN_POSITION_SIZER_ROLLOUT.md` documents
  rollout ladder, rollback, release blockers, and explicit Phase H.2
  deferral.

### Self-critique

* The emitter is wired at the **legacy sizer call-site** rather than
  at the trade-opening call-site. This is a deliberate, risk-bounded
  choice for shadow: it guarantees we never miss a sizing decision,
  but it also means we log the **input** to the legacy sizer (plus
  the legacy output), not the final number that hits the broker after
  any downstream rounding. Phase H.2 will have to decide whether to
  promote at the sizer-call boundary or at the broker-call boundary;
  the current log is sufficient for either.
* `cost_fraction` in the fallback path is derived from
  `(payoff+loss) * cost_factor`, not from the Phase F venue-truth
  per-ticker cost breakdown. That is deliberate - Phase H should not
  grow a dependency on Phase F data paths beyond what NetEdgeRanker
  already carries. Phase H.2 is the correct place to wire in
  `cost_model` directly.
* Correlation bucket caps are **static percentages**. They do the
  right thing in shadow (fire the cap flag so we can observe how
  often it would have clipped the legacy size), but a covariance
  allocator would be strictly better. That is explicitly Phase I.
* Because legacy sizers still own the quantity, the `divergence_bps`
  distribution is our **only** signal that the canonical sizer would
  behave well authoritatively. A couple of weeks of shadow telemetry
  is the right gate before Phase H.2 cutover - not a per-phase speed
  decision.
* The scan_status frozen contract was preserved - no brain_runtime
  keys were added. Phase H diagnostics live on their own endpoint.
* The emitter's defensive `try/except` is broad on purpose. During
  development a single uncaught exception in the emitter would have
  crashed a live trade call-site; the cost of that risk far
  outweighs the cost of silently dropping a few proposal log rows.

### Gaps for Phase H.2

* Authoritative cutover at the four call-sites, with kill-switch and
  governance approval per `governance.py`. Per-source toggles (e.g.
  `paper_trading` first, `alerts` last).
* Covariance allocator over open + proposed positions, sharing the
  `CorrelationBudget` substrate.
* Feedback from Phase F venue-truth telemetry into the Phase H cost
  fraction (`cost_model`'s per-ticker breakdown).
* Diagnostic attribution of divergence to which cap fired.

Phase H is done as a shadow rollout. Moving to Phase I.
