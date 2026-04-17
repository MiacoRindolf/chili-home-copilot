---
status: completed_shadow_ready
title: Phase F - Execution realism + venue truth (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Closeout (shipped, verified)

* Massive dead-cache fallback fix: `fetch_ohlcv`, `fetch_ohlcv_df`,
  batch `get_aggregates_df_batch`, and `get_quote` now short-circuit on
  `_massive_dead` **only for crypto**. Equities continue to
  Polygon/yfinance. 4 unit tests (`tests/test_market_data_dead_cache_fallback.py`).
* Migration `132_execution_cost_model` applied (chili container):
  `trading_execution_cost_estimates` + `trading_venue_truth_log`.
* Pure `execution_cost_model.py` (`CostFractionBreakdown`,
  `estimate_cost_fraction`, `estimate_capacity_usd`) + 26 unit tests.
* Builder `execution_cost_builder.py` (`compute_rolling_estimate`,
  `upsert_estimate`, `rebuild_all`, `estimates_summary`) + 15 DB tests.
* Venue-truth `venue_truth.py` (`FillObservation`,
  `record_fill_observation`, `venue_truth_summary`) + 11 DB tests.
* Ops log modules: `execution_cost_ops_log.py`, `venue_truth_ops_log.py`.
* Config: `brain_execution_cost_mode`, `brain_execution_cost_default_fee_bps`,
  `brain_execution_cost_impact_cap_bps`, `brain_execution_capacity_max_adv_frac`,
  `brain_venue_truth_mode`, `brain_venue_truth_ops_log_enabled`.
* Diagnostics endpoints live and returning the frozen shape:
  `GET /api/trading/brain/execution-cost/diagnostics`,
  `GET /api/trading/brain/venue-truth/diagnostics`.
* Release-blocker scripts verified (pass on shadow, fail on authoritative):
  `scripts/check_execution_cost_release_blocker.ps1`,
  `scripts/check_venue_truth_release_blocker.ps1`.
* Docker soak `scripts/phase_f_soak.py`: 32/32 OK (migration,
  mode env, idempotent upsert, monotonic cost, capacity cap,
  fill observation, summary shape, dead-cache gate).
* Frozen contract test `test_scan_status_brain_runtime.py`: 2/2 green
  (no regression to Phase 0..5 output).
* `.env` flipped to `BRAIN_EXECUTION_COST_MODE=shadow`,
  `BRAIN_VENUE_TRUTH_MODE=shadow`; chili + brain-worker recreated.
* Docs: `docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md`.

Nothing consumes estimates or venue-truth rows in this phase; both are
observational. Cutover to `authoritative` is gated by a future phase
that explicitly wires NetEdgeRanker and the position sizer to the
per-ticker estimate (Phase H).


# Phase F — Execution realism + venue truth (shadow rollout)

## Objective
Give NetEdgeRanker, backtests, and sizing a **per-ticker, forward-looking** cost estimate instead of a single global `backtest_spread`, gate order size by capacity, and start observing **expected vs realized** execution cost as a stream of venue-truth telemetry. Also fix the dead-cache bug that blocks Polygon/yfinance fallback for equities when Massive marks variants dead.

## Why
The Phase E NetEdgeRanker computes `expected_net_pnl = prob * payoff - costs`. Today `costs` is driven by a flat `backtest_spread` that can't distinguish AAPL from BTC-USD or a 100-share order from a 100,000-share order. That makes every downstream decision — ranking, sizing, gating — structurally miscalibrated.

Separate but related bug (found during Phase A/C soaks): an equity marked as "all Massive variants dead" short-circuits the fallback ladder, returning `[]` even though Polygon/yfinance would succeed. Must be limited to crypto where all three `X:BASE{USD,USDT,USDC}` candidates actually exist.

## Scope (in order)

1. **Massive dead-cache fallback fix** (smallest, standalone):
   * Gate `_massive_dead → return []` short-circuit on `is_crypto(ticker)` in
     `fetch_ohlcv` and `fetch_ohlcv_df`. Equities continue to Polygon/yfinance.
   * Tests `tests/test_market_data_dead_cache_fallback.py`.

2. **Migration 132** (`_migration_132_execution_cost_model`):
   * Table `trading_execution_cost_estimates`: per-(ticker, side, window_days) row with
     `median_spread_bps`, `p90_spread_bps`, `median_slippage_bps`,
     `p90_slippage_bps`, `avg_daily_volume_usd`, `sample_trades`,
     `last_updated_at`. UNIQUE `(ticker, side, window_days)`.
   * Table `trading_venue_truth_log`: per-fill observation with
     `expected_spread_bps`, `realized_spread_bps`, `expected_slippage_bps`,
     `realized_slippage_bps`, `expected_cost_fraction`, `realized_cost_fraction`,
     `notional_usd`, `trade_id (nullable FK)`, `paper_bool`, `mode`, `created_at`.
     Index on `(created_at DESC)` and `(ticker, created_at DESC)`.

3. **ORM** `ExecutionCostEstimate` and `VenueTruthLog` in `app/models/trading.py`.

4. **Pure cost model** `app/services/trading/execution_cost_model.py`:
   * `CostFractionBreakdown` dataclass (frozen):
     `spread, slippage, fees, impact, total` as fractions of notional.
   * `estimate_cost_fraction(ticker, side, notional_usd, estimate_row, *, fee_bps=1.0, impact_exponent=0.5)` → `CostFractionBreakdown`. Uses `p90_spread_bps` + `p90_slippage_bps` for conservative cost, impact as
     `impact_bps = sqrt(notional / ADV_usd) * 10bps` capped at 50bps.
   * `estimate_capacity_usd(estimate_row, *, max_adv_frac=0.05)` → max notional so we
     don't exceed 5% of rolling ADV.

5. **Rolling estimator** `app/services/trading/execution_cost_builder.py`:
   * `compute_rolling_estimate(db, *, ticker, side, window_days=30)` — query closed
     `Trade` rows with `tca_entry_slippage_bps`/`tca_exit_slippage_bps`, derive
     median/p90 spread + slippage, compute ADV from recent `MarketSnapshot` rows
     (or `market_data.fetch_ohlcv` fallback).
   * `upsert_estimate(db, row_dict)` — idempotent UPSERT on UNIQUE key.
   * `rebuild_all(db, *, tickers=None, window_days=30, side='long')` — batch writer.

6. **Venue-truth telemetry** `app/services/trading/venue_truth.py`:
   * `record_fill_observation(db, *, trade, expected_breakdown, realized_spread_bps,
     realized_slippage_bps, realized_cost_fraction, mode_override=None)` — writes
     one `VenueTruthLog` row and emits `[venue_truth_ops]` one-liner.
   * `venue_truth_summary(db, *, lookback_hours=24)` — aggregate for diagnostics.
   * Mode gate `brain_venue_truth_mode ∈ {off, shadow, authoritative}` (default `off`).

7. **Ops log** `app/trading_brain/infrastructure/venue_truth_ops_log.py`:
   * Prefix `[venue_truth_ops]`.
   * `format_venue_truth_ops_line(event, mode, ...)` mirroring existing families.

8. **Config** (`app/config.py`, six new settings):
   * `brain_execution_cost_mode: str = "off"` (off / shadow / authoritative).
   * `brain_execution_cost_default_fee_bps: float = 1.0`.
   * `brain_execution_cost_impact_cap_bps: float = 50.0`.
   * `brain_execution_capacity_max_adv_frac: float = 0.05`.
   * `brain_venue_truth_mode: str = "off"`.
   * `brain_venue_truth_ops_log_enabled: bool = True`.

9. **Diagnostics endpoints** (`app/routers/trading_sub/ai.py`):
   * `GET /api/trading/brain/execution-cost/diagnostics` →
     `{mode, estimates_total, tickers, by_side, stale_estimates, last_refresh_at}`.
   * `GET /api/trading/brain/venue-truth/diagnostics` →
     `{mode, lookback_hours, observations_total, mean_expected_cost_fraction,
     mean_realized_cost_fraction, mean_gap_bps, p90_gap_bps, worst_tickers}`.

10. **Release blockers**:
    * `scripts/check_execution_cost_release_blocker.ps1`: fails on
      `[execution_cost_ops] mode=authoritative` in shadow, and on
      empty `estimates_total` when `-DiagnosticsJson -MinEstimates` gate is used.
    * `scripts/check_venue_truth_release_blocker.ps1`: fails on
      `[venue_truth_ops] mode=authoritative` in shadow.

11. **Tests**:
    * `tests/test_market_data_dead_cache_fallback.py` — equity falls through, crypto does not.
    * `tests/test_execution_cost_model.py` — pure math (breakdown, capacity, edge cases).
    * `tests/test_execution_cost_builder.py` — DB integration (upsert idempotency, estimate math).
    * `tests/test_venue_truth.py` — DB write, ops log, summary, mode gate.

12. **Docs** `docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md` — cost-model formula, ADV source, capacity cap, venue-truth lifecycle, release blockers.

13. **Docker soak** `scripts/phase_f_soak.py`:
    * Migration 132 applied.
    * Insert synthetic cost estimates for 3 tickers × 2 sides → `estimate_cost_fraction` returns monotonic result with notional, capacity cap honoured.
    * Record 3 synthetic venue-truth observations → `venue_truth_summary` reports correct gap stats.
    * Both diagnostics endpoints return the frozen payload shape.
    * Release-blocker scripts pass on real logs, fail on synthetic authoritative.
    * Massive dead-cache: monkeypatch `massive_aggregate_variants_all_dead` → equity ticker still reaches yfinance fallback.

## Forbidden changes
* No change to live trading sizing or NetEdgeRanker gating paths. Cost model is **read-only** until an explicit cutover.
* No rewrite of `Trade` model / TCA columns — we **read** existing `tca_entry_slippage_bps` / `tca_exit_slippage_bps`, never rename or back-fill.
* No scheduler hook in this phase — estimator and venue-truth writes are called on-demand / from the paper-trading close hook only.
* No change to `backtest_spread` default — leave as-is; cost model is additive.
* No rewrite of `massive_client.py` dead-cache logic — only fix the call-site gating in `market_data.py`.

## Verification gates
* Pure tests green: `test_execution_cost_model.py`, `test_market_data_dead_cache_fallback.py`.
* DB tests green: `test_execution_cost_builder.py`, `test_venue_truth.py`.
* Frozen contract tests still green: `test_scan_status_brain_runtime.py`.
* Migration 132 applies cleanly in chili container.
* Soak `scripts/phase_f_soak.py` exits 0.
* Both release-blocker scripts verified twice (real-log pass, synthetic-authoritative fail).
* Diagnostics endpoints return payloads matching the shape frozen in this plan.

## Rollback
* `BRAIN_EXECUTION_COST_MODE=off` and `BRAIN_VENUE_TRUTH_MODE=off`.
* Rows in `trading_execution_cost_estimates` and `trading_venue_truth_log` are harmless (nothing reads them when mode=off).
* Dead-cache fix is a pure bug fix with no flag; to revert, restore the original `if _massive_dead: return []` line.

## Non-goals (explicit)
* No Kelly sizing or covariance portfolio optimizer (that's Phase H).
* No auto-promotion of cost model to authoritative.
* No per-asset-class capacity calibration beyond a single `max_adv_frac`.
* No broker-specific fee schedule — single global `fee_bps`.
* No real-time Level-2 quote capture; observations come from fills only.
