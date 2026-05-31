# NEXT_TASK: f-phase5af-auto-trader-monitor-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/auto_trader_monitor.py`, now the next
learning/research/reporting ORM-symbol candidate after Phase 5AE reclassified
`alpha_decay.py` as lifecycle-sensitive.

## Why This Is Next

Phase 5AD and Phase 5AE both showed that the inventory can overstate
adapter-readiness: `alerts.py` was live alert/order behavior, and
`alpha_decay.py` can demote promoted patterns. Continue the same careful
classification before touching the next candidate.

The remaining compatibility surface is now:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 14
adapter_candidate = 18
raw reader bucket = 0
```

`auto_trader_monitor.py` sounds monitor/reporting-oriented, but it may feed
operator controls or health decisions, so audit before edits.

## Scope

- Classify every legacy `Trade` ORM reference in `auto_trader_monitor.py`.
- Separate passive monitoring/reporting reads from action/control behavior.
- If references are passive and parity-obvious, add a narrow
  management-envelope helper and focused tests.
- If references affect controls, lifecycle, broker/order/close behavior, or
  runtime gates, close as an audit and queue a read-only parity probe first.

## Guardrails

- No autotrader control behavior change.
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
