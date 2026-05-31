# NEXT_TASK: f-phase5v-learning-reporting-adapter-slice-7

STATUS: QUEUED

## Goal

Convert one more low-risk learning/reporting `Trade` ORM consumer to a semantic
management-envelope helper, or deliberately close it as a false-positive cleanup
if inspection proves it has no real runtime `Trade` dependency.

## Current State

Phase 5U converted `edge_reliability.py` live evidence reads to
`load_edge_reliability_live_envelope_rows(...)`.

Remaining compatibility surface:

```text
orm_trade_symbol_compat     | 79
adapter_candidate           | 30
learning_research_reporting | 25
future_rename_blocker       | 33
leave_alone                 | 16
```

## Recommended Work Shape

1. Inspect the next learning/reporting candidates manually.
2. Prefer a true read-only aggregate/reporting consumer with simple row-shape
   parity.
3. Add or extend a helper in `management_envelopes.py`.
4. Keep focused tests on fake DB rows where possible; use `chili_test` only for
   behavioral parity that genuinely needs ORM fixtures.

## Candidate Notes

- Avoid `alpha_decay.py` and `stale_promoted_sweep.py` for now; they touch
  lifecycle/decay behavior.
- Avoid `economic_ledger.py` unless the slice has explicit ledger parity; it
  carries public-ish `trade_id` accounting semantics.
- `pattern_trade_analysis.py`, `realized_pnl_sql.py`, and
  `evidence_correction.py` may be false-positive/text-heavy surfaces. If so,
  close them as map hygiene rather than forcing a behavior change.
- `learning.py`, `net_edge_ranker.py`, and `pattern_imminent_alerts.py` need
  extra care because they can feed scoring or signal-generation behavior.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle demotion/promotion behavior changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No schema migration.
- Keep Phase 5M/N source-posture guard green.
