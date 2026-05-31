# NEXT_TASK: f-phase5t-learning-reporting-adapter-slice-5

STATUS: QUEUED

## Goal

Convert one actual read-only learning/reporting `Trade` ORM consumer to a
management-envelope helper with focused parity tests.

## Current State

Phase 5S cleaned comment/docstring-only false positives from the Phase 5O map.
The remaining compatibility surface is now:

```text
orm_trade_symbol_compat     | 81
adapter_candidate           | 32
learning_research_reporting | 27
future_rename_blocker       | 33
leave_alone                 | 16
```

## Recommended Work Shape

1. Pick a small actual `learning_research_reporting` consumer that reads closed
   live rows and does not mutate pattern lifecycle or trading state.
2. Move the row source behind a semantic `management_envelopes` helper.
3. Prove the helper preserves the old output shape with direct tests.
4. Update the Phase 5O map and canaries.

Good candidates:

- Small closed-envelope cost/diagnostic summaries when mode is shadow/off and
  behavior is pinned with tests.
- Reporting-only helpers that produce dashboards or diagnostics.
- Research aggregates with no writer side effects.

Avoid:

- `stale_promoted_sweep.py` and other lifecycle mutators.
- `auto_trader_*`, `pattern_imminent_alerts.py`, `market_data.py`, and open
  live monitor paths.
- Broker/order/close/reconcile, PDT, capital, and risk gate surfaces.
- Anything that writes ScanPattern fields without an explicit parity test.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.
