# NEXT_TASK: f-position-identity-phase-5z-stop-position-runtime-adapter-probe

STATUS: PENDING

## Goal

Build a read-only parity probe for a candidate stop-position runtime-envelope adapter.

Phase 5Y audited `/api/trading/stops/positions` and rejected a blind conversion. The endpoint is risk-facing UI and its helper chain expects `Trade`-like runtime objects. The next safe move is to prove whether a management-envelope row object can flow through the same helper chain and serialize the same payload.

## Recommended Work Shape

1. Add a small internal adapter, not a public rename:
   - read open rows from `trading_management_envelopes`
   - expose attributes with the names the existing helper chain expects
   - do not mutate through this adapter
2. Add a read-only probe that compares:
   - current `Trade` ORM stop-position serialization
   - candidate adapter stop-position serialization
3. Compare the exact public payload fields:
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
4. If parity is clean, queue the endpoint conversion as Phase 5Z-B.
5. If parity fails, keep the endpoint on the compatibility ORM and document the exact blockers.

## Guardrails

- Probe only; do not swap the endpoint yet.
- Do not touch stop execution, stop evaluation, or stop dispatch.
- Do not touch `api_monitor_run(...)`, active setup cards, `api_sell_trade(...)`, broker/order/close/reconcile/PDT/capital-gate behavior.
- Preserve all public response fields and UI semantics.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

This is the right bridge. It gives us data-science evidence on whether the stop-position surface is convertible without betting live risk display on an untested object contract.
