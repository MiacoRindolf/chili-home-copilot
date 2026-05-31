# Phase 5T - Execution Cost Envelope Adapter

Date: 2026-05-31

## Summary

Phase 5T converted the execution-cost estimator's closed-row reads from the
legacy `Trade` ORM surface to semantic management-envelope helpers.

The estimator's math and persistence contract are unchanged:

- TCA slippage parsing still uses entry/exit bps.
- Spread proxy still uses absolute entry slippage.
- ADV fallback still uses entry notional divided by window days.
- `trading_execution_cost_estimates` writes are unchanged.

## Code Changes

- Added `load_execution_cost_estimate_envelope_rows(...)`.
- Added `load_closed_management_envelope_tickers_since(...)`.
- Updated `compute_rolling_estimate(...)`, `_compute_rolling_estimates_for_ticker(...)`,
  and ticker auto-discovery in `rebuild_all(...)` to use those helpers.
- Added direct helper tests proving the SQL reads `trading_management_envelopes`
  and not `trading_trades`.

## Inventory Impact

```text
orm_trade_symbol_compat     81 -> 80
learning_research_reporting 27 -> 26
adapter_candidate           32 -> 31
future_rename_blocker       33 -> 33
leave_alone                 16 -> 16
```

## Validation

- `python -m py_compile app/services/trading/management_envelopes.py app/services/trading/execution_cost_builder.py tests/test_execution_cost_builder.py`
- Focused pure/unit execution-cost tests passed.
- Phase 5 analyzer reported `orm_trade_symbol_compat=80`,
  `learning_research_reporting=26`, and no unexpected runtime readers/mutations.

Full DB-backed `tests/test_execution_cost_builder.py` was not run to completion:
the shared `chili_test` fixture setup repeatedly spent several minutes in
Postgres file sync during its global truncate. Postgres was not restarted.

## Guardrails

- No broker/order/close/reconcile changes.
- No pattern lifecycle behavior changes.
- No capital/risk/PDT/portfolio gate logic changes.
- No schema migration.
- Cost-estimate persistence shape unchanged.

## Architect Verdict

Good narrow slice. This removes one actual learning/reporting dependency while
leaving the cost model itself untouched. The next slice should continue with
another read-only closed-envelope diagnostic, but avoid live trading gates unless
there is a dedicated live-path parity probe.
