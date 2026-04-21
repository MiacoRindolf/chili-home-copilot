# Trading brain — contributor safety guide

**Read this before your first PR that touches anything under `app/services/trading/` or `app/trading_brain/`.** CHILI trades real money via Robinhood and Coinbase. A correctness regression in the wrong place is a capital-loss event, not a UX papercut.

## TL;DR

1. Read [CLAUDE.md](../CLAUDE.md) — the "Hard rules" section. Six items. All six are non-negotiable.
2. If your change touches a hot path, there's a [release-blocker PowerShell script](#release-blocker-scripts-28-of-them) that will tell you whether you broke the contract. Run it before merging.
3. If you're changing the prediction mirror (`app/trading_brain/`), go through [phase rollback runbook](PHASE_ROLLBACK_RUNBOOK.md) even if you're not rolling back — the flags + soak contract apply to forward migrations too.
4. Tests must use a `_test`-suffixed DB. The conftest guard will hard-error if you forget. **Hard Rule 4.**
5. Never rename a log prefix. It breaks the alert rules listed in [TRADING_SLO.md](TRADING_SLO.md). Use `app/services/trading/ops_log_prefixes.py` for any new prefix.

## The "do not touch without a new phase" list

These surfaces are frozen by contract. Changes require a new phase with design + tests + soak + rollout doc, signed off before merge:

| Surface | Why it's frozen | Where it lives |
|---|---|---|
| Prediction-mirror authority contract | Phase 7 frozen; changing it would invalidate phases 3-8 | `app/trading_brain/infrastructure/prediction_read_phase5.py`, `prediction_mirror_session.py` |
| `[chili_prediction_ops]` log line format | Ops alert rules + Phase-7 release blocker grep pattern-match on the exact string | `app/trading_brain/infrastructure/prediction_ops_log.py` + `app/services/trading/ops_log_prefixes.py::PREDICTION_OPS` |
| Kill-switch DB persistence schema | `trading_risk_state` rows must round-trip across restart (Hard Rule 1) | `app/services/trading/governance.py::_persist_kill_switch_state` |
| Drawdown breaker DB persistence schema | Same as kill switch (Hard Rule 2) | `app/services/trading/portfolio_risk.py::_persist_breaker_state` |
| Migration ID uniqueness | Reusing an ID silently re-runs on fresh envs and no-ops on existing ones (Hard Rule 6) | `app/migrations.py::_assert_migration_ids_unique` |
| `venue_order_idempotency` DB table schema | Durable client_order_id guard survives restart | `app/services/trading/venue/idempotency_store.py` |

## Before you merge: the safety gates

### 1. Tests

```bash
conda activate chili-env
set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test  # or export on bash
pytest tests/ -v
```

If you only touched one surface, scope the run: `pytest tests/test_<area>*.py -v`. For the full trading stack: `pytest tests/ -v --cov=app/services/trading --cov=app/trading_brain --cov-report=term-missing`.

### 2. Release-blocker scripts (28 of them)

Every ships-blocking check in the trading surface has a PowerShell script in `scripts/`. If your change touches the corresponding area, run its script against recent `docker compose logs` before merging.

Index (as of Phase C):

