# Phase 5AA-A — Active Setup Runtime-Adapter Probe

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** read-only parity probe; no endpoint behavior changed

## Summary

Added `scripts/d-phase5aa-active-setup-runtime-adapter-probe.py`, a read-only parity probe for the active-setup monitor-card runtime object contract. The probe compares the current `Trade` ORM object path with candidate runtime objects loaded from the physical `trading_management_envelopes` table, then feeds both through the same serializer logic used by the active setup card.

Live result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
matched=true
old_setups=5
new_setups=5
old_suppressed=0
new_suppressed=0
quote_cache_entries=5
batch_cache_entries=1
trading_management_envelopes=r
trading_trades=v
```

## What The Probe Covers

- Active setup public card fields, including `trade_id`, `ticker`, plan fields, stop/target, current quote, broker-truth overlays, latest/recent monitor decisions, and execution-state metadata.
- Broker-stale suppression via `filter_broker_stale_open_trades`.
- Broker-position truth metrics via `broker_position_display_metrics`.
- Option/standard quote routing via `broker_quote_for_trade` and batch quote fallback.
- Pattern and breakout-alert enrichment.
- Suppressed-stale row parity.

## Guardrails Preserved

The probe did not touch:

- `api_monitor_run`
- sell/close paths
- stop execution/evaluation/dispatch
- broker/order/reconcile/PDT/capital-gate behavior
- `/trades`, `trade_id`, schema names, UI labels, or compatibility view semantics

## Verification

```text
python -m py_compile scripts\d-phase5aa-active-setup-runtime-adapter-probe.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5aa_active_setup_runtime_adapter_probe.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 13 passed

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
python scripts\d-phase5aa-active-setup-runtime-adapter-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: 0
```

## Architect Verdict

The active setup card is ready for a narrow Phase 5AA-B conversion using the proven runtime-envelope object. Keep the serializer/helper chain intact and change only the object loader for `api_monitor_active`. Do not touch monitor-run, sell/close, stop execution, broker/order/reconcile, PDT, capital gates, public `/trades`, or UI/schema labels.

