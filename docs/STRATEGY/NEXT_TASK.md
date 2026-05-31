# NEXT_TASK: f-phase5o-autopilot-scope-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/autopilot_scope.py`, the next Phase 5O adapter
candidate after `auto_trader_synergy.py` was reclassified as a live scale-in
capital gate.

## Why This Is Next

Recent candidates have repeatedly turned out to be live behavior surfaces, not
simple passive readers:

- `alerts.py` owns live alert/order/proposal behavior.
- `alpha_decay.py` can demote promoted alpha.
- `auto_trader_monitor.py` can submit live exits.
- `auto_trader_position_overrides.py` can submit close-now exits and mutate
  adoption/unadoption scope.
- `auto_trader_synergy.py` can return scale-in capital plans.

`autopilot_scope.py` is currently classified as
`private_helper_type_only / adapter_candidate`, but its helpers are used by
live monitor, close, option/crypto partitioning, and public risk surfaces.
Before any helper conversion, prove whether it is a harmless private type
adapter or a behavior-bearing classification gate.

Current surface after Phase 5O synergy audit:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 12
live_action_broker_reconcile = 18
private_helper_type_only = 6
risk_capital_gate = 19
adapter_candidate = 15
future_rename_blocker = 38
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in `autopilot_scope.py`.
- Identify which helpers are pure type/shape checks and which influence live
  option/crypto/equity routing or close/monitor behavior.
- If passive and covered by tests, add a small safe helper/adapter conversion.
- If behavior-bearing, add read-only envelope parity evidence and reclassify
  it as a future rename blocker.

## Guardrails

- No live entry, scale-in, close, broker, reconcile, risk/capital/PDT, or
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
