# NEXT_TASK: f-position-identity-phase-5v-monitor-read-parity-probe

STATUS: PENDING

## Goal

Build a read-only old-vs-new parity probe for the remaining monitor/router read candidates before converting any code away from `Trade` ORM symbols.

Phase 5U found that the remaining router/schema/UI surface is mostly public compatibility or live behavior. The only plausible narrow candidates are monitor read surfaces that can be compared safely first.

## Recommended Work Shape

1. Add a read-only probe script, likely `scripts/d-phase5v-monitor-read-parity-probe.py`.
2. Compare old `Trade`-based logic vs envelope-based SQL for:
   - `api_monitor_decisions(...)`: decision count and returned decision ids for representative users/actions.
   - `api_monitor_imminent_alerts(...)`: actioned-alert exclusion set.
   - Optional: `api_stop_decisions(...)`: stop-decision ids for current user and optional `trade_id`.
3. Emit a compact verdict:
   - `COMPLETE_POSITIVE` when all compared result sets match.
   - `MISMATCH` with counts/examples when not.
4. Add focused tests for the probe's comparison logic.
5. If parity is green, queue Phase 5W as the actual narrow conversion. Do not convert in this probe slice unless the evidence is trivial and fully pinned.

## Guardrails

- Read-only only.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not touch broker/order/close/reconcile/PDT/capital-gate behavior.
- Do not touch `api_monitor_run(...)`, `api_sell_trade(...)`, or stop execution.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

This is the correct next move because it turns the last plausible router helper candidate into measurable evidence. If the probe is green, conversion can be surgical. If it is not, the public/live contracts stay untouched.
