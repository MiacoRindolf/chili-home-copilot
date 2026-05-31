# NEXT_TASK: f-phase5o-pattern-position-monitor-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/pattern_position_monitor.py`, the next Phase 5O
adapter candidate after `autopilot_scope.py` was reclassified as a live
ownership/entry-scope gate.

## Why This Is Next

The scheduler handoff was already converted to pass envelope-shaped runtime
objects into the pattern monitor, but the monitor module itself still carries
legacy `Trade` ORM symbols and is classified as a learning/reporting adapter
candidate. That deserves a direct contract audit because anything named
`monitor` in this system can quietly influence stop/exit decisions.

Current surface after the autopilot-scope audit:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 12
live_action_broker_reconcile = 18
private_helper_type_only = 5
risk_capital_gate = 20
adapter_candidate = 14
future_rename_blocker = 39
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `pattern_position_monitor.py`.
- Determine whether each reference is passive reporting, stop/exit
  decision-support, or a live action path.
- If passive and covered by tests, add a small safe helper/adapter conversion.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live stop, target, close, broker, reconcile, risk/capital/PDT, or
  portfolio behavior change without parity evidence.
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
