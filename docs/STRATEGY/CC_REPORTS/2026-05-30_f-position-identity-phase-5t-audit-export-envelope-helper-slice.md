# Phase 5T - Audit Export Envelope Helper Slice

Date: 2026-05-30
Status: SHIPPED

## What changed

- Added `load_audit_export_envelope_rows(...)` to `management_envelopes.py`.
- Converted only `api_audit_export(...)` to source the `trades` export rows from `trading_management_envelopes`.
- Preserved the public audit contract: JSON key `trades`, CSV label `# TRADES`, and trade field order/names.
- Added JSON and CSV parity tests for the audit export surface.

## Verification

- `python -m py_compile app\routers\trading_sub\trades.py app\services\trading\management_envelopes.py`
- `python -m pytest tests\test_audit_export_envelope_helper.py tests\test_management_envelopes.py -q` -> 8 passed
- `python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q` -> 9 passed
- `python scripts\analyze_phase5_remaining_trade_refs.py --json --include app --fail-on-unexpected-runtime` -> ok=true
- `python scripts\d-phase5k-live-path-parity-probe.py` -> COMPLETE_POSITIVE
- `python scripts\d-phase5i-post-rename-soak-probe.py` -> COMPLETE_POSITIVE

## Architect verdict

This is a read-only cleanup with low trading blast radius. It removes one more private audit reader from the legacy trade ORM path without renaming any public trade/export language. Full public rename remains premature; next move is a parity/contract gate for the user-facing `/trades` API surface.