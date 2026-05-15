# Plan Request: f-execution-truth-wiring (Phase B of evidence-fidelity)

**Session:** `execution-truth-wiring-2026-05-14`
**Brief:** `docs/STRATEGY/QUEUED/f-execution-truth-wiring.md`
**Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
**Phase A reference:** commit `ca1705f` — canonical outcome split

## 1. Consult-gate finding (CRITICAL — brief assumption was wrong)

**Brief assumed:** `expected_cost_fraction` lives on `bracket_intent.expected_*`.
**Reality after grep:**

- `BracketIntent` (`app/models/trading.py:2174-2218`) has **no** `expected_spread_bps`, `expected_slippage_bps`, or `expected_cost_fraction` columns. Its expected fields are just `stop_price` / `target_price` / `entry_price`.
- `cost_aware_gate.cost_aware_min_edge_gate` (`app/services/trading/cost_aware_gate.py:161-233`) is a **fee+buffer pass/block gate**, not a cost-estimate writer. It returns `CostGateDecision(allowed, reason, fee_bps, threshold_bps, edge_bps)` — discarded after the gate decision; never persisted per-trade.
- `execution_cost_model.estimate_cost_fraction` (`app/services/trading/execution_cost_model.py:72-153`) is the pure compute that returns `CostFractionBreakdown(spread, slippage, fees, impact, total)`. It's invoked transiently — no per-trade persistence.
- The only persisted per-(ticker, side, window) cost truth is `trading_execution_cost_estimates` (`ExecutionCostEstimate` model, `app/models/trading.py:2109-2139`), populated by `execution_cost_builder.upsert_estimate` (`execution_cost_builder.py:213-282`). This is **rolling**, not per-trade.

**Resolution proposal:** Compute expected_* at close time using the current rolling estimate (lookup `trading_execution_cost_estimates` for `(ticker, side, window=30)`) plus `estimate_cost_fraction()`. If no rolling estimate exists yet for this (ticker, side), expected_* fields are `None` in the FillObservation (already optional in dataclass). This avoids any autotrader / placement-path change.

The downside is approximation: we capture today's expected estimate, not the estimate at the moment of placement. For stable windows the two are nearly identical; for thin-volatile periods they can drift. Surfacing here because the brief explicitly flagged this as a consult-gate item.

## 2. Wire-point (deviation from brief)

**Brief said:** `bracket_reconciler.py` — "add `record_fill_observation` call after each fill is committed to DB".
**Reality:** `bracket_reconciler.py` (`app/services/trading/bracket_reconciler.py`) is a **pure classifier** — no DB access, no Trade-row mutation, no fill commits. It returns a `ReconciliationDecision` dataclass and the caller (`bracket_reconciliation_service.py`) writes the log row. It is NOT the function that closes a Trade.

**Actual canonical close-hook surface:** `app/services/trading/brain_work/execution_hooks.py` exposes three functions called by every Trade-close path:

| Hook | Called from |
|---|---|
| `on_paper_trade_closed(db, pt)` | `paper_trading._paper_close_ledger` |
| `on_live_trade_closed(db, trade, source=…)` | `portfolio.close_trade` (185), `robinhood_exit_execution.py:431`, `stop_engine.py:1079`, `emergency_liquidation.py:131` |
| `on_broker_reconciled_close(db, trade, source=…)` | `broker_service` RH sync / manual cleanup |

These run in the same DB transaction as the close, are already wrapped in try/except in every caller, and are **the** documented close-hook surface (`docs/TRADING_BRAIN_WORK_LEDGER.md:18-20`). Wiring at this layer covers paper, live portfolio-close, broker-inferred close, RH stop-engine close, and emergency liquidation — without touching the autotrader main path, the broker code, or the placement path.

**Proposal:** Wire `record_fill_observation` into all three hooks in `execution_hooks.py`. paper_bool=True for `on_paper_trade_closed`, False for the live hooks.

## 3. Realized field sources

- `realized_slippage_bps` ← `trade.tca_entry_slippage_bps` (already populated by `tca_service.apply_tca_on_trade_fill`; signed bps, positive = paid worse). For paper: derive from `(exit_price - entry_price)` / `entry_price` slippage via `_apply_slippage` — or leave None (the field is Optional).
- `realized_spread_bps` ← look up the most-recent `TradingExecutionEvent.spread_bps` for `trade_id` (Coinbase live populates this; RH live populates via `record_execution_event`; paper has none → `None`).
- `realized_cost_fraction` ← `(spread_bps + slippage_bps + fee_bps) / 10000.0`. Fee constant from settings: `chili_coinbase_taker_fee_bps_round_trip` (120) for Coinbase, 1 bps for RH (RH crypto fee-free is a separate path). If any input bps is None, leave the fraction None and let `_cost_gap_bps` return None.

