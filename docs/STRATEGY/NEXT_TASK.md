# NEXT_TASK: f-phase5ad-alerts-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/alerts.py`, the next learning/research/reporting
ORM-symbol candidate, and decide whether its remaining legacy `Trade` ORM
surface is a false positive, a passive reader suitable for a management-envelope
helper conversion, or a live behavior path that needs a parity probe first.

## Why This Is Next

Phase 5AC removed `backtest_service.py` from the compatibility map as a
comment-only false positive. The remaining `orm_trade_symbol_compat` surface is
69 files, with 15 learning/research/reporting candidates and 20 adapter
candidates. `alerts.py` is the next listed research/reporting candidate, but it
is historically broad, so audit before editing.

## Scope

- Classify every `Trade` ORM or `Trade`-symbol reference in `alerts.py`.
- Separate passive research/reporting reads from live alert/monitor behavior.
- If a reference is a false-positive comment/type string, clean it and pin the
  analyzer count.
- If a reference is passive and parity-obvious, add a narrow
  management-envelope helper and convert with tests.
- If a reference affects live alert dispatch or trading behavior, close as an
  audit and queue a read-only parity probe.

## Guardrails

- No live broker/order/close/reconcile changes.
- No alert dispatch behavior changes without parity evidence.
- No stop evaluation or dispatch changes.
- No scheduler cadence changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.

## Exit Criteria

- Either a small safe cleanup/conversion ships with focused tests, or the task
  closes with a documented deferral and next parity-probe brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
