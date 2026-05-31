# Phase 5AC - Live Action Compatibility Boundary Audit

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** Audit/tooling only; no live trading behavior changed

## Summary

Phase 5AA and Phase 5AB converted the last two parity-proven display loaders:

- active setup cards
- AutoTrader desk live display rows

Phase 5AC audited what remains. The result is clean but important: the system is at an intentional compatibility boundary, not at a safe broad-rename point.

There are no unexpected runtime raw readers left against `trading_trades`, and there are no unexpected runtime mutations. The remaining surface is mostly the legacy `Trade` ORM class symbol serving public contracts, live broker/order/reconcile paths, risk gates, and research/reporting code.

## Analyzer Result

```text
bucket | files
-------+------
allowed_compatibility_writer_update | 4
compatibility_migration_test_history | 221
compatibility_relation_symbol | 2
docs_runbooks | 198
orm_trade_symbol_compat | 94

raw reader bucket | files
------------------+------
(none) | 0

unexpected_runtime_readers = []
unexpected_runtime_mutations = []
```

Phase 5AC added machine-readable grouping for the 94 remaining `Trade` ORM-symbol compatibility references:

```text
orm contract group | files
-------------------+------
learning_research_reporting | 39
live_action_broker_reconcile | 15
private_helper_type_only | 8
public_ui_schema_contract | 14
risk_capital_gate | 18
```

## Go / No-Go Matrix

| Move | Verdict | Reason |
|---|---:|---|
| Drop `trading_trades` compatibility view | NO | Public and writer compatibility contracts still intentionally rely on it. |
| Rename `Trade` ORM class broadly | NO | The remaining symbols touch live action, broker truth, PDT/capital gates, and public response vocabulary. |
| More raw reader cutovers | NO TARGET | Runtime raw-reader allowlist is already empty. |
| Convert more display loaders | NO TARGET | Active setup and AutoTrader desk display loaders are already moved. |
| Keep compatibility view + pin contracts | YES | This is the safest architecture boundary. |
| Future ORM rename spike | YES, audit-only first | Needs a deliberate alias/facade plan, not mechanical edits. |

## Contract Groups

### Public UI / Schema Contract - 14

These names are part of public routes, response schemas, templates, and UI vocabulary. Renaming them is a product/API compatibility decision, not a data-layer cleanup.

Examples: `app/routers/trading_sub/trades.py`, `app/schemas/trading.py`, `app/templates/trading/_tab_trades.html`.

### Live Action / Broker / Reconcile - 15

These paths are attached to order placement, exits, broker truth, bracket reconciliation, stop handling, and position repair. They should remain on the compatibility ORM until each path has its own parity probe and rollback story.

Examples: `app/services/broker_service.py`, `app/services/trading/stop_engine.py`, `app/services/trading/crypto/exit_monitor.py`.

### Risk / Capital Gate - 18

These paths can block or allow real capital. A rename-only patch here would still carry trading risk because identity semantics affect PDT, cash, exposure, portfolio risk, and fast-path gates.

Examples: `app/services/trading/compliance.py`, `app/services/trading/portfolio_risk.py`, `app/services/trading/fast_path/gates.py`.

### Learning / Research / Reporting - 39

These are lower blast-radius than live order paths, but still affect alpha evaluation, pattern promotion, realized-stat learning, drift detection, and evidence quality. They should move only behind semantic helper APIs when a specific metric needs cleanup.

Examples: `app/services/trading/learning.py`, `app/services/trading/tca_service.py`, `app/services/trading/pattern_trade_analysis.py`.

### Private Helper / Type-Only - 8

These are likely the eventual first rename candidates, but the value is mostly naming hygiene. There is no urgent trading or statistical gain from touching them now.

Examples: `app/services/trading/autotrader_desk.py`, `app/services/trading/autopilot_scope.py`.

## Architect Verdict

Do not push the full rename now.

The data model migration has reached the useful boundary: runtime raw readers are gone, high-value display loaders are on the envelope table, reader-cutover canaries are in place, and the compatibility view remains as a deliberate adapter for live writers, public APIs, and legacy ORM vocabulary.

The next safe slice is not another conversion. It is an audit-only ORM alias plan: define what a future `ManagementEnvelope` ORM class would look like, which public names stay `Trade`, which live-action paths need parity probes, and how to keep `trade_id` stable as an external/business key.

## Verification

```text
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: (none) | 0
# unexpected runtime readers/mutations: none

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 10 passed
```

## Files Changed

- `scripts/analyze_phase5_remaining_trade_refs.py`
- `tests/test_phase5_remaining_trade_refs.py`
