# NEXT_TASK: f-phase5z-learning-reporting-adapter-slice-11

STATUS: QUEUED

## Goal

Convert one more passive learning/reporting `Trade` ORM reader to a semantic
management-envelope helper, or close it as intentionally deferred after audit.

## Current State

Phase 5Y converted `regime_classifier.build_regime_scanner_sharpe_heatmap(...)`
to read closed live rows from `trading_management_envelopes`.

Remaining compatibility surface:

```text
orm_trade_symbol_compat     | 72
adapter_candidate           | 23
learning_research_reporting | 18
future_rename_blocker       | 33
leave_alone                 | 16
```

## Candidate Guidance

Inspect in this order:

1. `app/services/trading/scanner.py`
2. `app/services/trading/market_data.py`
3. `app/services/trading_scheduler.py`

Only convert a reader if it is passive and row-shape parity is obvious or
testable. Close the task as an audit if the surface feeds live broker/order,
risk, capital, lifecycle, or broad learning behavior.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle promotion/demotion changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Avoid broad `learning.py`, `alpha_decay.py`, lifecycle decay, and stale-sweep
  surfaces until separately scoped.
