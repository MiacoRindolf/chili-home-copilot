# NEXT_TASK: f-phase5ab-trading-scheduler-contract-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading_scheduler.py` and decide whether any remaining
`Trade` ORM surface is safe for a narrow management-envelope helper conversion.

## Why This Is Next

Phase 5Z/5AA cleared the scanner false positive and converted the market-data
quote anchor after a parity probe. The next named candidate is
`trading_scheduler.py`, but it coordinates live monitor timing around price,
stop, and broker-position work. It should not be mechanically converted.

## Scope

- Classify each remaining `Trade` reference in `trading_scheduler.py`.
- Identify whether any reference is pure reporting/diagnostic read-only work.
- If all references feed live monitor behavior, close the task as an audit and
  queue a dedicated parity probe instead of converting.

## Guardrails

- No broker/order/close/reconcile changes.
- No stop execution/evaluation changes.
- No scheduler cadence changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.

## Exit Criteria

- Either one tiny passive conversion ships with tests, or the task closes with
  a documented deferral and next parity-probe brief.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.

