# NEXT_TASK: f-phase5ag-auto-trader-position-overrides-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/auto_trader_position_overrides.py`, the next
adapter-candidate after Phase 5AF reclassified `auto_trader_monitor.py` as a
live exit-monitor path.

## Why This Is Next

The last three candidates were not safe direct conversions:

- `alerts.py` owns live alert/order/proposal behavior.
- `alpha_decay.py` can demote promoted alpha.
- `auto_trader_monitor.py` can submit live exits.

`auto_trader_position_overrides.py` is classified as `private_helper_type_only`
and may be a smaller semantic-helper candidate, but it still needs an audit
before edits because overrides may feed live controls.

Current surface:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 13
live_action_broker_reconcile = 17
adapter_candidate = 17
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `auto_trader_position_overrides.py`.
- Separate type-only/private-helper references from live control behavior.
- If references are type-only or passive, clean/convert with focused tests.
- If references affect pause/resume/override controls, close as an audit and
  queue a read-only parity probe first.

## Guardrails

- No live control behavior change without parity evidence.
- No broker/order/close/reconcile behavior change.
- No lifecycle, risk/capital/PDT/portfolio gate change.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.

## Exit Criteria

- Either a small safe cleanup/conversion ships with focused tests, or the task
  closes with a documented deferral and next parity-probe brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
