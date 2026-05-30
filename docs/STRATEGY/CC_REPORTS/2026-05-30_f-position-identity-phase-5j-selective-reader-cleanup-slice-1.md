# f-position-identity-phase-5j-selective-reader-cleanup-slice-1

## Summary

Phase 5J started with a narrow reader-only slice.

Converted semantic readers from the legacy compatibility view name
`trading_trades` to the physical envelope table name
`trading_management_envelopes`.

Converted files:

- `app/routers/brain.py`
  - brain health KPI profitability summary
  - signal-family diversity summary
  - external/manual no-pattern book summary
- `app/services/trading/management_envelopes.py`
  - management-envelope health helper
- `scripts/d-cb-phase6-soak-probe.py`
  - Coinbase Phase 6 soak read-only probe
- `scripts/d-maker-only-tca-probe.py`
  - maker-only TCA read-only probe
- `scripts/d-imminent-silence-audit.py`
  - imminent-alert silence read-only audit

No live writer, broker reconciliation, order placement, stop execution, or ORM
`Trade` class path was changed.

## Verification

Commands run:

```powershell
python -m py_compile app\routers\brain.py app\services\trading\management_envelopes.py scripts\d-cb-phase6-soak-probe.py scripts\d-maker-only-tca-probe.py scripts\d-imminent-silence-audit.py tests\test_phase5j_reader_cleanup.py
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 2 passed
- Phase 5I probe test: passed
- Phase 5I live probe: `COMPLETE_POSITIVE`
- Scheduled wrapper: `COMPLETE_POSITIVE`, `LOG_SCHEMA_ERRORS=0`
- Direct brain KPI live smoke:
  - `ok=True`
  - `profitability.trades_30d=265`
  - `manual_book.trades_30d=14`

## Architect Read

This is the right shape for Phase 5J: small reader slices, each followed by the
Phase 5I soak gate. The compatibility view stays in place, so old writers and
the SQLAlchemy `Trade` model remain boring.

Next slice should continue with read-only analytics services, especially
`decision_packet_coverage.py`, `divergence_service.py`, and read-only reporting
scripts. Continue to defer anything in broker/order management.
