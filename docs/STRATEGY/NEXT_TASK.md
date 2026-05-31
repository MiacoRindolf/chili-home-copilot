# NEXT_TASK: f-position-identity-phase-5y-stop-position-contract-audit

STATUS: PENDING

## Goal

Audit the stop-position rendering surface before any further Phase 5 router conversion.

Phase 5X safely converted the stop-decision audit-history endpoint. The next nearby surface is `api_stop_positions(...)`, but that endpoint is closer to live risk display: it joins open management envelopes to current price, stop/target, broker truth, brain context, and suppression logic. Treat it as a contract audit first, not a blind helper rewrite.

## Recommended Work Shape

1. Read `app/routers/trading.py::api_stop_positions(...)` end to end.
2. Classify every `Trade` field it uses into:
   - pure management-envelope fields
   - broker-truth/current-position fields
   - stop/target/risk display fields
   - user-facing compatibility field names
3. Decide whether a management-envelope helper can preserve the exact response shape without touching execution or stop logic.
4. If safe, write a Phase 5Y-A parity probe first.
5. If not safe, leave the endpoint on the compatibility ORM and record the reason.

## Guardrails

- Do not touch stop execution.
- Do not touch stop evaluation or dispatch.
- Do not touch `api_monitor_run(...)`, active setup cards, `api_sell_trade(...)`, broker/order/close/reconcile/PDT/capital-gate behavior.
- Preserve public response fields and UI semantics for stop positions.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

This is worth auditing because the stop-position endpoint is one of the last router surfaces still coupled to the legacy ORM symbol. But it is also risk-facing UI, so the right next move is a narrow contract audit and parity probe, not an immediate conversion.
