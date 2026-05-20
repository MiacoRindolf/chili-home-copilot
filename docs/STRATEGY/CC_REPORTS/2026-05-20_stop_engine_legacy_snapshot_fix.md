# Stop Engine Legacy Snapshot Fix - 2026-05-20

## Summary

The only remaining `FALLBACK_FIRED` CRITICAL stop-engine log came from legacy CRDL trade `1814`. Its `indicator_snapshot` was double-encoded JSON, so the stop engine could not read any ATR from it.

## Fix

- Added `_indicator_snapshot_dict(...)` to unwrap both normal and double-encoded snapshot rows.
- Added `_extract_atr_from_indicator_snapshot(...)` to centralize ATR extraction from:
  - top-level `atr`
  - `atr_14.value`
  - `breakout_alert.flat_indicators.atr`
  - `flat_indicators.atr`
- Reused the helper in both bracket-intent emission and stop evaluation fallback.
- Added tests for current nested snapshots and double-encoded legacy snapshots.

## Live Repair

CRDL trade `1814` was seeded with current ATR `0.0825` from `get_indicator_snapshot('CRDL', interval='1d')`.

## Verification

- `pytest tests/test_stop_engine_indicator_snapshot.py -q` -> `2 passed`
- `python -m py_compile app/services/trading/stop_engine.py` -> pass
- `broker-sync-worker` recreated
- Follow-up stop-engine cycle:
  - evaluated 8 open trades
  - zero `FALLBACK_FIRED` logs in the post-repair window
  - bracket reconciliation remained clean with `missing_stop=0`

