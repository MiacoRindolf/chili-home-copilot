# NEXT_TASK: f-phase5w-learning-reporting-adapter-slice-8

STATUS: QUEUED

## Goal

Convert one more actual read-only learning/reporting `Trade` ORM consumer to a
semantic management-envelope helper.

## Current State

Phase 5V removed four false-positive/text-only learning/reporting references.

Remaining compatibility surface:

```text
orm_trade_symbol_compat     | 75
adapter_candidate           | 26
learning_research_reporting | 21
future_rename_blocker       | 33
leave_alone                 | 16
```

## Recommended Work Shape

1. Re-run the candidate list from
   `docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`.
2. Pick a true read-only aggregate/reporting consumer with clear row-shape
   parity.
3. Add or extend a helper in `management_envelopes.py`.
4. Keep focused tests on fake DB rows where possible; use `chili_test` only
   when behavioral parity genuinely needs ORM fixtures.

## Candidate Guidance

- Avoid `alpha_decay.py` and `stale_promoted_sweep.py` until decay/lifecycle
  parity is explicitly scoped.
- Avoid `learning.py` broad conversions; it mixes read-only stats with
  load-bearing writer paths.
- Treat `net_edge_ranker.py` carefully: it is read-heavy, but it can feed edge
  ranking behavior. Convert only with parity tests for realized outcome rows.
- Avoid broker/order/close/reconcile/risk/capital paths.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.
