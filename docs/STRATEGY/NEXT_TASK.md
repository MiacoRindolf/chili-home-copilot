# NEXT_TASK: f-phase5ac-backtest-service-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/backtest_service.py`, the next low-risk
learning/research/reporting ORM-symbol candidate, and decide whether it can be
converted directly to management-envelope helpers or first needs a read-only
parity probe.

## Why This Is Next

Phase 5AB-D removed `trading_scheduler.py` from the Phase 5O compatibility
map. The remaining `orm_trade_symbol_compat` surface is now 70 files, with 16
in learning/research/reporting. `backtest_service.py` is the first listed
research/reporting candidate and should be safer than broker, risk, close,
router, or UI contracts.

## Scope

- Classify every `Trade` ORM reference in `backtest_service.py`.
- If references are passive historical/reporting reads, add a narrow
  management-envelope helper and convert with tests.
- If references affect simulation semantics or rely on ORM identity behavior,
  close as an audit and queue a parity probe.
- Keep public payload/key names unchanged if they contain `trade` vocabulary.

## Guardrails

- No live broker/order/close/reconcile changes.
- No stop evaluation or dispatch changes.
- No scheduler cadence changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No strategy result changes unless old-vs-new parity proves them first.

## Exit Criteria

- Either a small read-only conversion ships with focused tests, or the task
  closes with a documented deferral and next parity-probe brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
