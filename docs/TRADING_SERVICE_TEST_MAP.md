# Trading service → test coverage map

A living punch-list of every file under `app/services/trading/` and whether it has any test-file substring match in `tests/`. Generated initially from Phase C tech-debt audit; kept up-to-date as features are added.

**This is a working doc, not a hall of shame.** When a file is "UNMAPPED", the next engineer to modify it owns the test addition. Leave the row in place with a real owner + quarter target until coverage lands.

## Baseline (Phase C, 2026-Q2)

- Total services: **202** (under `app/services/trading/*.py`, excluding `__init__`)
- Services with any test-file name match: **86** (≈43%)
- UNMAPPED: **116** (≈57%)

The substring match overstates coverage slightly — a file that matches `test_<foo>_stub.py` only because its name contains `<foo>` may not actually exercise the service's full surface. Treat "MAPPED" as "has at least one test that exercises this surface" and bias toward caution.

## Critical tier — capital / hot-path / frozen contract

These UNMAPPED services either touch capital, the kill-switch surface, or a frozen contract. Target coverage this quarter (2026-Q3). Owner = module owner; if no owner, ping #trading in chat and claim it.

| Service | Why it matters | Owner | Target | Notes |
|---|---|---|---|---|
| `portfolio_risk.py` | Drawdown breaker (Hard Rule 2). Every sizing decision passes through `check_new_trade_allowed` / `check_drawdown_breaker` | TBD | 2026-Q3 | `test_governance_daily_loss.py` covers a tiny slice; needs full breaker-state unit tests |
| `stop_engine.py` | Writes stop-loss intents that persist across restart | TBD | 2026-Q3 | Phase-B partial-fill test pins adapter behavior; stop_engine's own logic uncovered |
| `tca_service.py` | Transaction cost analysis — slippage bps used by `auto_trader_rules.resolve_effective_slippage_pct` | TBD | 2026-Q3 | `suggest_adaptive_spread` is hot path |
| `execution_quality.py` | P90 slippage history used by rule gates | TBD | 2026-Q3 | Paired with `tca_service`; same owner makes sense |
| `regime_allocator.py` | Capital allocation by regime — direct input to position sizing | TBD | 2026-Q3 | — |
| `emergency_liquidation.py` | Name says it all. Must not be buggy when called | TBD | 2026-Q3 | Mock broker; assert exit-all paths |
| `robinhood_exit_execution.py` | Exit order placement for RH; paired with Phase-B venue tests | TBD | 2026-Q3 | `test_broker_sync.py` covers the `sync_pending_exit_order` path only |
| `order_intelligence.py` | Pre-order validation | TBD | 2026-Q3 | — |
| `monitor_rules_engine.py` | Runs exit rules on open positions each tick | TBD | 2026-Q3 | — |
| `pattern_engine.py` | Pattern match orchestration | TBD | 2026-Q4 | Large surface; start with smoke tests |
| `ml_engine.py` | ML model loading / prediction | TBD | 2026-Q4 | Model-file fixture needed |
| `position_plan_generator.py` | Position-plan from pattern + risk | TBD | 2026-Q4 | — |
| `auto_trader_llm.py` | LLM revalidation gate for live entries | TBD | 2026-Q4 | Mock the LLM client; pin prompt contract |
| `compliance.py` | Pre-trade compliance checks | TBD | 2026-Q4 | — |
| `ops_health_service.py` | Surfaces health to dashboard / alerts | TBD | 2026-Q4 | Integration test against `[execution_event_lag]` metrics |
| `ops_log_prefixes.py` | Phase-C registry (this file was just added) | Phase-C author | 2026-Q3 | 1 contract test: each constant is non-empty `[name]` and unique |

## Tested surfaces (sample)

These services have multiple test files or direct coverage — left here so the "go fix coverage" queue doesn't accidentally reopen already-solved ground.

- `auto_trader.py` — `test_auto_trader_integration.py`, `test_auto_trader_safety.py`, `test_auto_trader_monitor.py`, `test_auto_trader_synergy.py`, `test_auto_trader_rules.py`
- `bracket_reconciler.py` / `bracket_reconciliation_service.py` — `test_bracket_reconciliation_*.py` suite
- Venue adapters — `test_venue_*.py` (Phase B added fault-injection coverage)
- `learning.py` / `learning_predictions.py` — `test_trading_brain_*_dual_write.py`, `_read_phase5.py`, `_authority_phase7.py`, `_observability_phase6.py`
- `governance.py` — `test_governance_daily_loss.py` (but the breaker surface of portfolio_risk is still UNMAPPED — see above)

