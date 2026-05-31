# CC Report: f-position-identity-phase-5t-audit-export-envelope-helper-slice

Date: 2026-05-30
Status: SHIPPED

## Summary

Moved the audit export trade-row source behind a management-envelope helper while preserving the public export contract.

This is a read-only router cleanup. It does not rename `/trades`, `trade_id`, schema classes, UI labels, or any audit payload field names. It does not touch live broker/order/close/reconcile/PDT/capital-gate paths.

## Changes

- Added `load_audit_export_envelope_rows(...)` in `app/services/trading/management_envelopes.py`.
- Converted `app/routers/trading_sub/trades.py::api_audit_export(...)` away from direct `db.query(Trade)` for the trade section.
- Preserved the public audit export contract:
  - JSON section key remains `trades`.
  - CSV section label remains `# TRADES`.
  - CSV and JSON trade field names/order remain unchanged.
- Added `_audit_export_trade_rows(...)` as a small private formatter so the public shape is pinned separately from the database source.

## Verification

- `python -m py_compile app/services/trading/management_envelopes.py app/routers/trading_sub/trades.py`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test python -m pytest tests/test_management_envelopes.py tests/test_phase5t_audit_export_helper.py tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`
  - Result: `21 passed, 1 warning`
- `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
  - Result: `orm_trade_symbol_compat | 94`
  - Raw reader bucket remains `(none) | 0`

## Architect Notes

The focused analyzer count remains 94 because `app/routers/trading_sub/trades.py` still owns public `/trades` CRUD and live trade-management surfaces. That is expected. This slice removed one private read-only export source, not the router's public compatibility contract.

Phase 5T closes the last clearly safe router helper conversion identified by the Phase 5R audit. The next move should not be a blind rename. It should be a short Phase 5U audit that separates the remaining public/API contracts from any private monitor/reporting helper that can be safely moved.

## Next

`f-position-identity-phase-5u-router-monitor-contract-audit`

Recommended scope:

1. Inspect remaining `Trade` ORM-symbol ownership in `app/routers/trading_sub/monitor.py`, `app/schemas/trading.py`, UI templates, and public `/trades` paths.
2. Decide which surfaces are public compatibility contracts and which are private helper candidates.
3. Do not rename payload fields or schema classes in the audit slice.
