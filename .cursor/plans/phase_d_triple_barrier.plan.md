---
status: completed_shadow_ready
title: Phase D - Triple-barrier labels + economic promotion metric (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Closeout (shadow-ready)

Shipped in shadow mode; promotion behavior unchanged (`brain_promotion_metric_mode=accuracy`).

Verified:
- 34/34 pure tests in `test_triple_barrier.py`.
- 15/15 pure tests in `test_promotion_metric.py`.
- 13/13 DB integration tests in `test_triple_barrier_labeler.py`.
- Frozen scan-status brain_runtime contract still green (2/2).
- Docker soak (scripts/phase_d_soak.py) in the chili container: migration 131 applied, `BRAIN_TRIPLE_BARRIER_MODE=shadow` loaded, 3 synthetic rows (TP/SL/timeout) inserted idempotently with correct label values and realized returns, summary aggregates by barrier correctly, cleanup idempotent.
- `/api/trading/brain/triple-barrier/diagnostics` live with frozen payload shape.
- `check_triple_barrier_release_blocker.ps1`: exit 0 on real shadow logs, exit 1 on synthetic `mode=authoritative`, exit 1 on bogus distribution via `-DiagnosticsJson -MinLabels`.
- pattern_ml.train() now registers `oos_brier_score`, `oos_log_loss`, `expected_pnl_oos_pct` in the registry metrics dict (safe additive change).

# Phase D — Triple-barrier labels + economic promotion metric (shadow rollout)

## Objective
Replace fixed-horizon binary labels (`future_return_5d > 1%`) with **triple-barrier** labels (take-profit hit / stop-loss hit / timeout) and switch `ModelRegistry` promotion from single-key accuracy to an **economic composite** (expected-PnL per trade + Brier score) — both in **shadow mode** until explicit cutover.

## Why
Current promotion:
- Labels are binary on arbitrary 5d/1% threshold → models trained without payoff awareness.
- `ModelRegistry.check_shadow_vs_active` compares a single metric (`oos_accuracy`) → a model that is slightly more accurate but calibrated worse (higher Brier) or has lower expected PnL will still get promoted.
- This is a direct P0 from the NetEdgeRanker north star: promotion must reward **calibrated probability × realized payoff**, not classification accuracy.

## Scope (in order)
1. `pit_contract.py` update: make `future_return_5d` **intentionally forbidden** for DSL but allowed as a **training target** (already handled — just confirm).
2. **New pure module** `app/services/trading/triple_barrier.py`:
   - `TripleBarrierConfig(tp_pct, sl_pct, max_bars, side='long')` dataclass.
   - `TripleBarrierLabel(label: int, exit_bar_idx: int, realized_return_pct: float, barrier_hit: str)` dataclass where `label ∈ {-1, 0, +1}` and `barrier_hit ∈ {'tp','sl','timeout','missing_data'}`.
   - `compute_label(entry_close: float, future_bars: list[OHLCV], cfg) -> TripleBarrierLabel`.
   - ATR variant: `compute_label_atr(entry_close, entry_atr, future_bars, atr_mult_tp, atr_mult_sl, max_bars, side)`.
   - Deterministic, O(max_bars), no external state.
3. **Migration 131** (`_migration_131_triple_barrier`):
   - Table `trading_triple_barrier_labels`: `id`, `snapshot_id (FK, nullable)`, `ticker`, `label_date (date)`, `side`, `tp_pct`, `sl_pct`, `max_bars`, `label (smallint)`, `barrier_hit (varchar 16)`, `exit_bar_idx (int)`, `realized_return_pct (float)`, `entry_close`, `created_at`.
   - UNIQUE `(ticker, label_date, side, tp_pct, sl_pct, max_bars)` for idempotency.
   - Indexes: `(label_date)`, `(ticker, label_date)`, `(snapshot_id)`.
4. **ORM** `TripleBarrierLabelRow` in `app/models/trading.py`.
5. **Ops log** `app/trading_brain/infrastructure/triple_barrier_ops_log.py`: `[triple_barrier_ops]` prefix + `format_triple_barrier_ops_line`.
6. **Labeler service** `app/services/trading/triple_barrier_labeler.py`:
   - `label_snapshots(db, *, limit=500, tp_pct=1.5, sl_pct=1.0, max_bars=5) -> LabelerReport` — selects MarketSnapshots needing labels, fetches forward OHLCV via `market_data`, computes labels, upserts rows. Emits one ops log per write.
   - Mode gate via `settings.brain_triple_barrier_mode` (`off` / `shadow` / `authoritative`).
7. **Economic promotion metric** — `app/services/trading/promotion_metric.py` (pure):
   - `compute_economic_score(oos_brier: float, expected_pnl_per_trade: float, *, brier_penalty=1.0) -> float`
     returning `expected_pnl_per_trade - brier_penalty * oos_brier`.
   - `compare_economic(active_metrics, shadow_metrics, *, min_improvement=0.0, max_brier_regression=0.01) -> dict` → `{better, economic_delta, brier_delta, expected_pnl_delta, reason}`.
8. **Extend ModelRegistry**:
   - New method `check_shadow_vs_active_economic(model_type, *, min_economic_improvement=0.0, max_brier_regression=0.01)` — **does not auto-promote**; returns decision only.
   - `check_shadow_vs_active` signature **unchanged** (backward compatible).
9. **pattern_ml.train() patch**:
   - After computing `oos_brier` and calibrated `y_prob_oos`, compute `expected_pnl_oos_pct = mean(y_prob_oos * realized_return_oos - assumed_cost_bps)` using **triple-barrier labels** if present, else fall back to realized `future_return_5d`.
   - Register `oos_brier_score` **and** `expected_pnl_oos_pct` in `reg.register(metrics=...)` (safe additive change — `metrics` is an opaque dict).
10. **Config** (`app/config.py`):
    - `brain_triple_barrier_mode: str = "off"` (off/shadow/authoritative)
    - `brain_triple_barrier_tp_pct: float = 1.5`
    - `brain_triple_barrier_sl_pct: float = 1.0`
    - `brain_triple_barrier_max_bars: int = 5`
    - `brain_triple_barrier_ops_log_enabled: bool = True`
    - `brain_promotion_metric_mode: str = "accuracy"` (accuracy/shadow/economic)
11. **Diagnostics endpoint** `GET /api/trading/brain/triple-barrier/diagnostics`:
    - Returns: `{mode, labels_total, tp_pct_cfg, sl_pct_cfg, max_bars_cfg, label_distribution: {-1,0,+1}, by_barrier: {tp,sl,timeout}, tickers_distinct, lookback_hours, last_label_at}`.
12. **Release blocker** `scripts/check_triple_barrier_release_blocker.ps1`:
    - Fails on `[triple_barrier_ops]` with `mode=authoritative` when `-RequireShadow`.
    - Fails if a provided `-DiagnosticsJson` payload has `labels_total < min` or `label_distribution` all-zero in a non-empty universe.
13. **Tests**:
    - `tests/test_triple_barrier.py` — pure math: TP-first, SL-first, timeout, both-in-same-bar tie-break (conservative → SL), ATR variant, long vs short, empty future bars, missing data.
    - `tests/test_triple_barrier_labeler.py` — idempotency, DB upsert, mode gating, report counts.
    - `tests/test_promotion_metric.py` — `compute_economic_score`, `compare_economic` edge cases (missing keys, huge Brier regression, tiny expected-PnL improvement).
14. **Docs** `docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md` — rollout ladder, barrier tie-break rule, ATR variant, labeler cadence, ops log format, diagnostics, release blocker, promotion-metric cutover plan.
15. **Docker soak** `scripts/phase_d_soak.py`:
    - Migration 131 applied.
    - `BRAIN_TRIPLE_BARRIER_MODE=shadow` in container.
    - Seed N synthetic MarketSnapshots (or pick existing) with known forward bars → assert triple-barrier labels match hand-computed expectations.
    - Call diagnostics endpoint → assert non-empty distribution.
    - Run release blocker on real logs → exit 0.
    - Synthetic `mode=authoritative` line → release blocker exit 1.

## Forbidden changes
- No change to live trading path, no change to existing promotion behavior (`check_shadow_vs_active` remains the authoritative call until explicit cutover).
- No change to existing `MarketSnapshot.future_return_*` columns.
- No rewrite of `ModelRegistry` signatures (additive only).
- No re-training trigger, no scheduler hook — labeling is on-demand / scheduled externally.
- No hardcoded credentials / API endpoints; use `market_data` service.

## Verification gates
- `pytest tests/test_triple_barrier.py tests/test_triple_barrier_labeler.py tests/test_promotion_metric.py` all green.
- `pytest tests/test_scan_status_brain_runtime.py` still green (frozen contract).
- Migration 131 applies cleanly in Docker.
- Soak script exits 0 with real label writes and assertions matching hand-computed labels.
- Release-blocker check exits 0 on real shadow logs, exits 1 on synthetic authoritative line.
- Diagnostics endpoint returns populated payload.

## Rollback
- `brain_triple_barrier_mode=off` (ops log silenced, labeler no-ops).
- Migration 131 is additive; no destructive rollback needed for shadow. If table must be dropped: `DROP TABLE trading_triple_barrier_labels;`.

## Non-goals (explicit)
- No automatic cutover to economic promotion — Phase D leaves `brain_promotion_metric_mode=accuracy`.
- No ATR auto-tuning; fixed pct barriers in shadow.
- No per-asset-class barrier tuning; one global config pair in shadow.
- No feature-level relabeling of historical paper trades in this phase (handled in Phase H/J).
