# NEXT_TASK: f-position-identity-phase-5aa-active-setup-contract-audit

STATUS: PENDING

## Goal

Audit the remaining active setup / monitor-card runtime object contracts before any further router conversion.

Phase 5Z-B converted the stop-position read after a dedicated parity probe. The next risk-facing surfaces are active setup cards and monitor-run/sell/close actions. Do not mechanically convert them. First classify their live object expectations and decide which pieces can use management-envelope runtime objects safely.

## Recommended Work Shape

1. Inspect active setup card and monitor-run surfaces:
   - `app/routers/trading_sub/monitor.py`
   - `app/services/trading/autotrader_desk.py`
   - related tests in `tests/test_monitor_api_execution_state.py` and `tests/test_autotrader_desk_api.py`
2. Classify each remaining `Trade` use:
   - passive display read
   - broker-truth/risk display
   - live action input
   - public API/UI compatibility contract
3. Only write probes for passive display reads.
4. Leave monitor-run, sell/close, broker/order/reconcile/PDT/capital-gate paths alone unless a later parity gate specifically proves safety.

## Guardrails

- Do not touch sell/close or monitor-run behavior.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

Phase 5 is now deep into user-facing and live-action territory. Continue with audits and probes, not broad rename sweeps.