## 4. Cost-estimate "persist" (D2) — refinement

The brief said "Parallel wire to `execution_cost_model.persist_cost_estimate` … Call it before placement". There is no such function, and "before placement" would require an autotrader main-path change (forbidden).

**Proposal:** Refresh the rolling estimate **on the same close hook** by calling `execution_cost_builder.compute_rolling_estimate(db, ticker=trade.ticker, side=trade.direction, window_days=30) → upsert_estimate(db, est, mode_override="shadow")`. This populates `trading_execution_cost_estimates` for that (ticker, side) lazily on every close, no autotrader change, no schema change. Idempotent on `uq_execution_cost_estimates`. Wrapped in try/except.

## 5. Settings / mode gates

Both functions already gate on settings:
- `record_fill_observation` honours `settings.brain_venue_truth_mode` (default `off`). Brief mandate: **stay at "shadow" at merge**. I will not flip it. The operator flips via env / config when ready.
- `execution_cost_builder.upsert_estimate` honours `settings.brain_execution_cost_mode` (default `off`).

**Question (raise if non-trivial):** Should I default both modes to `"shadow"` in `app/config.py` (current defaults are `off`), or leave them off and require an operator-driven flip? Brief says "observations get written but don't gate live trading" — that implies writes are happening, which means mode must be `shadow`. Default proposal: change `brain_venue_truth_mode` and `brain_execution_cost_mode` defaults from `"off"` to `"shadow"` in this PR (settings change only; no behaviour change for non-DB consumers since nothing reads either table yet).

## 6. Deliverables (revised)

| ID | File | Change |
|---|---|---|
| D1 | `app/services/trading/brain_work/execution_hooks.py` | Add `_record_venue_truth(db, trade_or_pt, *, paper_bool)` helper; call from all 3 hooks. Wrap in try/except so failures NEVER block the close path. |
| D1b | `app/services/trading/brain_work/execution_hooks.py` (same file) | New helper `_compute_fill_observation_fields(db, trade_or_pt, paper_bool)` — pulls TCA bps from trade, spread_bps from latest TradingExecutionEvent for trade_id, computes cost fraction. Returns `FillObservation` or `None` (when nothing to record). |
| D2 | `app/services/trading/brain_work/execution_hooks.py` (same file) | Same hook calls `_refresh_rolling_cost_estimate(db, trade)` (try/except). Lazy per-close rebuild for that (ticker, side). |
| D3 | `tests/test_execution_truth_wiring.py` | Fixture seeds a closed Trade + TradingExecutionEvent, invokes `on_live_trade_closed`, asserts a row appears in `trading_venue_truth_log` with non-null expected/realized fields. Second test: paper close path. Third: try/except guard — failing inner write does NOT raise. |
| D4 | `scripts/venue-truth-backfill.ps1` | Walks past 30 days of closed Trade rows, computes FillObservation per trade, calls `record_fill_observation` in shadow mode. `-DryRun:$true` default. Kill switch at `scripts/venue-truth-backfill-stop.flag`. Logs a per-broker summary at the end. Mirrors the shape of `canonical-outcome-backfill.ps1` (Phase A). |
| D5 | `app/config.py` | Default `brain_venue_truth_mode = "shadow"`, `brain_execution_cost_mode = "shadow"` (currently `"off"`). |
| D6 | `docs/STRATEGY/CC_REPORTS/2026-05-14_execution-truth-wiring.md` | CC_REPORT. |

**No migrations.** Both tables already exist (mig 196 / 197 per Phase F). Reusing them as the brief mandates.

## 7. Test plan

`TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test`

Test cases (`tests/test_execution_truth_wiring.py`):

1. **`test_live_close_writes_venue_truth_row`** — Seed a Trade with status=open, ticker, direction=long, entry_price, quantity, filled_at, tca_entry_slippage_bps. Seed one TradingExecutionEvent for that trade_id with spread_bps. Seed a rolling ExecutionCostEstimate for (ticker, "long", 30). Set trade.status=closed + exit_price/exit_date. Call `on_live_trade_closed(db, trade, source="test")`. Assert: `trading_venue_truth_log` has 1 row, `expected_*` and `realized_*` populated, `paper_bool=False`, `mode="shadow"`.
2. **`test_paper_close_writes_venue_truth_row`** — Same shape but on PaperTrade + `on_paper_trade_closed`. Assert `paper_bool=True`, realized_slippage_bps from `_apply_slippage` (or None — see implementation), expected_* from the rolling estimate.
3. **`test_record_fill_observation_failure_does_not_block_hook`** — Monkeypatch `record_fill_observation` to raise. Call `on_live_trade_closed`. Assert no exception bubbles up AND the legacy emitter (`emit_live_trade_closed_outcome`) still fires (covered by existing test `test_live_trade_close_emitter_coverage.py` semantics).
4. **`test_close_with_no_rolling_estimate_records_null_expected`** — Seed Trade without an ExecutionCostEstimate row. Call hook. Assert `trading_venue_truth_log` row written with `expected_spread_bps=NULL`, `expected_slippage_bps=NULL`, `expected_cost_fraction=NULL` (realized still populated).
5. **`test_close_refreshes_rolling_estimate`** — Seed 3 closed Trades for (ticker, long) with `tca_entry_slippage_bps` set. Call `on_live_trade_closed` on a 4th trade. Assert `trading_execution_cost_estimates` has a row for (ticker, "long", 30) with `sample_trades >= 3` and `last_updated_at` recent.

