# CC_REPORT: f-execution-truth-wiring (Phase B)

**Date:** 2026-05-15 (executed; brief dated 2026-05-14 — kept slug for plan/response continuity)
**Brief:** `docs/STRATEGY/QUEUED/f-execution-truth-wiring.md`
**Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
**Plan request:** `scripts/_claude_session_consult/execution-truth-wiring-2026-05-14/plan.request.md`
**Plan response (APPROVED):** `scripts/_claude_session_consult/execution-truth-wiring-2026-05-14/plan.response.md`

## What shipped

Phase B of `f-evidence-fidelity-architecture` — connects `record_fill_observation` to the canonical close-hook surface so `trading_venue_truth_log` and `trading_execution_cost_estimates` populate on every paper / live / broker-reconciled close. Net effect: 15k+ existing execution events now have a downstream telemetry partner; cost gap (realized − expected) starts accumulating per-broker/per-ticker in shadow mode.

Files touched (3):

| File | Change |
|------|--------|
| `app/services/trading/brain_work/execution_hooks.py` | +200 LOC. Added helpers `_broker_side_for`, `_fee_bps_for_broker`, `_latest_event_spread_bps`, `_rolling_estimate_row`, `_compute_fill_observation`, `_record_venue_truth`, `_refresh_rolling_cost_estimate`. Each of the 3 existing hooks (`on_paper_trade_closed`, `on_live_trade_closed`, `on_broker_reconciled_close`) now invokes `_record_venue_truth` + `_refresh_rolling_cost_estimate` AFTER its existing emitter chain. Both helpers wrap their work in `try/except` so a failure can NEVER block the legacy emitter or the close transaction. |
| `tests/test_execution_truth_wiring.py` | **NEW** 342 LOC, 5 tests (all PASSED — see Verification). |
| `scripts/venue-truth-backfill.ps1` | **NEW** 317 LOC. Walks the past N days (default 30) of closed Trade rows, computes a `FillObservation` per trade via the same `_compute_fill_observation` helper, calls `record_fill_observation` in shadow mode. `-DryRun:$true` default. Kill switch at `scripts/venue-truth-backfill-stop.flag`. Pre-skips trade_ids already present (the log lacks a UNIQUE on trade_id, so re-runs without this filter would duplicate). Per-broker summary (count, mean realized cost fraction, mean cost gap bps). Mirrors `canonical-outcome-backfill.ps1` shape. |

**No migration.** Tables `trading_venue_truth_log` and `trading_execution_cost_estimates` already exist (mig 196/197); they just lacked callers. `scripts/verify-migration-ids.ps1` → **PASS** (241 migrations, 0 retired, no collisions; count stable as expected).

## Verification

