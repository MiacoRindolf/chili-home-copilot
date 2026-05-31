# NEXT_TASK: f-position-identity-phase-5w-monitor-read-helper-conversion

STATUS: PENDING

## Goal

Convert the two Phase 5V parity-proven monitor read surfaces from `Trade` ORM joins to management-envelope helper/SQL reads while preserving public response contracts.

Phase 5V proved old-vs-new parity for:

- `api_monitor_decisions(...)`
- `api_monitor_imminent_alerts(...)`

Live probe result: `COMPLETE_POSITIVE`, 20 checks matched, `PARITY_MISMATCHES=0`.

## Recommended Work Shape

1. Add narrow helper functions in `app/services/trading/management_envelopes.py` or a monitor-specific helper module:
   - monitor decision user/action scope
   - actioned imminent-alert exclusion scope
2. Convert only:
   - `app/routers/trading_sub/monitor.py::api_monitor_decisions(...)`
   - `app/routers/trading_sub/monitor.py::api_monitor_imminent_alerts(...)`
3. Preserve public response keys:
   - `decisions`
   - `trade_id`
   - `ticker`
   - `direction`
   - imminent alert payload shape
4. Re-run Phase 5V probe after conversion.
5. Keep `api_monitor_run(...)`, active setup cards, stop surfaces, and all live broker/order/close paths unchanged.

## Guardrails

- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not touch `api_monitor_run(...)`.
- Do not touch `api_sell_trade(...)`.
- Do not touch stop execution or stop-position rendering.
- Do not touch broker/order/close/reconcile/PDT/capital-gate behavior.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Do not include `api_stop_decisions(...)` in this conversion; its old compatibility-view join exceeded the Phase 5V timeout and needs a separate design.

## Architect Verdict

This is a small, evidence-backed conversion. It reduces router `Trade` coupling without changing public contracts or live trading behavior.
