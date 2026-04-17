---
name: Phase E - NetEdgeRanker v1 (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
overview: Introduce a single calibrated NetEdgeRanker.score() as the canonical measure of expected net edge, wired in shadow mode alongside existing heuristics. Equities-first, crypto-safe. Mirrors the prediction-mirror rollout discipline (off -> shadow -> compare -> authoritative). Cannot move live capital in this phase.
status: completed_shadow_ready
phase_ladder:
  - off
  - shadow
  - compare
  - authoritative
---

## Objective

Create a production-grade `NetEdgeRanker` service that scores every signal as:

```
expected_net_pnl_per_unit =
    calibrated_prob(win | ctx, regime) * expected_payoff(ctx)
  - spread_cost(ctx)
  - slippage_cost(ctx)
  - fees_cost(ctx)
  - miss_prob_cost(ctx)
  - partial_fill_cost(ctx)
```

Ship it in **shadow mode** only: it writes its score to `trading_net_edge_scores` on every decision, but no sizing / promotion / auto-enter path actually reads it for trading yet. We compare its ranking to the existing heuristic (`compute_expectancy_edges`) and surface calibration and disagreement metrics via a diagnostics endpoint.

This is Phase E of the parent plan, deliberately picked first as the backbone everything else plugs into.

## Scope (what we change)

1. New module: `app/services/trading/net_edge_ranker.py`
   - Dataclasses: `NetEdgeSignalContext`, `NetEdgeCostBreakdown`, `NetEdgeScore`, `NetEdgeProvenance`
   - Class: `NetEdgeRanker` with `score()`, `calibrate()`, `snapshot_calibration()`, `diagnostics()`, `mode_is_active()`
   - In-process TTL cache for calibrator state
   - Cold-start defaults when sample count below `brain_net_edge_min_samples`

2. New migration: `app/migrations.py::_migration_127_net_edge_ranker` (tail is `126_mesh_dependency_edges`)
   - Table `trading_net_edge_scores`: per-decision log (decision_id, pattern_id, ticker, asset_class, regime, ctx_hash, calibrated_prob, expected_payoff, spread_cost, slippage_cost, fees_cost, miss_prob_cost, partial_fill_cost, expected_net_pnl, heuristic_score, disagree_flag, mode, created_at)
   - Table `trading_net_edge_calibration_snapshots`: daily calibrator state per regime (regime, sample_count, reliability_json, brier_score, log_loss, method=isotonic|platt|cold_start, fitted_at, version_id)
   - Both with appropriate indexes (ticker+created_at, regime+fitted_at)

3. New SQLAlchemy models: `app/models/trading.py::NetEdgeScoreLog`, `NetEdgeCalibrationSnapshot`

4. Config additions in `app/config.py`:
   - `brain_net_edge_ranker_mode: str = "off"` (off|shadow|compare|authoritative)
   - `brain_net_edge_ops_log_enabled: bool = True`
   - `brain_net_edge_min_samples: int = 50`
   - `brain_net_edge_cache_ttl_s: int = 300`
   - `brain_net_edge_shadow_sample_pct: float = 1.0` (1.0 = log every decision in shadow)

5. Structured ops logger: new helper `app/trading_brain/infrastructure/net_edge_ops_log.py` using prefix `[net_edge_ops]`. Fields: mode, decision_id, pattern_id, ticker, asset_class, regime, read_source, net_edge, heuristic_score, disagree_vs_heuristic, sample_pct. Mirrors `prediction_ops_log.py` shape exactly.

6. Shadow hooks (read-only, guarded by `brain_net_edge_ranker_mode in {"shadow","compare","authoritative"}`):
   - `paper_trading.auto_enter_from_signals`: after the heuristic produces its decision, call `NetEdgeRanker.score()` and log. **Sizing and enter/skip remain driven by the heuristic.**
   - `opportunity_scoring` / `opportunity_board`: produce an alternate ranking from NetEdgeRanker and write to the ops log. **Displayed ranking is unchanged.**

7. New router: `app/routers/trading_sub/ai.py` gets `GET /api/trading/brain/net-edge/diagnostics`
   - Reliability curve (binned prob vs realized win rate)
   - Brier score and log-loss
   - Per-regime calibration error
   - Disagreement rate vs heuristic (last 24h)
   - Sample count and last calibration timestamp
   - Returns frozen JSON shape analogous to `scan/status`

8. New tests: `tests/test_net_edge_ranker.py`
   - Cost math unit tests (spread, slippage, fees composition)
   - Calibrator isotonic monotonicity
   - Cold-start path when sample_count < min_samples
   - Mode gating: `off` -> function returns None, no DB writes, no log lines
   - Ops-log shape parity with prediction_ops_log
   - Diagnostics endpoint shape + HTTP 200 on empty state
   - Fallback: if calibrator raises, score returns None and caller proceeds with heuristic

9. New doc: `docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md`
   - Phase ladder (off -> shadow -> compare -> authoritative)
   - Release-blocker grep: no `[net_edge_ops] mode=authoritative` while `brain_net_edge_ranker_mode != "authoritative"`
   - Rollback = set mode to `off` and recreate chili service (`.env` mutation semantics per Docker rule)
   - Mirror-read validation contract (Phase F+ territory but documented now for continuity)

## Forbidden (explicit non-goals of this phase)

- **No authoritative wiring.** `NetEdgeRanker` must not gate any entry, exit, sizing, or promotion decision in this phase. Any code path that would let it do so lives behind `mode == "authoritative"` with the rollout doc flipped.
- No changes to `scan/status` response shape (`brain_runtime`, `encode_error`, frozen contract per `chili-scan-status-deploy-validation.mdc`).
- No changes to `[chili_prediction_ops]` format or the prediction-mirror release-blocker grep.
- No changes to exit logic, broker logic, `DynamicPatternStrategy`, or `portfolio_risk.size_position`.
- No new ML training loop. Calibration is isotonic regression / Platt over realized PnL rows that already exist; no gradient boosting, no LightGBM in this phase.
- No changes to `rules_json` schema, pattern DSL, or mining validation gates.
- No UI changes beyond the diagnostics endpoint JSON (no new pages or dashboard cards in this phase).
- No changes to migration numbering outside slot 070.
- No edits to `.env` committed.

## File-touch order

1. `app/config.py` - add settings
2. `app/migrations.py` - add migration 127
3. `app/models/trading.py` - add the two new ORM classes
4. `app/services/trading/net_edge_ranker.py` - new module (dataclasses + class + cold-start)
5. `app/trading_brain/infrastructure/net_edge_ops_log.py` - new ops logger
6. Cost model functions inside `net_edge_ranker.py` pulling from `execution_quality.suggest_adaptive_spread`, `massive_client.is_crypto`, `settings.backtest_spread`, `settings.backtest_commission`
7. Calibrator in same module: pulls realized outcomes from `trading_trades`, `trading_paper_trades`, `trading_execution_events`; fits per-regime isotonic / Platt; writes `trading_net_edge_calibration_snapshots`
8. `app/services/trading/paper_trading.py` - shadow hook in `auto_enter_from_signals` (log only)
9. `app/services/trading/opportunity_scoring.py` - shadow hook (log only)
10. `app/routers/trading_sub/ai.py` - diagnostics endpoint
11. `tests/test_net_edge_ranker.py`
12. `docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md`

## Verification gates (must pass before closeout)

- `pytest tests/test_net_edge_ranker.py -v` green
- `pytest tests/test_indicator_parity.py tests/test_scan_status_brain_runtime.py -v` still green (no regression)
- Migration 127 applies cleanly against a fresh test Postgres (CI-equivalent `conda run -n chili-env python -c "from app.db import engine; from app.migrations import run_migrations; run_migrations(engine)"`)
- With `brain_net_edge_ranker_mode=off`: zero `[net_edge_ops]` log lines, zero rows in `trading_net_edge_scores`
- With `brain_net_edge_ranker_mode=shadow` and a fabricated paper auto-enter signal in a test: one `[net_edge_ops]` line, one row written, `heuristic_score` populated, `disagree_flag` populated
- Diagnostics endpoint returns HTTP 200 with `{"ok": true, "net_edge_ranker": {...}}` on empty DB (cold-start shape)
- Release-blocker grep: `Select-String "\[net_edge_ops\].*mode=authoritative"` over logs returns empty while mode is `shadow`

## Rollback criteria

- Any verification gate fails -> revert changes, do not ship, open follow-up plan.
- If shadow ops logging materially increases log volume (>2x baseline over 1h), set `brain_net_edge_shadow_sample_pct=0.1` and re-evaluate.
- If calibrator fits produce non-monotone reliability curves persistently (>3 days), freeze mode at `off`, file a follow-up to inspect feature leakage.

## Definition of done

- All 16 todos in the phase checklist marked completed with evidence
- Merged with `brain_net_edge_ranker_mode=off` as default (no behavior change in prod until flipped)
- Rollout doc published
- Release-blocker grep script committed
- Follow-up plan `phase_f_venue_truth.plan.md` drafted (not executed) referencing NetEdgeRanker's cost-model inputs it will upgrade

## Risks explicitly accepted in this phase

- Calibrator quality will be poor until Phase D (triple-barrier labels) lands. This is fine: shadow mode is harmless and produces measurements we can use to decide whether Phase D improves calibration.
- Cost model is initially a composition of existing heuristic costs; it does not yet use venue-truth (Phase F). That is why this phase ends at `shadow`, not `authoritative`.
- Ledger truth (Phase A) is not in place yet. Realized PnL used for calibration is sourced from today's `Trade.pnl`, which inherits whatever noise the current system has. Shadow mode does not amplify that noise into live decisions.
