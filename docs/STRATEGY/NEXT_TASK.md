# NEXT_TASK: f-phase5r-learning-reporting-adapter-slice-3

STATUS: QUEUED

## Goal

Convert one more small read-only learning/reporting `Trade` ORM consumer to an
envelope-shaped adapter/helper with direct parity tests.

## Current State

Phase 5Q converted TCA summary reporting to a management-envelope helper and
reduced the remaining ORM compatibility surface:

```text
orm_trade_symbol_compat     | 91
adapter_candidate           | 42
learning_research_reporting | 37
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

Good candidate families:

- Read-only performance/diagnostic aggregates that summarize closed envelopes.
- Reporting helpers that do not write pattern state.
- False-positive cleanup only when the symbol is plainly type/comment text and
  tests pin that no runtime behavior changed.

Avoid for this slice:

- `stale_promoted_sweep.py` and other lifecycle mutators.
- `auto_trader_*`, `pattern_imminent_alerts.py`, `market_data.py`, and open
  live monitor paths.
- Broker/order/close/reconcile, PDT, capital, and risk gate surfaces.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.
