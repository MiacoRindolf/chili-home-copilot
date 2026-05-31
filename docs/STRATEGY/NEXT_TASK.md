# NEXT_TASK: f-phase5o-pattern-imminent-alerts-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/pattern_imminent_alerts.py`, the next Phase 5O
adapter candidate after `pattern_position_monitor.py` was reclassified as a
live monitor/action-adjacent path.

## Why This Is Next

The remaining adapter candidates are increasingly concentrated in learning and
alert-generation surfaces. `pattern_imminent_alerts.py` is a good next slice
because it can influence which alerts become AutoTrader candidates. That makes
it data-science relevant and potentially live-selection relevant, even if it
does not place orders directly.

Current surface after the pattern-position monitor audit:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 11
live_action_broker_reconcile = 19
private_helper_type_only = 5
risk_capital_gate = 20
adapter_candidate = 13
future_rename_blocker = 40
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `pattern_imminent_alerts.py`.
- Determine whether the references are passive research/reporting, alert
  generation, candidate selection, or live-gate-adjacent.
- If passive and covered by tests, add a small safe helper/adapter conversion.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live alert cadence, order, stop, close, broker, reconcile,
  risk/capital/PDT, or portfolio behavior change without parity evidence.
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
