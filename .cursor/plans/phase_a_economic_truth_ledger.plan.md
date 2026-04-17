---
name: Phase A - Economic-truth ledger (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
overview: Introduce a single canonical economic-truth ledger (`trading_economic_ledger`) that records every economic event (entry fill, exit fill, fee, adjustment) with explicit cash_delta / realized_pnl_delta / position state. In shadow mode, every paper and live close also writes through the ledger and reconciles ledger-derived PnL against legacy PnL; disagreements land in `trading_ledger_parity_log`. Legacy `Trade` / `PaperTrade` rows remain the authoritative source of PnL this phase. Same rollout ladder as Phase B/E.
status: completed_shadow_ready
phase_ladder:
  - off
  - shadow
  - compare
  - authoritative
depends_on:
  - phase_b_exit_engine_unification (shadow-ready)
  - phase_e_net_edge_ranker_v1 (shadow-ready)
---

## Objective

Chili has multiple sources of truth for "what did we earn":
- `trading_paper_trades.pnl` (paper)
- `trading_trades.pnl` (live)
- `trading_execution_events` (order lifecycle, not cash)
- session/snapshot PnL

No single source reconciles fills + fees + cash + realized/unrealized PnL end-to-end. That ambiguity breaks calibration for Phase E (NetEdgeRanker) and blocks Phase D (economic promotion metric) — triple-barrier labels are only meaningful against a trusted realized-PnL stream.

Phase A creates that stream in shadow mode. No legacy path changes behavior. The ledger watches, computes, logs deltas.

## Why now

- Phase E calibration needs realized-PnL per signal that isn't polluted by fee accounting drift.
- Phase D's economic promotion metric is "expected PnL per signal" — meaningless without a canonical realized PnL.
- Phase G (live brackets) needs position + cash state reconciled against the venue; Phase A provides the internal truth side of that reconciliation before venue-truth lands.

## Scope (what we change)

### 1. New canonical module: `app/services/trading/economic_ledger.py`

Pure, side-effect free except for DB writes. No network, no HTTP, no fetch_ohlcv.

**Public functions**:
- `record_entry_fill(db, *, source, trade_id, paper_trade_id, user_id, scan_pattern_id, ticker, direction, quantity, fill_price, fee=0.0, venue=None, broker_source=None, event_ts=None, mode, provenance=None) -> EconomicLedgerEvent`
- `record_exit_fill(db, *, source, trade_id, paper_trade_id, user_id, scan_pattern_id, ticker, direction, quantity, fill_price, entry_price, fee=0.0, venue=None, broker_source=None, event_ts=None, mode, provenance=None) -> EconomicLedgerEvent`
- `reconcile_trade(db, *, source, trade_id=None, paper_trade_id=None, legacy_pnl, mode, provenance=None) -> LedgerParityLog`
- `ledger_summary(db, *, lookback_hours, source_filter=None) -> dict`
- `mode_is_active() -> bool`

**Semantics locked**:
- Long entry: `cash_delta = -(qty * fill_price) - fee`, `realized_pnl_delta = 0`.
- Long exit: `cash_delta = +(qty * fill_price) - fee`, `realized_pnl_delta = qty * (fill_price - entry_price) - fee_entry_attrib - fee_exit`.
- Short entry: mirror, `cash_delta = +(qty * fill_price) - fee`, `realized_pnl_delta = 0`.
- Short exit: `cash_delta = -(qty * fill_price) - fee`, `realized_pnl_delta = qty * (entry_price - fill_price) - fees`.
- Idempotency: entry rows keyed `(source, trade_id|paper_trade_id, event_type='entry_fill')`; exit rows keyed `(source, trade_id|paper_trade_id, event_type='exit_fill')`. Duplicate calls are no-ops.
- Parity tolerance: `|legacy_pnl - ledger_pnl| <= brain_economic_ledger_parity_tolerance_usd` counts as agree.

### 2. Shadow hooks

**Paper** (`app/services/trading/paper_trading.py`):
- `open_paper_trade` after `db.flush()`: call `record_entry_fill` if shadow+.
- `_close_paper_trade`: after PnL is computed, call `record_exit_fill` + `reconcile_trade` if shadow+.

**Live** (`app/services/trading/brain_work/execution_hooks.py`):
- `on_live_trade_closed` and `on_broker_reconciled_close`: lazy-emit entry_fill from `trade.avg_fill_price`/`filled_quantity`/`filled_at` if none recorded, then emit `exit_fill` + `reconcile_trade` from `trade.exit_price`/`quantity`/`exit_date`/`pnl`.

Legacy `pt.pnl` and `trade.pnl` remain authoritative. Ledger never mutates them.

### 3. Storage

Migration `129_economic_ledger`:

- `trading_economic_ledger` (append-only):
  - id, source ('paper'|'live'|'broker_sync'), trade_id, paper_trade_id, user_id, scan_pattern_id, ticker, event_type ('entry_fill'|'exit_fill'|'partial_fill'|'fee'|'adjustment'), direction, quantity, price, fee, cash_delta, realized_pnl_delta, position_qty_after, position_cost_basis_after, venue, broker_source, event_ts, mode, provenance_json, created_at
  - Indexes: (source, created_at DESC), (trade_id, created_at), (paper_trade_id, created_at), (ticker, created_at DESC), (event_type, created_at DESC)
  - Partial unique: one entry_fill + one exit_fill per trade_id/paper_trade_id (idempotency via partial unique index).

- `trading_ledger_parity_log`:
  - id, source, trade_id, paper_trade_id, ticker, legacy_pnl, ledger_pnl, delta_pnl, delta_abs, agree_bool, tolerance_usd, mode, provenance_json, created_at
  - Indexes: (source, created_at DESC), (agree_bool, created_at DESC), (ticker, created_at DESC)

### 4. ORM

`EconomicLedgerEvent` and `LedgerParityLog` in `app/models/trading.py`.

### 5. Observability

- `app/trading_brain/infrastructure/ledger_ops_log.py` with `[ledger_ops]` prefix. Bounded one-line format: mode, source, event_type, trade_ref, ticker, qty, price, cash_delta, realized_pnl_delta, agree.
- `GET /api/trading/brain/ledger/diagnostics` in `app/routers/trading_sub/ai.py`: lookback_hours, returns mode, event totals, parity_rate, mean/p95 abs delta, top disagreements.

### 6. Tests

`tests/test_economic_ledger.py`:
- Long entry → cash_delta signed correctly, realized_pnl_delta = 0.
- Long exit at profit → cash_delta positive, realized_pnl_delta matches hand calc.
- Short entry/exit symmetry.
- Fee is always subtracted from cash_delta and realized_pnl_delta.
- Idempotency: calling record_entry_fill twice for the same paper_trade_id is a no-op on the second call.
- `reconcile_trade`: matches legacy PnL within tolerance on synthetic paper trade.
- `reconcile_trade`: flags disagree when legacy diverges by more than tolerance (simulated).
- Crypto: bare-concat ticker + -USD both accepted.

### 7. Release blocker

`scripts/check_ledger_release_blocker.ps1`: exits 1 if any `[ledger_ops]` line contains `mode=authoritative`, mirroring net-edge + exit-engine blockers.

### 8. Docs

`docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md` — ladder, forward/rollback, ops log shape, diagnostics shape, blocker grep, known limitations (entry lazy-emit for live, no venue reconciliation until Phase F/G).

## Forbidden changes (this phase)

- No modification of `Trade.pnl` or `PaperTrade.pnl` derivation. Legacy stays authoritative.
- No changes to broker_service order placement.
- No changes to `TradingExecutionEvent`.
- No changes to `scan/status` contract, prediction-mirror flags, exit-engine parity.
- No venue-truth reconciliation (that is Phase F territory).
- No retroactive backfill of ledger from historical closed trades (future one-off script).

## File-touch order

1. `app/config.py` — add `brain_economic_ledger_mode`, `brain_economic_ledger_ops_log_enabled`, `brain_economic_ledger_parity_tolerance_usd`.
2. `app/migrations.py` — add `_migration_129_economic_ledger`.
3. `app/models/trading.py` — add `EconomicLedgerEvent` and `LedgerParityLog`.
4. `app/trading_brain/infrastructure/ledger_ops_log.py` — new structured logger.
5. `app/services/trading/economic_ledger.py` — new canonical module.
6. `tests/test_economic_ledger.py` — unit tests (must pass before hooks land).
7. `app/services/trading/paper_trading.py` — entry + exit hook + reconcile.
8. `app/services/trading/brain_work/execution_hooks.py` — live trade close lazy-emit + reconcile.
9. `app/routers/trading_sub/ai.py` — diagnostics endpoint.
10. `scripts/check_ledger_release_blocker.ps1` — blocker.
11. `docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md` — rollout doc.
12. `.env` — `BRAIN_ECONOMIC_LEDGER_MODE=shadow` for soak.

## Verification gates

1. `pytest tests/test_economic_ledger.py -v` — all pass.
2. Frozen contracts green: `pytest tests/test_scan_status_brain_runtime.py tests/test_net_edge_ranker.py tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -v`.
3. Docker soak: `BRAIN_ECONOMIC_LEDGER_MODE=shadow`; migration 129 applied; open+close synthetic paper trade writes ≥2 ledger rows + 1 parity row; diagnostics endpoint returns non-empty payload; release-blocker returns 0 on real logs and 1 on synthetic authoritative line.

## Rollback criteria

- If parity agreement < 99% on paper trades during soak, STOP — do not enable in production, fix the ledger math.
- If hooks add > 10ms p95 to `_close_paper_trade` or `on_live_trade_closed`, revert the hook, keep the module + tests.
- Flip-back: set `BRAIN_ECONOMIC_LEDGER_MODE=off` in `.env`, recreate chili + brain-worker.

## Non-goals

- No venue reconciliation — we reconcile ledger-PnL vs legacy-PnL only. Ledger-vs-venue is Phase F/G.
- No new NetEdgeRanker wiring from ledger — that lands once Phase A is authoritative.
- No retroactive backfill script.

## Definition of done

- `trading_economic_ledger` + `trading_ledger_parity_log` migrated.
- Paper open/close hooks and live close hooks both populate in shadow mode.
- Diagnostics endpoint returns meaningful counts.
- Release blocker enforced.
- Docs + rollback runbook published.
- All frozen tests green.
- Phase A plan flipped to `status: completed_shadow_ready`.

## Todos

- [x] **pa-config** - Add 3 settings.
- [x] **pa-migration** - Migration 129.
- [x] **pa-models** - `EconomicLedgerEvent` and `LedgerParityLog` ORM.
- [x] **pa-opslog** - `[ledger_ops]` structured logger.
- [x] **pa-ledger** - `economic_ledger.py` pure module.
- [x] **pa-tests** - `tests/test_economic_ledger.py` (28 passed).
- [x] **pa-paper-hooks** - Paper entry + exit shadow hooks.
- [x] **pa-live-hooks** - Live close lazy-emit hook.
- [x] **pa-diag-endpoint** - `GET /api/trading/brain/ledger/diagnostics`.
- [x] **pa-release-blocker** - `check_ledger_release_blocker.ps1` (verified: exit 0 on real shadow logs, exit 1 on synthetic `mode=authoritative`).
- [x] **pa-docs** - Rollout doc.
- [x] **pa-soak** - Docker soak: migration 129 applied, BRAIN_ECONOMIC_LEDGER_MODE=shadow in container, open+close synthetic paper trade wrote 2 ledger rows (entry_fill + exit_fill) + 1 parity row with `agree=true`, diagnostics endpoint returned events_total=2, parity_rate=1.0, max_abs_delta=$0.0002 (< $0.01 tolerance).
