# Phase 5V - /trades Route Shadow Compare

Date: 2026-05-30
Status: SHIPPED

## What changed

- Added passive `/trades` shadow comparison for stable database-backed fields.
- The live route still returns the current compatibility/ORM output.
- Broker-truth display overlays remain excluded from the stable comparison; local entry/quantity are compared against the envelope base row.
- On mismatch, the route logs `[phase5v] /trades envelope shadow mismatch` with a small sample and still returns the existing response.

## Verification

- `python -m py_compile app\routers\trading_sub\trades.py`
- `python -m pytest tests\test_trades_api_shadow_compare.py tests\test_audit_export_envelope_helper.py tests\test_management_envelopes.py -q` -> 11 passed
- `python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q` -> 9 passed
- `python scripts\analyze_phase5_remaining_trade_refs.py --json --include app --fail-on-unexpected-runtime` -> ok=true
- `python scripts\d-phase5u-trades-api-parity-probe.py` -> COMPLETE_POSITIVE
- `python scripts\d-phase5k-live-path-parity-probe.py` -> COMPLETE_POSITIVE
- `python scripts\d-phase5i-post-rename-soak-probe.py` -> COMPLETE_POSITIVE

## Architect verdict

This is the right stopping point before route cutover: the endpoint now observes whether envelope rows can reproduce the stable public row shape under live endpoint execution, without changing user-visible behavior.