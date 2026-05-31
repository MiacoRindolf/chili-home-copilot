# NEXT_TASK: f-position-identity-phase-5z-b-stop-position-endpoint-conversion

STATUS: PENDING

## Goal

Convert `/api/trading/stops/positions` to load open runtime objects from `trading_management_envelopes`, using the Phase 5Z-A parity-proven adapter path.

Phase 5Z-A returned `COMPLETE_POSITIVE` on live data: the candidate management-envelope runtime object produced the same stop-position payload as the current `Trade` ORM runtime object for the live stop-position set.

## Recommended Work Shape

1. Add a small helper in `management_envelopes.py`:
   - loads open rows from `trading_management_envelopes`
   - returns read-only Trade-like runtime objects
   - does not mutate
2. Convert only `api_stop_positions(...)` to use that helper.
3. Keep the existing serializer logic and helper chain intact:
   - `filter_broker_stale_open_trades(...)`
   - `broker_position_display_metrics(...)`
   - `is_option_trade(...)`
   - `broker_quote_for_trade(...)`
   - `fetch_quote(...)`
   - `_build_brain_context(...)`
4. Run the existing stop-position tests and the Phase 5Z-A parity probe after conversion.

## Guardrails

- Do not touch stop execution, stop evaluation, or stop dispatch.
- Do not touch `api_monitor_run(...)`, active setup cards, `api_sell_trade(...)`, broker/order/close/reconcile/PDT/capital-gate behavior.
- Preserve all public response fields and UI semantics.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

Green light for a narrow conversion. The parity probe earned it.
