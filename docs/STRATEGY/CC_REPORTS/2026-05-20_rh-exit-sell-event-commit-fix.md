# RH Exit Sell Event Commit Fix - 2026-05-20

## Summary

Phase 4 position-identity logic exposed a Robinhood exit persistence bug. AKAM trade 2061 was economically sold by the autotrader (`monitor_exit_filled`, `pattern_exit_now`, PnL `-8.49`), but the sell-side `trading_execution_events` row was only flushed, not committed. On session close the audit row vanished, so Phase 4 saw no recorded sell for the position and the trade reopened as an apparent missing-stop candidate.

## Fix

- `app/services/trading/robinhood_exit_execution.py`
  - Added `db.commit()` after both Robinhood sell-side `record_execution_event(...)` calls:
    - submit-fill path
    - pending-exit sync-fill path
  - Added defensive `db.rollback()` in each non-fatal audit-write exception block.
- `tests/test_sell_side_recording.py`
  - Added static contract tests proving the Robinhood sell writers commit after audit writes and roll back on failure.

## Live Repair

AKAM trade `2061` was repaired manually:

- trade set back to `closed`
- `exit_price=143.42`
- `pnl=-8.49`
- `exit_reason=pattern_exit_now`
- sell execution event inserted for order `6a0cb81a-3d84-4d62-a7e2-9ea3ba66d217`
- bracket intent `467` set to `reconciled/agree`

## Verification

- `pytest tests/test_sell_side_recording.py -q` -> `7 passed`
- `python -m py_compile app/services/trading/robinhood_exit_execution.py` -> pass
- `docker compose up -d --force-recreate autotrader-worker broker-sync-worker chili` -> clean
- Broker reconciliation sweep after repair:
  - `trades_scanned=9`
  - `brackets_checked=9`
  - `missing_stop=0`
  - `unreconciled=0`
- AKAM:
  - trade `2061` is `closed`
  - position `250` has one filled sell event
  - bracket intent is `reconciled/agree`