## 8. Backfill script outline (D4)

```powershell
# scripts/venue-truth-backfill.ps1
param(
    [int]$LookbackDays = 30,
    [switch]$DryRun = $true,
    [int]$BatchSize = 100
)
# Kill switch: scripts/venue-truth-backfill-stop.flag
# Runs inside chili container via: docker compose exec chili python -m scripts.venue_truth_backfill
# 1) SELECT trade_id, ticker, direction, entry_price, quantity, exit_price, tca_entry_slippage_bps
#    FROM trading_trades WHERE status='closed' AND exit_date >= NOW() - INTERVAL '$LookbackDays days'
# 2) For each: build FillObservation from same _compute_fill_observation_fields helper
# 3) If -DryRun: log a histogram (per-broker count, mean realized_cost_fraction, mean gap_bps) — no DB write.
# 4) Else: record_fill_observation(db, obs) — idempotent? No, the venue_truth_log lacks a UNIQUE on trade_id.
#    Backfill MUST therefore skip trade_ids already present (SELECT DISTINCT trade_id FROM trading_venue_truth_log).
# 5) Honour kill switch every batch.
```

Idempotency notes: `trading_venue_truth_log` has **no** unique constraint on `trade_id` — re-running the backfill would duplicate rows. The script will pre-query the existing `trade_id` set and skip those. Document in script header.

## 9. Hard-constraint compliance plan

| Constraint | How met |
|---|---|
| No broker-code changes | ✅ Wire is in `execution_hooks.py`, not venue/* |
| No autotrader main-path changes | ✅ Wire is post-fill in close hooks |
| `record_fill_observation` mode stays `"shadow"` at merge | ✅ Default in config = `"shadow"`. Brief mandate honoured. |
| Backfill `-DryRun:$true` default + kill switch | ✅ Same pattern as `canonical-outcome-backfill.ps1` |
| `record_fill_observation` failure MUST NOT block reconciler | ✅ Inner try/except in each of the 3 hooks |
| No new tables — reuse log + estimates | ✅ Both already exist (mig 196/197) |
| TEST_DATABASE_URL must end in `_test` | ✅ Standard conftest guard |
| Plan-gate active; escalate only unstated deviations | ✅ Escalating two: brief's wire-point assumption (bracket_reconciler.py is pure) and brief's source-of-truth assumption (bracket_intent.expected_* doesn't exist). Both flagged above. |

## 10. Open questions for Cowork

1. **Source-of-truth approximation OK?** Computing expected_* at close time from the rolling estimate (rather than the placement-time estimate) is the only path that avoids autotrader/bracket_intent changes. Approval to proceed with this approximation? Alternative: add expected_* columns to BracketIntent in a new migration + wire `bracket_intent_writer` to capture cost_aware_gate output at placement — that adds ~80 LOC and crosses into placement-path territory.
2. **Default both modes to `"shadow"`?** Or leave defaults `"off"` and require operator flip via env? Brief language ("observations get written but don't gate live trading") implies shadow-at-merge.
3. **Paper-trade realized_slippage_bps source?** PaperTrade has no `tca_*` columns. Options: (a) reconstruct from `_apply_slippage`'s `chili_paper_slippage_bps` default (5 bps), (b) leave `None`. Both leave realized_cost_fraction partially populated. Proposal: (b) — paper realized slippage is synthetic and would corrupt the rolling estimate later. Live realized is the truth we care about.
4. **`_refresh_rolling_cost_estimate` on every close?** Cheaper alternative: only call it when `random.random() < 0.1` (10% of closes, amortised refresh) or on a separate scheduler. Brief implies "wire it", so default proposal is "on every close". If you'd prefer a separate scheduler, say so and I'll switch to the existing scheduler-worker.

Awaiting `APPROVED` / `REVISE: …` / `ABORT: …`.
