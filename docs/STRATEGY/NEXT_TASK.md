# NEXT_TASK: f-phase5ae-alpha-decay-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/alpha_decay.py`, now the next
learning/research/reporting ORM-symbol candidate after Phase 5AD reclassified
`alerts.py` as a live alert/order surface.

## Why This Is Next

Phase 5AD showed that candidate files must be classified before conversion:
`alerts.py` looked like a reporting candidate in the inventory, but it actually
owns live fallback monitoring, proposal execution, envelope creation, execution
event writing, and sector-gate behavior.

The remaining compatibility surface is now:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 14
adapter_candidate = 19
raw reader bucket = 0
```

`alpha_decay.py` is the next listed learning/research/reporting candidate and
should be smaller than `alerts.py`, but it still needs an audit before edits.

## Scope

- Classify every legacy `Trade` ORM reference in `alpha_decay.py`.
- Separate passive alpha/reporting reads from lifecycle, promotion, or decay
  behavior.
- If references are passive and parity-obvious, add a narrow
  management-envelope helper and focused tests.
- If references affect promotion/demotion/lifecycle behavior, close as an
  audit and queue a read-only parity probe before conversion.

## Guardrails

- No promotion/demotion behavior change.
- No lifecycle, pattern-stage, or capital gate change.
- No broker/order/close/reconcile behavior change.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.

## Exit Criteria

- Either a small safe cleanup/conversion ships with focused tests, or the task
  closes with a documented deferral and next parity-probe brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
