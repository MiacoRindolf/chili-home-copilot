# NEXT_TASK: f-phase5p-learning-reporting-adapter-slice

STATUS: QUEUED

## Goal

Convert one small read-only learning/reporting `Trade` ORM consumer to an
envelope-shaped adapter/helper with direct parity tests.

## Current State

Phase 5O mapped the remaining 93 ORM compatibility files:

```text
adapter_candidate     | 44
future_rename_blocker | 33
leave_alone           | 16
```

The safest adapter candidates are read-only learning/reporting consumers. They
do not place orders, close positions, reconcile broker state, or gate capital.

## Recommended Work Shape

1. Pick one small `learning_research_reporting` file from
   `docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`.
2. Prefer a pure read/query/report helper with existing focused tests.
3. Add a local adapter/helper that accepts envelope-shaped fields instead of
   relying directly on the `Trade` ORM symbol.
4. Prove old/new parity with direct tests.
5. Re-run:
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5o_remaining_runtime_compat_map.py`
   - Phase 5M/N source-posture watch

## Guardrails

- No broker/order/close/reconcile changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.

