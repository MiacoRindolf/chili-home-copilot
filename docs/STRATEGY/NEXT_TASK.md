# NEXT_TASK: f-phase5y-regime-classifier-envelope-parity

STATUS: QUEUED

## Goal

Audit `app/services/trading/regime_classifier.py` and, if the reader is truly
passive, convert its closed-trade performance reader to a management-envelope
helper with a focused parity test.

## Current State

Phase 5X converted `setup_vitals.monitored_tickers_for_vitals(...)` to use
open tickers from `trading_management_envelopes`.

Remaining compatibility surface:

```text
orm_trade_symbol_compat     | 73
adapter_candidate           | 24
learning_research_reporting | 19
future_rename_blocker       | 33
leave_alone                 | 16
```

## Recommended Work Shape

1. Inspect `regime_classifier.build_regime_scanner_sharpe_heatmap(...)`.
2. Confirm the direct `Trade` query is reporting/analysis-only and does not gate
   live orders, risk, capital, or lifecycle transitions.
3. Add a narrow helper in `management_envelopes.py` that preserves the exact row
   shape needed by the heatmap.
4. Add parity/source tests before swapping the reader.
5. Keep the change read-only; no schema migration.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle promotion/demotion changes.
- No capital/risk/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Avoid broad `learning.py`, `alpha_decay.py`, and lifecycle sweep surfaces.
