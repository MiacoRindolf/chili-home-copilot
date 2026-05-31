# NEXT_TASK: f-phase5ab-b-trading-scheduler-scope-conversion

STATUS: QUEUED

## Goal

Convert only the scheduler selection queries proven by the Phase 5AB parity
probe from direct `Trade` ORM reads to management-envelope helper reads.

## Evidence

Phase 5AB live probe:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
SCHEDULER_SCOPE_CHECKS=9
SCHEDULER_SCOPE_MISMATCHES=0
```

The current `trading_trades` compatibility-view scopes and the candidate
`trading_management_envelopes` scopes matched exactly.

## Scope

Create small helpers for the exact scheduler selections:

- price-monitor user ids
- price-monitor pattern tickers
- broker-backed monitor user ids
- broker-backed pattern tickers
- daytrade fast-monitor user ids
- crypto stop-monitor user ids / counts
- pattern-position monitor user ids
- event-driven pattern trigger trade ids/objects if the downstream API can
  accept envelope-shaped runtime objects without changing behavior

## Guardrails

- No scheduler cadence changes.
- No stop evaluation or dispatch behavior changes.
- No broker/order/close/reconcile changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- If any downstream function requires actual SQLAlchemy `Trade` identity
  semantics, stop and narrow the conversion to user/ticker selections only.

## Exit Criteria

- Focused tests pin old/new scheduler scope parity.
- Phase 5AB probe remains `COMPLETE_POSITIVE` after conversion.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.

