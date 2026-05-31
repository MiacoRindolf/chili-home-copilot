# NEXT_TASK: f-phase5o-brain-plasticity-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/brain_neural_mesh/plasticity.py`, the next Phase 5O
adapter candidate after `brain_neural_mesh/action_handlers.py` was reclassified
as a live-action future rename blocker.

## Why This Is Next

The remaining Phase 5O adapter candidates are increasingly close to lifecycle
and live decision surfaces. `brain_neural_mesh/plasticity.py` is adjacent to
action handling and may consume realized outcomes or mutate mesh weights. It
should receive an evidence-first audit before any rename/conversion pressure.

Current surface after the brain action-handlers audit:

```text
orm_trade_symbol_compat = 66
learning_research_reporting = 6
live_action_broker_reconcile = 20
private_helper_type_only = 5
risk_capital_gate = 21
adapter_candidate = 7
future_rename_blocker = 43
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `brain_neural_mesh/plasticity.py`.
- Determine whether the references are passive learning/reporting reads,
  realized-outcome evidence, mesh weight mutation inputs, lifecycle inputs, or
  action-adjacent runtime state.
- If passive and covered by tests, add a small safe helper/adapter conversion.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live alert cadence, order, stop, close, broker, reconcile,
  risk/capital/PDT, lifecycle, or portfolio behavior change without parity
  evidence.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; while PM/control-plane governance
  remains frozen, push evidence branches only and do not force a merge/deploy.
- Source posture is currently unstable due to an external dirty-root app-service
  restart loop; do not restart Postgres or clean the dirty root. Treat source
  posture evidence honestly if it remains alerting.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
