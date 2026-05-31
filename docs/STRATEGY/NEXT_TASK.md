# NEXT_TASK: f-phase5o-pattern-condition-monitor-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/pattern_condition_monitor.py`, the next Phase 5O
adapter candidate after `scanner.py` was closed as a false-positive source
token cleanup.

## Why This Is Next

The remaining Phase 5O adapter candidates are now smaller but still mixed
between true learning/reporting readers and possible behavior-bearing monitor
surfaces. `pattern_condition_monitor.py` is the next best slice because it is
pattern-monitor-adjacent and may influence pattern health or alert decisions.

Current surface after the scanner false-positive closeout:

```text
orm_trade_symbol_compat = 68
learning_research_reporting = 9
live_action_broker_reconcile = 19
private_helper_type_only = 5
risk_capital_gate = 21
adapter_candidate = 11
future_rename_blocker = 41
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `pattern_condition_monitor.py`.
- Determine whether the references are passive condition-health reads,
  alert/monitor decision inputs, lifecycle-sensitive evidence, or live
  gate-adjacent.
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

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
- Source posture remains `COMPLETE_POSITIVE`; if it drifts to the dirty root,
  correct only app services from the clean Phase5AB-D runtime worktree and do
  not restart Postgres.