## Second tier — UNMAPPED but lower-risk

The remaining ~100 UNMAPPED services are mostly:
- Helpers / utilities (snapshot aggregators, formatters, trivial wrappers)
- Read-only analysis modules (backtest_provenance, pattern_stats_recompute, …)
- Scheduler-triggered background jobs with isolated surfaces

Full list (alphabetical; populated by grep of `app/services/trading/` against `tests/test_*.py`):

ai_context, alert_formatter, alerts, alpha_decay, attribution_service, autopilot_scope, autotrader_desk, backtest_asset_cleanup, backtest_engine, backtest_provenance, backtest_queue, backtest_queue_worker, batch_job_constants, bracket_intent, brain_assistant, brain_assistant_context, brain_batch_job_log, brain_resource_budget, breadth_relstr_service, broker_account_repair, capacity_governor, cross_asset_service, daily_playbook, data_quality, data_retention, decision_ledger, deployment_ladder_service, divergence_service, evolution_objective, execution_family_registry, execution_realism_service, expectancy_service, experiment_tracker, feature_parity, indicator_core, insight_backtest_panel_sync, intraday_session_service, intraday_signals, journal, learning_cycle_report, management_scope, market_analysis, microstructure, mining_validation, model_registry, mtf_consensus, ohlcv_aggregate_fetch, opportunity_scoring, paper_trading, parameter_stability, pattern_adjustment_advisor, pattern_condition_monitor, pattern_evidence_service, pattern_evolution_apply, pattern_ml, pattern_position_monitor, pattern_recognition, pattern_regime_autopilot_model, pattern_regime_autopilot_service, pattern_regime_killswitch_model, pattern_regime_killswitch_service, pattern_regime_ledger_lookup, pattern_regime_m2_common, pattern_regime_performance_service, pattern_regime_promotion_model, pattern_regime_promotion_service, pattern_regime_tilt_model, pattern_regime_tilt_service, pattern_stats_recompute, pattern_trade_analysis, pattern_trade_features, pattern_trade_ml, pattern_trade_storage, pattern_validation_projection, performance_attribution, portfolio, portfolio_optimizer, prescreen_internal_signals, prescreen_job, prescreen_normalize, prescreener, price_bus, public_api, pullback_detector, regime, regime_mining, reproducibility, runtime_mode_override, runtime_status, runtime_surface_state, scanner, scoring, selection_bias, sentiment, shadow_testing, signal_explainability, snapshot_bar_ops, statistical_pattern_hypotheses, stored_backtest_rerun, strategy_proposals, thesis, ticker_regime_service, top_picks, trade_plan_extractor, universe_snapshot, vol_dispersion_service, web3_service, web_pattern_researcher

**Rule of engagement:** the next engineer to touch any of these owns adding at least a smoke test that exercises the public entry point. "Add a test when you change the code" is cheaper than a dedicated coverage sprint.

## How to regenerate this list

```bash
cd /path/to/chili-home-copilot
# Total services
ls app/services/trading/*.py | xargs -n1 basename | sed 's/\.py$//' | grep -v '^__init__$' | wc -l

# Unmapped (zero test-file substring matches)
for svc in $(ls app/services/trading/*.py | xargs -n1 basename | sed 's/\.py$//'); do
  [ "$svc" = "__init__" ] && continue
  n=$(ls tests/test_*.py 2>/dev/null | grep -ci "$svc")
  [ "$n" -eq 0 ] && echo "$svc"
done
```

Run `conda run -n chili-env pytest --cov=app/services/trading --cov-report=term-missing` for a real-coverage view (Phase-C dev deps added `pytest-cov`).

## Related

- `docs/TRADING_SLO.md` — what the test coverage should protect
- `docs/CONTRIBUTOR_SAFETY.md` — soak gates + release-blocker scripts
- `CLAUDE.md` Hard Rule 4 — tests must use a `_test`-suffixed DB
