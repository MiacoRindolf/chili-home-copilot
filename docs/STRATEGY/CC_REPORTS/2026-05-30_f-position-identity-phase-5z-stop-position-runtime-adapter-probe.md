# Phase 5Z-A: Stop-Position Runtime Adapter Probe

Date: 2026-05-30

## Summary

Added a read-only parity probe for `/api/trading/stops/positions`.

The probe compares the current `Trade` ORM runtime object path with a candidate object built from `trading_management_envelopes` rows, then feeds both through the same stop-position serialization chain:

- broker stale-open filtering
- broker-position display metrics
- option detection
- broker/market quote routing
- stop-engine brain context
- UI state computation

No endpoint behavior changed in this slice.

## Manually Authorized Read-Only Live Result

The live-data sample below is manually authorized read-only evidence, not the
default or CI-safe validation path. The probe now defaults `DATABASE_URL` to
`TEST_DATABASE_URL` or local `chili_test`, rejects non-test database URLs unless
`PHASE5Z_ALLOW_LIVE_PROBE=true` is set, and keeps broker/market quote reads
disabled unless that same explicit opt-in is present.

With the explicit live-probe opt-in, `scripts/d-phase5z-stop-position-runtime-adapter-probe.py` returned:

- `VERDICT_STATUS=COMPLETE_POSITIVE`
- `matched=true`
- old positions: 5
- new positions: 5
- old suppressed stale rows: 0
- new suppressed stale rows: 0
- quote cache entries: 10
- `trading_management_envelopes='r'`
- `trading_trades='v'`

This means the candidate runtime-envelope object can preserve the stop-position payload on current live data.

## Verification

- `python -m py_compile scripts/d-phase5z-stop-position-runtime-adapter-probe.py`
- `pytest tests/test_phase5z_stop_position_runtime_adapter_probe.py tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`
  - Result: 16 passed, 1 existing SQLAlchemy warning
- Manually authorized read-only live probe:
  - `COMPLETE_POSITIVE`

## Architect Verdict

Phase 5Z-A proves the adapter approach. The stop-position endpoint is now eligible for a narrow conversion in Phase 5Z-B:

- load open rows from `trading_management_envelopes`
- expose a read-only Trade-like runtime object
- keep the public payload untouched
- keep stop execution/evaluation/dispatch untouched

This is the right data-science gate: we now have evidence before touching the risk display.
