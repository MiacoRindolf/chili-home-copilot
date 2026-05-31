# NEXT_TASK: f-phase5q-learning-reporting-adapter-slice-2

STATUS: QUEUED

## Goal

Convert one more small read-only learning/reporting `Trade` ORM consumer to an
envelope-shaped adapter/helper with direct parity tests.

## Current State

Phase 5P reduced the remaining ORM compatibility surface:

```text
orm_trade_symbol_compat     | 92
adapter_candidate           | 43
learning_research_reporting | 38
future_rename_blocker       | 33
leave_alone                 | 16
```

The successful pattern was:

1. choose a read-only consumer
2. move the ticker/trade read behind a management-envelope helper
3. prove behavior with a focused test
4. update the Phase 5O map and canaries

## Recommended Work Shape

1. Pick another small `learning_research_reporting` file from the updated
   Phase 5O map.
2. Avoid anything that mutates pattern lifecycle or trading state, even if the
   current classifier places it in `learning_research_reporting`.
3. Add or reuse a management-envelope helper only for read-only fields.
4. Prove parity with direct tests.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.

