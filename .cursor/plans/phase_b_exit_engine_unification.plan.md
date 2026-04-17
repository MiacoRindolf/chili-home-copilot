---
name: Phase B - Exit-engine unification (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
overview: Collapse the two parallel exit-decision code paths (backtest DynamicPatternStrategy vs live_exit_engine.compute_live_exit_levels) into one canonical, pure ExitEvaluator that takes (position_state, bar_ohlc, exit_config) -> ExitDecision. Both backtest and live/paper call the same evaluator. Ship in shadow mode first with a parity log; switch backtest + live reads to the canonical path only after parity has soaked. Equities-first, crypto-safe. No live capital can move differently in this phase.
status: completed_shadow_ready
phase_ladder:
  - off
  - shadow
  - compare
  - authoritative
depends_on:
  - phase_e_net_edge_ranker_v1 (merged, shadow-ready)
---

## Objective

The single highest-leverage source of "backtest says edge, live says no" on this system is that exit logic exists twice:

- **Backtest**: `app/services/backtest_service.py` (`DynamicPatternStrategy`, 2065 lines)
- **Live / paper**: `app/services/trading/live_exit_engine.py` (`compute_live_exit_levels`, 142 lines) — the file header literally reads *"mirrors DynamicPatternStrategy exit logic"*. That is the drift surface.

Phase B replaces the mirror with a shared, pure evaluator. Both paths consume it; neither owns it. Once parity is proven, the duplicated logic is deleted or reduced to a thin adapter.

This phase does NOT introduce new exit rules. It re-homes the existing rules so backtest PnL and live PnL stop diverging for mechanical reasons.

## Why now

- NetEdgeRanker's `expected_payoff` term (Phase E) is only honest if `win`, `loss`, and `time-to-exit` mean the same thing in backtest and live. Right now they don't, by construction.
- Every promoted pattern's `viability_score` and `oos_return_pct` are computed in backtest. Live delivery is bounded by the live-exit mirror. Any drift between them is silent alpha leakage.
- This unblocks Phase D (triple-barrier labels become trustworthy) and Phase G (live brackets + stop reconciliation can read the same config the backtest respected).

## Scope (what we change)

### 1. New canonical module: `app/services/trading/exit_evaluator.py`

Pure, side-effect free, deterministic. No DB, no network, no HTTP, no `fetch_ohlcv_df`.

**Dataclasses**:
- `ExitConfig` — mirrors current `_load_exit_config` defaults + pattern overrides (atr_stop_mult, atr_target_mult, trailing_enabled, trailing_atr_mult, max_bars, use_bos, bos_buffer_pct, partial_at_1r, stop_hit_priority). Frozen, hashable.
- `PositionState` — entry_price, stop_price, target_price, direction, entry_bar_idx, bars_held, highest_high_since_entry, lowest_low_since_entry, partials_taken.
- `BarContext` — open, high, low, close, volume, atr_current, swing_low_5, swing_high_5, bar_idx, bar_ts.
- `ExitDecision` — action ∈ {hold, exit_stop, exit_target, exit_trail, exit_time_decay, exit_bos, partial}, exit_price, reason_code, r_multiple, updated_state.

**Function**: `evaluate_bar(config: ExitConfig, state: PositionState, bar: BarContext) -> ExitDecision`

Semantics locked (this is the frozen contract for the phase):
1. Stop first — if the bar traverses stop, exit at stop (conservative fill).
2. Target second — if bar traverses target, exit at target.
3. BOS third — if `use_bos` and close breaches swing-low by `bos_buffer_pct`, exit at close.
4. Time-decay fourth — if `bars_held >= max_bars`, exit at close.
5. Trail update — tighten stop only; never loosen.
6. Partial — if `partial_at_1r` and R-move >= 1 and not yet partialed, emit `partial` action.

### 2. Adapter for live: rewrite `app/services/trading/live_exit_engine.py`

- `compute_live_exit_levels` becomes a thin wrapper that:
  - Loads `ExitConfig` from `ScanPattern.exit_config` (same `_load_exit_config` logic — move it into the adapter or into `exit_evaluator` as a pure dict→ExitConfig builder).
  - Builds `PositionState` from the `PaperTrade` / `Trade` row.
  - Pulls latest bar via existing `fetch_ohlcv_df` and builds `BarContext` + ATR.
  - Calls `exit_evaluator.evaluate_bar`.
  - Maps `ExitDecision` back to the legacy dict shape the caller already consumes (`{action, exit_price, trailing_stop, r_multiple, bos_level, …}`).
- Legacy dict shape stays identical so `run_exit_engine` and its callers are untouched behaviorally in `authoritative` mode.

### 3. Adapter for backtest: `app/services/backtest_service.py::DynamicPatternStrategy`

- Extract current per-bar exit block in `DynamicPatternStrategy.next()` into a new helper.
- That helper builds the same `ExitConfig` / `PositionState` / `BarContext` and calls `exit_evaluator.evaluate_bar`.
- Result is mapped back to backtrader-style `self.close()` / `self.sell()` calls.
- Nothing about strategy **entry** logic changes in this phase.

### 4. Shadow rollout (mandatory)

Per `chili-docker-validation-rollout.mdc`:

- Config in `app/config.py`:
  - `brain_exit_engine_mode: str = "off"` (off | shadow | compare | authoritative)
  - `brain_exit_engine_ops_log_enabled: bool = True`
  - `brain_exit_engine_parity_sample_pct: float = 1.0`
- Behavior by mode:
  - `off`: canonical evaluator is NOT called. Legacy paths untouched. (Default — ship this way.)
  - `shadow`: both paths compute their decision AND the canonical evaluator computes in parallel. Only the legacy decision takes effect. Disagreements are logged + persisted.
  - `compare`: same as shadow but the diagnostics endpoint surfaces aggregated disagreement rates and the blocker script treats sustained disagreement > threshold as a release blocker.
  - `authoritative`: legacy code path is a thin adapter; canonical evaluator is the only decision maker. **Not part of this phase** — landed in a follow-up after soak.

### 5. Parity telemetry

- New migration `128_exit_evaluator_parity` (tail will be `127_net_edge_ranker`):
  - Table `trading_exit_parity_log`: per-bar, per-position disagreement record. Fields: id, source (`backtest` | `live`), position_id (nullable for backtest synthetic ids), ticker, bar_ts, legacy_action, legacy_exit_price, canonical_action, canonical_exit_price, pnl_diff_pct (nullable until realized), agree_bool, mode, config_hash, created_at. Indexed on (source, created_at), (ticker, created_at).
- SQLAlchemy model `ExitParityLog` in `app/models/trading.py`.
- Structured ops logger at `app/trading_brain/infrastructure/exit_engine_ops_log.py`, prefix `[exit_engine_ops]`, mirrors `net_edge_ops` exactly: mode, source, position_id, ticker, legacy_action, canonical_action, agree, config_hash, sample_pct. Bounded fields, no PII.

### 6. Diagnostics endpoint

`GET /api/trading/brain/exit-engine/diagnostics` in `app/routers/trading_sub/ai.py`:
- lookback_hours (1..168)
- returns: mode, sample_count, agree_rate, per_source {backtest, live} {sample_count, agree_rate, top_disagreement_actions}, last_parity_snapshot, p50/p95 pnl_diff_pct on realized trades.

### 7. Unit + parity tests

`tests/test_exit_evaluator.py`:
- Deterministic tests: each of the 6 exit rules fires correctly on synthetic `BarContext` / `PositionState` (stop hit, target hit, BOS, time decay, trail update monotonic, partial at 1R).
- Stop-vs-target priority tie-break when a single bar straddles both levels.
- Monotonic trail (stop never loosens).
- Parity tests: feed identical bar sequence into both (a) a stripped copy of the current backtest exit block and (b) the canonical evaluator. Assert identical decisions for >=50 synthetic paths (trend, chop, gap-down, gap-up).
- Crypto path: same evaluator, same result — no special-casing.

### 8. Release-blocker check

`scripts/check_exit_engine_release_blocker.ps1`:
- Fails (exit 1) if ANY `[exit_engine_ops]` line with `mode=authoritative` appears in the log stream, until the authoritative cutover phase opens.
- Mirrors `scripts/check_net_edge_ranker_release_blocker.ps1`.

### 9. Docs

`docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md`:
- Mode ladder, forward and rollback procedure, ops log schema, diagnostics endpoint, parity metrics, release-blocker rule.
- Explicit statement that authoritative cutover is a separate phase.

## Forbidden changes (this phase)

- No new exit rules. Not trailing-by-regime. Not volatility targeting. Not hold-until-NetEdge-flips.
- No changes to entry logic.
- No changes to broker integration or bracket order submission.
- No changes to `pattern_position_monitor.py` behavior.
- No schema changes to `ScanPattern.exit_config`.
- No touching the prediction-mirror flags, `learning` contract, or `scan/status` shape.
- No deletion of the legacy `compute_live_exit_levels` or `DynamicPatternStrategy` exit block; they shrink to adapters only.

## File-touch order

1. `app/config.py` — add 3 settings (mode, ops_log_enabled, parity_sample_pct).
2. `app/migrations.py` — add `_migration_128_exit_evaluator_parity`.
3. `app/models/trading.py` — add `ExitParityLog`.
4. `app/trading_brain/infrastructure/exit_engine_ops_log.py` — new structured logger.
5. `app/services/trading/exit_evaluator.py` — new canonical pure evaluator + dataclasses + `ExitConfig.from_pattern_dict`.
6. `tests/test_exit_evaluator.py` — unit tests for the 6 rules + monotonic trail + partial + priority tie-break. Must pass before step 7.
7. `app/services/trading/live_exit_engine.py` — thin adapter; legacy dict shape preserved. Shadow-hook: when `mode in {shadow, compare}`, compute canonical + legacy, log parity, return legacy. When `mode == authoritative` (future), return canonical.
8. `app/services/backtest_service.py::DynamicPatternStrategy` — shadow-hook inside the per-bar exit block. Same logic: shadow → compute both, log parity, act on legacy; authoritative → act on canonical. This is surgical — only the exit branch, not entries.
9. `tests/test_exit_evaluator_parity_backtest.py` + `tests/test_exit_evaluator_parity_live.py` — synthetic parity harness.
10. `app/routers/trading_sub/ai.py` — add diagnostics endpoint.
11. `scripts/check_exit_engine_release_blocker.ps1` — new.
12. `docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md` — new.

## Verification gates

1. `pytest tests/test_exit_evaluator.py -v` → all pass.
2. `pytest tests/test_exit_evaluator_parity_backtest.py tests/test_exit_evaluator_parity_live.py -v` → all pass.
3. Frozen contracts still green:
   - `pytest tests/test_scan_status_brain_runtime.py -v`
   - `pytest tests/test_net_edge_ranker.py -v`
   - `pytest tests/test_indicator_parity.py -v` (if present)
4. `docker compose up -d --build chili brain-worker scheduler-worker` with `BRAIN_EXIT_ENGINE_MODE=shadow` in `.env`.
5. Migration `128_exit_evaluator_parity` applied: `SELECT version_id FROM schema_version WHERE version_id LIKE '128%'`.
6. Force a synthetic live exit evaluation and a synthetic backtest bar; verify a row lands in `trading_exit_parity_log` with `agree=true` on a known-safe path.
7. `GET /api/trading/brain/exit-engine/diagnostics` returns `{mode:"shadow", sample_count>=1}`.
8. Release-blocker grep returns exit 0 (no `mode=authoritative` anywhere).
9. `[chili_prediction_ops] read=auth_mirror explicit_api_tickers=false` grep stays empty (unchanged contract).

## Rollback criteria

- If parity agreement on the backtest path is < 99% on golden synthetic sequences, STOP — do not enable shadow in production, fix the evaluator.
- If shadow mode introduces any measurable latency regression (> 5ms p95 on `run_exit_engine`), revert the shadow hook, keep the canonical evaluator + tests.
- Flip-back procedure: set `BRAIN_EXIT_ENGINE_MODE=off` in `.env`, `docker compose up -d --force-recreate chili brain-worker scheduler-worker`, verify no `[exit_engine_ops]` lines in last 5m.

## Non-goals (explicit)

- No authoritative cutover. That is a later phase with its own plan, its own soak window, and its own release-blocker change.
- No changes to `NetEdgeRanker` payoff computation. Once Phase B is authoritative, `NetEdgeRanker` automatically benefits because `expected_payoff` will be computed from a consistent exit model — but that wiring is Phase D / Phase J territory, not here.
- No covariance / portfolio-optimizer work — that's Phase H.
- No venue-truth execution work — that's Phase F.

## Definition of done (for this phase)

- Both backtest and live paths compute BOTH the legacy decision and the canonical decision.
- Legacy is still authoritative (canonical is shadow).
- `trading_exit_parity_log` is being populated.
- Diagnostics endpoint returns meaningful counts.
- Release blocker enforced.
- Docs + rollback runbook published.
- All frozen tests still green.
- Phase B plan file set to `status: completed_shadow_ready` (mirroring Phase E).

## Todos

- [x] **pb-plan** - Write this plan file.
- [x] **pb-config** - Added 3 settings to `app/config.py`: `brain_exit_engine_mode`, `brain_exit_engine_ops_log_enabled`, `brain_exit_engine_parity_sample_pct`.
- [x] **pb-migration** - Migration `128_exit_evaluator_parity` with `trading_exit_parity_log` table + 4 indexes (source, ticker, mode, agree_bool).
- [x] **pb-model** - `ExitParityLog` SQLAlchemy model in `app/models/trading.py`.
- [x] **pb-opslog** - `app/trading_brain/infrastructure/exit_engine_ops_log.py` with frozen `[exit_engine_ops]` prefix and bounded 9-field line format.
- [x] **pb-evaluator** - `app/services/trading/exit_evaluator.py`: pure `ExitConfig` / `PositionState` / `BarContext` / `ExitDecision` dataclasses + `evaluate_bar()` with frozen priority (stop > target > BOS > time_decay > trail > partial). Plus `build_config_live()` and `build_config_backtest()` flavor helpers.
- [x] **pb-evaluator-tests** - `tests/test_exit_evaluator.py`: 21 unit tests (all 6 rules, priority tie-breaks, monotonic + non-monotonic trail, partial once-only, crypto semantics, short direction).
- [x] **pb-live-adapter** - `live_exit_engine.compute_live_exit_levels` unchanged in behavior; `_phase_b_shadow_parity` hook added that writes `ExitParityLog` and emits `[exit_engine_ops]` line. Treats `authoritative` as `shadow` with warning (no-op this phase).
- [x] **pb-backtest-adapter** - `DynamicPatternStrategy.next()` exit branch preserved verbatim; `_phase_b_bt_shadow_parity` helper records canonical decision into strategy-attached parity sink. Carries monotonic trail state via `_canonical_trailing_stop` attr.
- [x] **pb-parity-tests-bt** - `tests/test_exit_evaluator_parity.py`: 90 backtest paths (30 seeds × 3 regimes × 3 assertion types), all green; trailing-stop monotonicity invariant verified.
- [x] **pb-parity-tests-live** - 150 live paths (50 seeds × 3 regimes) + crypto test + known-divergence test documenting legacy BOS-overwrites-stop bug.
- [x] **pb-diag-endpoint** - `GET /api/trading/brain/exit-engine/diagnostics` returns `{mode, total, agree, disagree, disagreement_rate, per_source, top_mismatches, configs}`.
- [x] **pb-release-blocker** - `scripts/check_exit_engine_release_blocker.ps1` mirrors net-edge blocker; exits 1 on any `[exit_engine_ops] mode=authoritative` line.
- [x] **pb-docs** - `docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md` with ladder, forward/rollback, ops log shape, diagnostics shape, blocker grep, and the two documented legacy divergences (live no-trail, backtest non-monotonic trail, live BOS-overwrite).
- [x] **pb-docker-soak** - `.env` set to `BRAIN_EXIT_ENGINE_MODE=shadow`. Rebuild + `--force-recreate chili brain-worker`. Migration 128 confirmed applied. Synthetic live parity row written through the real hook (`agree=True`, `mode=shadow`, `config_hash=afe291e051a55a28`). `[exit_engine_ops]` line formatted correctly. Diagnostics endpoint returns `total=2, agree=2, disagreement_rate=0.0`. Release-blocker script exits 0 against real logs; verified it exits 1 when fed a synthetic authoritative line. Synthetic rows cleaned from `trading_exit_parity_log` post-verification. Full test suite: **272/272 green** (21 unit + 227 parity + 22 NetEdgeRanker + 2 scan_status).
