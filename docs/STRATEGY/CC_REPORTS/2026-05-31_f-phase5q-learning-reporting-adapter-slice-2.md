# CC Report: Phase 5Q Learning/Reporting Adapter Slice 2

Date: 2026-05-31
Owner: Codex
Branch: `codex/phase5q-learning-reporting-adapter-slice-2`

## Summary

Converted the TCA summary reporting aggregate off the legacy `Trade` ORM query
surface and onto the semantic `trading_management_envelopes` relation through a
management-envelope helper.

This is a read-only reporting slice. It does not touch broker/order/close,
reconcile, PDT, capital, risk, pattern lifecycle, public `/trades` naming, or
schema.

## What Changed

- Added `tca_summary_by_ticker_from_management_envelopes(...)` in
  `app/services/trading/management_envelopes.py`.
- Updated `app/services/trading/tca_service.py::tca_summary_by_ticker(...)` to
  delegate to that helper.
- Preserved the public TCA summary payload:
  - `overall_fills`
  - `overall_avg_entry_slippage_bps`
  - `by_ticker`
  - `exit_overall_closes`
  - `exit_overall_avg_slippage_bps`
  - `exit_by_ticker`
- Updated the Phase 5O runtime compatibility map and canaries:
  - `orm_trade_symbol_compat`: 92 -> 91
  - `learning_research_reporting`: 38 -> 37
  - `adapter_candidate`: 43 -> 42

## Validation

- `python -m py_compile ...` passed for touched service/test files.
- `pytest -q tests/test_management_envelopes.py tests/test_phase5o_remaining_runtime_compat_map.py tests/test_phase5_remaining_trade_refs.py`
  passed: 32 passed.
- `pytest -q tests/test_trading.py::TestTcaService::test_tca_summary_aggregates tests/test_trading.py::TestTcaAPI::test_tca_summary_endpoint -vv`
  passed: 2 passed.
- `python scripts/analyze_phase5_remaining_trade_refs.py --fail-on-unexpected-runtime`
  passed with 0 unexpected runtime readers/mutations and 91 ORM compatibility
  files.

## Architect Read

This is the right kind of Phase 5 reduction: small, read-only, and semantic. The
TCA summary is observability/reporting, not a trading decision gate, so moving
it behind `trading_management_envelopes` reduces rename debt without changing
live execution behavior.

The remaining adapter candidates should continue to be filtered manually. Some
files classified as `learning_research_reporting` are actually lifecycle
mutators or live monitors and should not be touched in these small slices.

## Next

Proceed with another small read-only learning/reporting adapter slice. Continue
to avoid live monitor, broker/reconcile, risk/capital, and pattern lifecycle
mutation code paths unless a dedicated parity probe proves the path first.
