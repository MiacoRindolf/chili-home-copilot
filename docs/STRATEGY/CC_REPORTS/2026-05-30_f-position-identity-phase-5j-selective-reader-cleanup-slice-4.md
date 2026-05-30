# f-position-identity-phase-5j-selective-reader-cleanup-slice-4

## Summary

Phase 5J slice 4 converted realized-stat and sizing readers from the legacy
compatibility view name `trading_trades` to the semantic physical table
`trading_management_envelopes`.

Converted files:

- `app/services/trading/realized_stats_sync.py`
  - live realized source CTE
  - no-closed-live-trades existence reader
- `app/services/trading/hrp_sizing.py`
  - active-position symbol reader

`app/services/trading/net_edge_ranker.py` was deliberately skipped because it
already had unrelated local edits. No live writer, broker reconciliation, order
placement, stop execution, or ORM `Trade` class path changed.

## Verification

Commands run:

```powershell
python -m py_compile app\services\trading\realized_stats_sync.py app\services\trading\hrp_sizing.py tests\test_phase5j_reader_cleanup.py
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 5 passed
- Phase 5I probe test: passed
- Phase 5I live probe: `COMPLETE_POSITIVE`
- Scheduled wrapper: `COMPLETE_POSITIVE`, `LOG_SCHEMA_ERRORS=0`
- App-code search: no `FROM trading_trades` or `JOIN trading_trades` remains
  in the two converted services.
- Live smoke:
  - HRP active-position symbol reader returned a list
  - realized stats dry-run returned `updated=40`, `skipped=0`

## Architect Read

This keeps the cleanup on the safe side of the architecture: readers and
learning aggregate sources now use the semantic physical table, while live
writers and the ORM compatibility class remain unchanged.

Next slice should either handle clean read-only scripts, or wait until unrelated
local edits in `net_edge_ranker.py` are resolved before touching that module.
