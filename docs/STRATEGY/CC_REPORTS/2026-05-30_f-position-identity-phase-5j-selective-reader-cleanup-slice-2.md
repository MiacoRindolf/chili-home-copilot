# f-position-identity-phase-5j-selective-reader-cleanup-slice-2

## Summary

Phase 5J slice 2 converted two read-only analytics services from the legacy
compatibility view name `trading_trades` to the semantic physical table
`trading_management_envelopes`.

Converted files:

- `app/services/trading/decision_packet_coverage.py`
  - decision packet coverage totals
  - unlinked packet sample query
  - proposal/packet backfill candidate reader
- `app/services/trading/divergence_service.py`
  - venue-truth divergence joins
  - bracket reconciliation divergence joins
  - pattern candidate discovery unions

No live writer, broker reconciliation, order placement, stop execution, or ORM
`Trade` class path changed.

## Verification

Commands run:

```powershell
python -m py_compile app\services\trading\decision_packet_coverage.py app\services\trading\divergence_service.py tests\test_phase5j_reader_cleanup.py
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 3 passed
- Phase 5I probe test: passed
- Phase 5I live probe: `COMPLETE_POSITIVE`
- Scheduled wrapper: `COMPLETE_POSITIVE`, `LOG_SCHEMA_ERRORS=0`
- App-code search: no `FROM trading_trades` or `JOIN trading_trades` remains
  in the two converted services.

## Architect Read

The reader cleanup is moving in the right sequence: analytics first, live
writers last. The compatibility view remains in place, so this does not change
execution behavior.

Next slice can target more read-only analytics modules such as
`dynamic_priors.py`, `ticker_scope_autotune.py`, and reporting scripts. Keep
deferring broker/order management, migrations, and ORM class renames.
