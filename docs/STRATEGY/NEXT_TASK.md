# NEXT_TASK: f-phase5x-learning-reporting-adapter-slice-9

STATUS: QUEUED

## Goal

Convert one more actual read-only learning/reporting `Trade` ORM consumer to a
semantic management-envelope helper, or close another small false-positive
cluster if inspection proves no runtime dependency exists.

## Current State

Phase 5W converted `net_edge_ranker._load_training_pairs(...)` live training
rows to `load_net_edge_training_envelope_rows(...)`.

Remaining compatibility surface:

```text
orm_trade_symbol_compat     | 74
adapter_candidate           | 25
learning_research_reporting | 20
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
- Avoid broad `learning.py` conversions; it mixes read-only stats with
  load-bearing writer paths.
- `setup_vitals.py` has a small open-ticker query that may be a good next
  candidate if it is reporting-only.
- `regime_classifier.py` has a closed-trade performance reader; convert only
  with parity tests if it does not feed a live gate.
- Avoid broker/order/close/reconcile/risk/capital paths.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.
