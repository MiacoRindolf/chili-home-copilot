# NEXT_TASK: f-phase5o-momentum-neural-live-runner-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/momentum_neural/live_runner.py`, the next Phase 5O
adapter candidate after `pattern_condition_monitor.py` was closed as a
false-positive source token cleanup.

## Why This Is Next

The remaining Phase 5O adapter candidates are increasingly close to live
decision surfaces. `momentum_neural/live_runner.py` is currently classified as
`learning_research_reporting / adapter_candidate`, but its runtime role and
name suggest possible live-selection influence. It should receive an
evidence-first audit before any rename/conversion pressure.

Current surface after the pattern-condition monitor false-positive closeout:

```text
orm_trade_symbol_compat = 67
learning_research_reporting = 8
live_action_broker_reconcile = 19
private_helper_type_only = 5
risk_capital_gate = 21
adapter_candidate = 10
future_rename_blocker = 41
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `momentum_neural/live_runner.py`.
- Determine whether the references are passive neural/reporting reads, live
  candidate selection, entry gating, or action-adjacent runtime state.
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
