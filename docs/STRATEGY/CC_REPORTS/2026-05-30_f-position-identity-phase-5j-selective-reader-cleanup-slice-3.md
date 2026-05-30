# f-position-identity-phase-5j-selective-reader-cleanup-slice-3

## Summary

Phase 5J slice 3 converted learning/reporting readers from the legacy
compatibility view name `trading_trades` to the semantic physical table
`trading_management_envelopes`.

Converted files:

- `app/services/trading/dynamic_priors.py`
  - population win-rate prior
  - population average-return prior
- `app/services/trading/ticker_scope_autotune.py`
  - per-pattern/per-ticker realized stats reader
- `app/services/trading/pattern_stats_recompute.py`
  - scan-pattern aggregate maintenance source reads
  - table-existence guard updated to the physical envelope table

No live writer, broker reconciliation, order placement, stop execution, or ORM
`Trade` class path changed.

## Verification

Commands run:

```powershell
python -m py_compile app\services\trading\dynamic_priors.py app\services\trading\ticker_scope_autotune.py app\services\trading\pattern_stats_recompute.py tests\test_phase5j_reader_cleanup.py
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 4 passed
- Phase 5I probe test: passed
- Phase 5I live probe: `COMPLETE_POSITIVE`
- Scheduled wrapper: `COMPLETE_POSITIVE`, `LOG_SCHEMA_ERRORS=0`
- App-code search: no `FROM trading_trades` or `JOIN trading_trades` remains
  in the three converted services.
- Live smoke:
  - dynamic prior win-rate query returned `0.3433962264150943`
  - dynamic prior average-return query returned `-0.025259278575476878`
  - ticker-autotune reader executed successfully
  - SQLAlchemy physical table introspection sees `trading_management_envelopes`
    and does not see `trading_trades` as a physical table

## Architect Read

This slice was slightly more than pure dashboard reading because
`pattern_stats_recompute.py` writes aggregate stats into `scan_patterns`, but
its source-of-truth read is still a learning/reporting aggregate, not live
execution. Moving it to the renamed physical table is correct after Phase 5H.

Next slice can continue with realized-stat / net-edge reporting readers while
still deferring live broker/order-management paths.
