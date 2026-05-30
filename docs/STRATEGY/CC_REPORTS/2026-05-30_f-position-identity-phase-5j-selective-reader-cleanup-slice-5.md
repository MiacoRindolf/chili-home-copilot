# f-position-identity-phase-5j-selective-reader-cleanup-slice-5

## Summary

Phase 5J slice 5 converted another clean reader-only batch from the legacy
compatibility view name `trading_trades` to the semantic physical table
`trading_management_envelopes`.

Converted files:

- `app/routers/admin.py`
  - bracket cover-policy snapshot join
- `app/routers/trading_sub/ai.py`
  - AI health total-envelope count
- `app/services/trading/brain_work/handlers/quality_score.py`
  - realized-PnL quality-score aggregate reader
- `scripts/d-pid537-watcher.py`
  - seven-day closed PnL reader
  - pid 537 lifetime PnL reader
- `scripts/walkforward_monthly_dd_breaker.py`
  - monthly drawdown breaker walk-forward source reader

No live writer, broker reconciliation, order placement, stop execution, or ORM
`Trade` class path changed.

## Verification

Commands run:

```powershell
python -m py_compile app\routers\admin.py app\routers\trading_sub\ai.py app\services\trading\brain_work\handlers\quality_score.py scripts\d-pid537-watcher.py scripts\walkforward_monthly_dd_breaker.py tests\test_phase5j_reader_cleanup.py
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 6 passed
- Phase 5I probe test: passed
- Phase 5I live probe: `COMPLETE_POSITIVE`
- Scheduled wrapper: `COMPLETE_POSITIVE`, `LOG_SCHEMA_ERRORS=0`
- App/script search: no `FROM trading_trades` or `JOIN trading_trades` remains
  in the converted files.
- Live smoke:
  - envelope count query returned `725`
  - admin cover-policy query executed and returned `0`
  - pid 537 watcher executed and reported `COMPLETE_POSITIVE`

## Extra Finding

The pid 537 watcher now reports:

- `PID_537_N=17`
- `PID_537_WR=0.6471`
- `PID_537_PAYOFF=13.0411`
- `PID_537_STAGE=promoted`

The original watcher objective is satisfied because pid 537 has reached the
positive evidence gate and is already promoted. Follow-up should close or
retarget that watcher separately from Phase 5J.

## Architect Read

This was the last obviously clean reader-only batch. The remaining references
are mostly one of:

- live execution / broker / stop / reconciliation paths
- historical migrations and compatibility contracts
- tests that intentionally exercise the compatibility view
- files with unrelated local edits (`net_edge_ranker.py`,
  `scripts/analyze_trade_quality_funnel.py`)

Continue only with carefully inspected single-file slices.
