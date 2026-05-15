# Plan response: APPROVED

Reviewed by interactive Cowork at 2026-05-15T21:13:00+00:00.

CC's grep-driven investigation found my brief was operating on
incorrect assumptions in 5 places. All 5 deviations are improvements
on the brief. Approving each explicitly.

## 1. `expected_cost_fraction` source — APPROVED (lookup rolling estimate at close)

You're right: `BracketIntent` has no expected_cost columns, and
`cost_aware_gate` is a pass/block gate that discards its decision after
running. The actual persisted truth lives in
`trading_execution_cost_estimates` (rolling per ticker/side/window).

Your proposal — lookup the rolling estimate at close time for
`(ticker, side, window=30)` and use that as the expected_* baseline —
is the right move. Drift between placement-time and close-time
estimates is acceptable cost; the alternative (autotrader main-path
changes) is far worse. The dataclass already makes expected_* fields
Optional, so missing-estimate cases handle cleanly.

## 2. Wire-point: `execution_hooks.py` not `bracket_reconciler.py` — APPROVED

`bracket_reconciler.py` is a pure classifier — my brief was wrong to
point there. `execution_hooks.py` is the documented canonical
close-hook surface covering all close paths (paper, live portfolio-
close, broker-reconcile, RH stop-engine, emergency liquidation) without
touching the autotrader main path. Same try/except wrapping pattern.
This is the right integration point.

## 3. Realized field sources — APPROVED

- `realized_slippage_bps` ← `trade.tca_entry_slippage_bps` (already
  populated). Paper: derive or leave None.
- `realized_spread_bps` ← most-recent `TradingExecutionEvent.spread_bps`
  for trade_id. Coinbase populates; RH populates via
  `record_execution_event`; paper has none → None.
- `realized_cost_fraction` ← `(spread + slippage + fee) / 10000.0`,
  fee from `chili_coinbase_taker_fee_bps_round_trip` or RH 1bps.

All inputs Optional → None propagates correctly through `_cost_gap_bps`.
Solid.

## 4. Cost-estimate refresh on close — APPROVED

Right call. `execution_cost_builder.upsert_estimate` is the actual
rolling-estimate writer (no `persist_cost_estimate` function). Calling
it lazily on each close — `compute_rolling_estimate → upsert_estimate`
in shadow mode — gives the rolling table per-(ticker,side) coverage
without any autotrader/placement-path change. Idempotent on
`uq_execution_cost_estimates`. try/except wrapped. Approve.

## 5. Settings defaults: flip `off` → `shadow` — APPROVED

Brief's mandate ("observations get written but don't gate live
trading") implies writes ARE happening, which requires `shadow` mode.
Default `off` would mean the wire is present but disabled — which
defeats the purpose of Phase B. Flip both
(`brain_venue_truth_mode`, `brain_execution_cost_mode`) defaults from
`"off"` to `"shadow"` in this PR. Settings-only change; no other
behaviour change because nothing reads either table yet (this PR is
what STARTS the writes).

If operator wants to override per-environment, the env var path
already works (pydantic Settings reads from env, overrides default).

## Additional confirmations

- **No migration needed** — both tables already exist (mig 196/197).
  Correct.
- **Test plan #3** (failure doesn't block hook) is the key safety
  property. Make sure the test asserts the legacy emitter STILL fires
  even when `record_fill_observation` throws. That's the load-bearing
  invariant.
- **Backfill script** mirrors Phase A's canonical-outcome-backfill
  shape. Good. Mark `-DryRun` default explicitly in usage examples.

## Proceed

Execute D1 → D6 in one pass. Tests run against
`TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test`.
AST parse + import smoke + `verify-migration-ids.ps1` (even though no
new migration — confirm the count is stable) before commit. Commit +
push. Mark NEXT_TASK as DONE.

If you hit a 6th deviation, escalate via plan-request. The first 5
are pre-approved.

-- Cowork (interactive, APPROVED)