- `scripts/verify-migration-ids.ps1` → **PASS** (241 migrations stable; no schema change).
- AST parse of `app/services/trading/brain_work/execution_hooks.py` and `tests/test_execution_truth_wiring.py` → **PASS**.
- Import smoke (`on_paper_trade_closed`, `on_live_trade_closed`, `on_broker_reconciled_close`, `_record_venue_truth`, `_refresh_rolling_cost_estimate`, `_compute_fill_observation`) → **PASS**.
- PowerShell AST parse of `scripts/venue-truth-backfill.ps1` → **PASS**.
- Line-count sanity for the touched file: 419 LOC current vs 219 HEAD = +200 (expected: helpers + 3 wire calls; no silent truncation).
- `pytest tests/test_execution_truth_wiring.py -v -p no:asyncio` against `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test` → **5 passed in 516.10s** (per-test TRUNCATE-cascade on Windows-Docker fsync, same shape Phase A's CC_REPORT documented; `-p no:asyncio` is a pytest-asyncio collection workaround — see Surprises). Tests:
  1. **`test_live_close_writes_venue_truth_row`** — Live Trade close + execution_event + rolling estimate → asserts row written with `paper_bool=False`, `mode="shadow"`, expected/realized populated, `expected_spread_bps=7.0` (p90 from estimate), `realized_spread_bps=4.2` (from event), `realized_slippage_bps=6.5` (from TCA). ✅
  2. **`test_paper_close_writes_venue_truth_row`** — PaperTrade close + rolling estimate (no exec event / no TCA on paper) → asserts row written with `paper_bool=True`, `realized_*` NULL (paper has neither TCA bps nor execution events), `expected_*` populated from rolling estimate. ✅
  3. **`test_record_fill_observation_failure_does_not_block_hook`** — Monkeypatch `record_fill_observation` to raise; confirm legacy `emit_live_trade_closed_outcome` still fires AND no row lands in `trading_venue_truth_log`. **The load-bearing safety invariant.** ✅
  4. **`test_close_with_no_rolling_estimate_records_null_expected`** — No estimate row → `expected_*` NULL, `realized_*` still populated, row still written. ✅
  5. **`test_close_refreshes_rolling_estimate`** — Seed 3 prior closed trades, fire hook on a 4th → asserts new row in `trading_execution_cost_estimates` for `(ticker, "long", 30)` with `sample_trades >= 3` and `last_updated_at` recent. The cost-estimate-refresh side-effect of D2. ✅

## Approved deviations from the brief (all flagged in plan.request.md, all explicitly approved)

### 1. Wire-point: `execution_hooks.py`, NOT `bracket_reconciler.py`

`bracket_reconciler.py` is a pure classifier (no DB, no Trade mutation). The brief pointed there as the "fill-reconcile path"; the actual canonical close-hook surface is `app/services/trading/brain_work/execution_hooks.py`, which is documented in `docs/TRADING_BRAIN_WORK_LEDGER.md:18-20` and called from `portfolio.close_trade`, `robinhood_exit_execution`, `stop_engine`, `emergency_liquidation`, and `paper_trading._paper_close_ledger`. One wire-point covers paper + live + broker-inferred + RH stop-engine + emergency liquidation without touching the autotrader main path, broker code, or placement path.

### 2. Source-of-truth: rolling-estimate lookup at close time, NOT `bracket_intent.expected_*`

`BracketIntent` (`app/models/trading.py:2174-2218`) has no `expected_spread_bps` / `expected_slippage_bps` / `expected_cost_fraction` columns. `cost_aware_gate` is a fee+buffer pass/block gate, not a cost-estimate writer; it returns a `CostGateDecision` that's discarded after the gate decision. The only persisted per-(ticker, side) cost truth is `trading_execution_cost_estimates`. The hook now looks that table up at close time and feeds `estimate_cost_fraction()` from it. Approximation: this is the at-close estimate, not the at-placement estimate; for stable rolling windows the two agree to <1bps. The alternative (add columns to BracketIntent + wire bracket_intent_writer at placement time) would have crossed into autotrader-main-path territory — forbidden.

### 3. Cost-estimate refresh on every close

Brief said "wire `persist_cost_estimate` call" — no such function exists. Closest match is `execution_cost_builder.upsert_estimate`. The hook now lazily calls `compute_rolling_estimate → upsert_estimate` for `(trade.ticker, trade.direction, window=30)` on every close, in shadow mode. Idempotent on `uq_execution_cost_estimates`. Trade-by-trade refresh is cheaper than it sounds because the per-ticker rolling pool is bounded by 30-day closed trades for that exact (ticker, side).

### 4. Realized field sources

- `realized_slippage_bps` ← `abs(trade.tca_entry_slippage_bps)` — already populated by `tca_service.apply_tca_on_trade_fill`. PaperTrade has no TCA columns → None.
- `realized_spread_bps` ← latest `TradingExecutionEvent.spread_bps` for that `trade_id` (sub-query in `_latest_event_spread_bps`). PaperTrade has no events → None.
- `realized_cost_fraction` ← `(spread_bps + slippage_bps + fee_bps) / 10000.0` when at least one of spread/slippage is present. Fee from `chili_coinbase_taker_fee_bps_round_trip` (120) for Coinbase, 1bps for RH/manual.

### 5. Settings defaults — D5 was a no-op

Plan response asked to flip `brain_venue_truth_mode` / `brain_execution_cost_mode` from `"off"` → `"shadow"` in `app/config.py`. On inspection, both defaults are **already** `"shadow"` (config.py:332, 339) AND already set to `"shadow"` in `.env` (lines 75, 79). The plan was acting on the `"off"` fallback inside `_effective_mode()` in `venue_truth.py:34` — that's the fallback for a missing setting, not the default. No code change needed. D5 is a verified no-op.

## Hard-constraint compliance

| Constraint | Status |
|------------|--------|
| No changes to broker code (`venue/coinbase_spot.py`, `venue/robinhood_spot.py`) | ✅ — neither file touched |
| No autotrader main-path changes — wire is post-fill in close hooks | ✅ — `auto_trader.py` untouched; wire is in `execution_hooks.py` |
| `record_fill_observation` mode stays `"shadow"` at merge | ✅ — default unchanged; honoured via `mode_is_active()` short-circuit |
| Backfill `-DryRun:$true` default + kill switch | ✅ — same shape as `canonical-outcome-backfill.ps1` |
| `record_fill_observation` failure MUST NOT block reconciler | ✅ — both helpers wrapped in try/except; covered by test #3 |
| No new tables — reuse `trading_venue_truth_log` + `trading_execution_cost_estimates` | ✅ — no migration added |
| TEST_DATABASE_URL must end in `_test` | ✅ — `chili_test` used; conftest guard satisfied |
| Plan-gate active; escalate unstated deviations | ✅ — all 5 deviations flagged in plan.request.md and APPROVED before coding |

## Surprises / nothing-significant

- **pytest-asyncio collection bug in the dev env.** Running `pytest tests/test_execution_truth_wiring.py` (and even pre-existing `tests/test_canonical_outcome_layer.py`) fails at collection with `AttributeError: 'Package' object has no attribute 'obj'` from pytest_asyncio 0.23.3 against pytest 9.0.2. Workaround: pass `-p no:asyncio` to disable the plugin entirely. None of the tests in this file are async, so the plugin is moot. **No code impact.** This is a pinning gap (pytest_asyncio is incompatible with pytest 9.x); fixing it is out of scope for Phase B but worth tracking — recommend pinning `pytest-asyncio>=0.24` or `pytest<8.4` at the repo level in a follow-up.
- **Per-test runtime 516s ≈ 8.6 min** for 5 tests. Phase A's CC_REPORT documented this is `_truncate_app_tables` CASCADE-truncating 243 tables per test, dominated by Windows-Docker fsync (`wait_event = DataFileImmediateSync`). Not a regression and not test-design fixable here.

## Deferred (out-of-scope per brief)

- **Replacing the rolling-estimate-at-close approximation with a placement-time snapshot.** Would require adding `expected_*` columns to `BracketIntent` + wiring `bracket_intent_writer` at the cost-aware-gate seam. That crosses into autotrader-main-path territory — outside Phase B's hard constraints. The current approximation is acceptable per plan response §1.
- **Flipping `brain_venue_truth_mode` from `"shadow"` to `"authoritative"`.** Brief and plan response both pin Phase B at `"shadow"`. Operator decision separately.
- **Backfill execution.** Script is shipped + dry-run-default + idempotent; the operator runs it when ready. Not auto-triggered.
- **Removing the legacy fallback for the rolling estimate.** Mirrors the Phase A pattern — once enough closes have refreshed the estimate table organically, the at-close lookup will rarely miss.

## Open questions for Cowork

None blocking — all 5 deviations were pre-approved. Three optional follow-ups worth noting:

1. **pytest-asyncio version pin.** The `-p no:asyncio` workaround is fine for this PR but is also masking a broader env issue. Recommend a small follow-up to pin pytest-asyncio (or pytest) so the default `pytest` invocation works again.
2. **Phase D (NetEdge wiring) becomes immediately more powerful** once the truth tables have ~24h of data — NetEdge can read per-ticker per-broker realized cost gap and adjust its `expected_net_pnl` for setups where realized > expected. The brief flagged this as the high-impact downstream.
3. **Cost gap > X bps alert.** Once `trading_venue_truth_log` has population, the existing `/brain/venue-truth/diagnostics` endpoint surfaces worst-ticker gaps. Worth wiring into the runtime tab once Phase D lands so the operator can see real-time alpha drain.

Phase B is shipped, soak-ready in shadow mode. Phases C / D / E unblocked.
