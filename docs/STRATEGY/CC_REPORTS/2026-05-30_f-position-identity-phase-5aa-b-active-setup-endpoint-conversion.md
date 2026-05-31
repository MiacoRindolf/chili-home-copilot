# Phase 5AA-B — Active Setup Endpoint Conversion

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** active setup display loader only

## Summary

Converted the active setup card endpoint (`/api/trading/monitor/active` and `/api/trading/active-setups`) to load read-only runtime objects from the physical `trading_management_envelopes` table.

The serializer and helper chain are unchanged:

- broker-stale filtering
- broker-position truth overlays
- option detection
- broker/market quote routing
- breakout-alert and pattern enrichment
- monitor decision enrichment
- execution-state metadata
- public payload fields and names

## What Changed

- Added `load_open_active_setup_envelope_objects(...)` to `app/services/trading/management_envelopes.py`.
- Added an active-card-only helper in `app/routers/trading_sub/monitor.py`.
- Swapped `api_monitor_active` to use the active-card envelope helper.

## Guardrails Preserved

No changes to:

- `api_monitor_run`
- sell/close behavior
- stop execution/evaluation/dispatch
- broker/order/reconcile/PDT/capital-gate behavior
- `/trades`, `trade_id`, schema names, UI labels, or response fields
- `trading_trades` compatibility view

`api_monitor_run` intentionally remains on `_monitored_open_trades(...)` and the old `Trade` helper path because it is a live action surface.

## Verification

```text
python -m py_compile app\routers\trading_sub\monitor.py app\services\trading\management_envelopes.py scripts\d-phase5aa-active-setup-runtime-adapter-probe.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5aa_active_setup_endpoint_conversion.py tests\test_phase5aa_active_setup_runtime_adapter_probe.py tests\test_management_envelopes.py tests\test_monitor_api_execution_state.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 50 passed

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
python scripts\d-phase5aa-active-setup-runtime-adapter-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE
# old_setups=5, new_setups=5, matched=true

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: 0
```

## Architect Verdict

This is the right depth of Phase 5 conversion: move proven display reads to the semantic envelope table while preserving live action paths. The next remaining user-facing surface is the AutoTrader desk position list. That surface needs an audit/probe first because it mixes display fields with override/close controls and broker truth.

