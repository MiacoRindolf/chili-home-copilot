# Phase 5Z-B: Stop-Position Endpoint Conversion

Date: 2026-05-30

## Summary

Converted `/api/trading/stops/positions` to load open runtime objects from `trading_management_envelopes`.

This uses the Phase 5Z-A parity-proven runtime-envelope object path. The endpoint's serializer and helper chain remain intact:

- `filter_broker_stale_open_trades(...)`
- `broker_position_display_metrics(...)`
- `is_option_trade(...)`
- `broker_quote_for_trade(...)`
- `fetch_quote(...)`
- `_build_brain_context(...)`

No stop execution, stop evaluation, stop dispatch, sell/close, broker/order/reconcile/PDT/capital gate, `/trades`, schema-name, UI-label, or response-field behavior changed.

## What Changed

- Added `load_open_stop_position_envelope_objects(...)` in `management_envelopes.py`.
- The helper reads open rows from `trading_management_envelopes` and exposes read-only Trade-like attributes via `SimpleNamespace`.
- `api_stop_positions(...)` now calls the helper instead of querying `Trade` directly.
- The Phase 5Z parity probe now imports the same helper used by the endpoint.

## Public Contract Preserved

The endpoint still returns:

- `positions`
- `suppressed_stale_trades`
- `suppressed_stale_count`

Position row fields remain:

- `id`
- `ticker`
- `asset_type`
- `direction`
- `entry_price`
- `current_price`
- `stop_loss`
- `take_profit`
- `trail_stop`
- `high_watermark`
- `stop_model`
- `quantity`
- `broker_source`
- broker-truth fields
- `R`
- `current_r`
- `stop_distance_pct`
- `pnl_pct`
- `state`
- `entry_date`
- `brain`

## Verification

- `python -m py_compile app/services/trading/management_envelopes.py app/routers/trading.py scripts/d-phase5z-stop-position-runtime-adapter-probe.py`
- `pytest tests/test_management_envelopes.py tests/test_phase5z_stop_position_endpoint_conversion.py tests/test_phase5z_stop_position_runtime_adapter_probe.py -q`
  - Result: 22 passed, 1 existing SQLAlchemy warning
- `pytest tests/test_stop_engine_options_auto_exec.py::test_stop_positions_option_uses_premium_quote_not_underlying tests/test_stop_engine_options_auto_exec.py::test_stop_positions_use_broker_truth_and_hide_duplicate_coinbase_envelope -q -vv`
  - Result: 2 passed
- `python scripts/d-phase5z-stop-position-runtime-adapter-probe.py`
  - `COMPLETE_POSITIVE`
  - matched=true
  - old positions=5
  - new positions=5
  - suppressed-stale drift=0
- `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
  - `orm_trade_symbol_compat | 94`
  - raw reader bucket: 0

Note: one broad parallel pytest command timed out because two pytest processes contended for the shared advisory DB lock. The affected stop-position tests were re-run serially and passed.

## Architect Verdict

This is the right kind of endpoint conversion: evidence first, then a narrow swap that leaves the risk logic and public payload untouched.

Next should be another contract audit/probe, not an ORM class rename. The remaining router/desk surfaces include active setup cards, monitor-run, sell/close, `/trades`, and user-facing trade language; each needs its own parity gate.