| Script | Area | What it checks |
|---|---|---|
| `check_bracket_reconciliation_release_blocker.ps1` | Bracket reconciliation | `[bracket_reconciliation]` sweep correctness |
| `check_breadth_relstr_release_blocker.ps1` | Market breadth signal | snapshot freshness |
| `check_capital_reweight_release_blocker.ps1` | Capital reweight model | prediction stability |
| `check_chili_prediction_ops_release_blocker.ps1` | **Phase 7 authority** | Hard stop: `read=auth_mirror` + `explicit_api_tickers=false` → block |
| `check_cross_asset_release_blocker.ps1` | Cross-asset regime | signal freshness |
| `check_divergence_release_blocker.ps1` | Divergence panel | metric freshness |
| `check_drift_monitor_release_blocker.ps1` | Drift monitor | escalation count within budget |
| `check_execution_cost_release_blocker.ps1` | Execution cost model | TCA sanity |
| `check_exit_engine_release_blocker.ps1` | Exit engine | stop decisions present for all opens |
| `check_intraday_session_release_blocker.ps1` | Intraday session snapshot | session boundary correctness |
| `check_ledger_release_blocker.ps1` | Economic ledger | PnL reconciliation |
| `check_live_brackets_release_blocker.ps1` | Live bracket reconciliation | orphan / missing stop count |
| `check_macro_regime_release_blocker.ps1` | Macro regime snapshot | signal freshness |
| `check_net_edge_ranker_release_blocker.ps1` | Net edge ranker | ranking stability |
| `check_ops_health_release_blocker.ps1` | Ops health service | SLO breach rate |
| `check_pattern_regime_autopilot_release_blocker.ps1` | M.2 autopilot | eligibility predicate |
| `check_pattern_regime_killswitch_release_blocker.ps1` | M.2 kill-switch | kill-switch consistency |
| `check_pattern_regime_perf_release_blocker.ps1` | M.1 perf ledger | cell-confidence integrity |
| `check_pattern_regime_promotion_release_blocker.ps1` | M.2 promotion | promotion decision consistency |
| `check_pattern_regime_tilt_release_blocker.ps1` | M.2 tilt | tilt integrity |
| `check_pit_release_blocker.ps1` | Point-in-time hygiene | PIT contract |
| `check_position_sizer_release_blocker.ps1` | Position sizer | sizing bounds |
| `check_recert_queue_release_blocker.ps1` | Recert queue | backlog shape |
| `check_risk_dial_release_blocker.ps1` | Risk dial | dial value bounds |
| `check_ticker_regime_release_blocker.ps1` | Ticker-level regime | snapshot freshness |
| `check_triple_barrier_release_blocker.ps1` | Triple-barrier labels | label integrity |
| `check_venue_truth_release_blocker.ps1` | Venue truth log | broker-truth drift |
| `check_vol_dispersion_release_blocker.ps1` | Volatility dispersion | metric freshness |

**Usage pattern:**

```powershell
docker compose logs chili --since 30m 2>&1 | .\scripts\check_<area>_release_blocker.ps1
# or against a saved file:
.\scripts\check_<area>_release_blocker.ps1 -Path .\saved.log
```

Exit 0 = clean. Exit 1 = blocker match found (stderr lists them). Exit 2 = file not found.

If your change doesn't touch a specific area, you don't need to run all 28. Match the script to the surface you changed.

### 3. Phase rollouts — the special case

If you're flipping a prediction-mirror flag (`BRAIN_PREDICTION_*_ENABLED`), the phase rollout runbook takes precedence over everything else:

1. Read [PHASE_ROLLBACK_RUNBOOK.md](PHASE_ROLLBACK_RUNBOOK.md)
2. Run `.\scripts\check_chili_prediction_ops_release_blocker.ps1` against the soak logs
3. Confirm soak duration matches the rollout doc
4. Only then merge

For incident response (not rollout): `.\scripts\rollback-prediction-mirror.ps1` (idempotent, supports `-WhatIf`).

### 4. Migrations

```powershell
.\scripts\verify-migration-ids.ps1
```

Fast check; runs the same `_assert_migration_ids_unique` that executes at app startup. If you added a migration, run this before submitting the PR.

### 5. Kill-switch / drawdown-breaker incident runbooks

- [KILL_SWITCH_RUNBOOK.md](KILL_SWITCH_RUNBOOK.md) — activation / reset / audit
- [DRAWDOWN_BREAKER_RUNBOOK.md](DRAWDOWN_BREAKER_RUNBOOK.md) — incident playbook

These are for ops, not merge-time gates. Linking here so they're findable.

## Logging new prefixes

If your change adds a new structured log prefix:

1. Add a constant in `app/services/trading/ops_log_prefixes.py` with a docstring describing the surface, fields, and alert thresholds (if any)
2. Update `docs/TRADING_SLO.md` if the prefix implies an SLO
3. If the new prefix is release-gated, add a script under `scripts/check_<name>_release_blocker.ps1` following the existing pattern

Never introduce a free-floating `"[prefix]"` literal. The Phase-C audit caught 7 ad-hoc prefixes; keeping the registry pure prevents regression.

## The conservative default

When in doubt, gate your change behind a feature flag defaulting to False. Add the flag to `app/config.py`. Document rollout + rollback in the PR description. Phase B (broker-equity TTL cache) is the current template — see `chili_autotrader_broker_equity_cache_enabled`.

## See also

- [CLAUDE.md](../CLAUDE.md) — project overview + hard rules
- [docs/TRADING_SLO.md](TRADING_SLO.md) — what good looks like
- [docs/TRADING_SERVICE_TEST_MAP.md](TRADING_SERVICE_TEST_MAP.md) — test coverage baseline
- [docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md](TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md) — prediction mirror phases
- [app/trading_brain/README.md](../app/trading_brain/README.md) — phase tracker + flag defaults
