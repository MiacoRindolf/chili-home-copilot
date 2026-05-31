# CC Report: f-position-identity-phase-5w-monitor-read-helper-conversion

Date: 2026-05-30
Status: SHIPPED

## Summary

Converted the two Phase 5V parity-proven monitor read surfaces to management-envelope helpers while preserving public response contracts.

Converted:

- `api_monitor_decisions(...)`
- `api_monitor_imminent_alerts(...)`

Not touched:

- `api_monitor_run(...)`
- active setup cards
- `api_sell_trade(...)`
- stop execution or stop-position rendering
- broker/order/close/reconcile/PDT/capital-gate behavior
- public `/trades`, `trade_id`, schema class names, or UI labels

## Changes

- Added `load_monitor_decision_envelope_rows(...)` in `app/services/trading/management_envelopes.py`.
- Added `load_imminent_alert_actioned_envelope_ids(...)` in `app/services/trading/management_envelopes.py`.
- Routed `api_monitor_decisions(...)` through the helper while preserving:
  - `decisions`
  - `trade_id`
  - `ticker`
  - `direction`
  - pagination fields
- Routed `api_monitor_imminent_alerts(...)` through the actioned-alert helper while preserving the existing alert payload.

## Verification

- `python -m py_compile app/services/trading/management_envelopes.py app/routers/trading_sub/monitor.py scripts/d-phase5v-monitor-read-parity-probe.py`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test python -m pytest tests/test_management_envelopes.py tests/test_phase5w_monitor_read_conversion.py tests/test_phase5v_monitor_read_parity_probe.py tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`
  - Result: `28 passed, 1 warning`
- `python scripts/d-phase5v-monitor-read-parity-probe.py`
  - Result: `COMPLETE_POSITIVE`
  - `PARITY_CHECKS=20`
  - `PARITY_MISMATCHES=0`
- `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
  - Result: `orm_trade_symbol_compat | 94`
  - Raw reader bucket remains `(none) | 0`

## Architect Notes

The focused ORM-symbol count remains 94 because `monitor.py` still legitimately owns `Trade` objects for active setup cards and `api_monitor_run(...)`, and `trades.py` still owns public `/trades` behavior. This slice reduces internal read coupling without changing those public/live contracts.

`api_stop_decisions(...)` is not included. Phase 5V found the old compatibility-view stop-decision join exceeds the read-only parity timeout in live data, so it needs a separate performance-aware design rather than being bundled into this conversion.

## Next

`f-position-identity-phase-5x-stop-decision-read-design`

Investigate the slow old stop-decision join and decide whether to:

1. Convert `api_stop_decisions(...)` directly to an envelope helper with response parity tests, or
2. Leave it as a public compatibility surface until a bounded old-vs-new probe can prove equivalence.
