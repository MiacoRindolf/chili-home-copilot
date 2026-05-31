# NEXT_TASK: f-position-identity-phase-5x-stop-decision-read-design

STATUS: PENDING

## Goal

Investigate the remaining stop-decision read surface before converting it away from `Trade` ORM joins.

Phase 5W converted the two parity-proven monitor read surfaces. The next plausible router read is `api_stop_decisions(...)`, but Phase 5V showed the old compatibility-view stop-decision join exceeds the live read-only probe timeout. That makes it a performance/design task, not a blind conversion.

## Recommended Work Shape

1. Profile the current `api_stop_decisions(...)` query shape:
   - old ORM join through `Trade`
   - equivalent envelope-table join
   - bounded recent-stop-decision sample
2. Determine why the old compatibility-view join is slow.
3. If the envelope-table query is strictly faster and response-equivalent, implement a narrow helper conversion with tests.
4. If old-vs-new parity cannot be proven cheaply, leave the endpoint on the compatibility contract and write the decision down.

## Guardrails

- Do not touch stop execution.
- Do not touch stop-position rendering.
- Do not touch `api_monitor_run(...)`, active setup cards, `api_sell_trade(...)`, broker/order/close/reconcile/PDT/capital-gate behavior.
- Preserve public response fields: `id`, `trade_id`, `as_of_ts`, `state`, `old_stop`, `new_stop`, `trigger`, `reason`, `executed`.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

This is worth investigating because the old stop-decision join timing out may be a real endpoint performance issue. But it must stay isolated from stop execution and active stop rendering.
