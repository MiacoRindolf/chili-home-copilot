# NEXT_TASK: f-phase5o-auto-trader-synergy-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/auto_trader_synergy.py`, the next Phase 5O
adapter-candidate after `auto_trader_position_overrides.py` was reclassified
as a live control path.

## Why This Is Next

The last four candidates were not safe direct conversions:

- `alerts.py` owns live alert/order/proposal behavior.
- `alpha_decay.py` can demote promoted alpha.
- `auto_trader_monitor.py` can submit live exits.
- `auto_trader_position_overrides.py` can submit close-now exits and mutate
  adoption/unadoption scope.

`auto_trader_synergy.py` is still classified as
`learning_research_reporting / adapter_candidate`, but it reads per-position
overrides and may affect scale-in/synergy behavior. It needs a contract audit
before any conversion.

Current surface:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 13
live_action_broker_reconcile = 18
private_helper_type_only = 6
adapter_candidate = 16
future_rename_blocker = 37
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in `auto_trader_synergy.py`.
- Determine whether it is truly read-only learning/reporting or whether it
  affects live entry/scale-in gates.
- If passive, add/ship a focused envelope parity probe or safe helper
  conversion.
- If it affects live placement/scale-in behavior, close as an audit,
  reclassify as a future rename blocker, and queue the parity evidence needed
  before conversion.

## Guardrails

- No live entry, scale-in, close, broker, reconcile, risk/capital/PDT, or
  portfolio behavior change without parity evidence.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; if PM/control-plane governance is
  frozen, push a branch/PR for evidence but do not force a merge.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
