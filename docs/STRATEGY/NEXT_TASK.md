# NEXT_TASK: f-position-identity-phase-5k-d-pdt-reader-flag-soak

STATUS: PENDING

## Goal

Run the narrow live soak for the PDT day-trade reader flag.

Phase 5K-D source is already default-off safe:

```text
CHILI_PHASE5K_PDT_USE_ENVELOPES=false
```

When enabled, only the PDT day-trade count source changes:

```text
trading_trades compatibility view
  -> trading_management_envelopes physical base table
```

The PDT filters themselves do not change.

## Pre-Soak Evidence

- Phase 5K-A live-path parity: `COMPLETE_POSITIVE`.
- PDT parity group: `CHECK_PDT_DAY_TRADES=OK`.
- Direct production function check:

  ```text
  PDT_COMPAT_COUNT 3
  PDT_ENVELOPE_COUNT 3
  ```

- Focused tests:

  ```text
  python -m pytest tests\test_pdt_guard_phase5k_reader_flag.py tests\test_phase5k_live_path_parity_probe.py -q
  13 passed
  ```

## Soak Steps

1. Confirm Postgres is healthy and autotrader is running.
2. Confirm Phase 5K-A and Phase 5I probes are still green.
3. Set:

   ```text
   CHILI_PHASE5K_PDT_USE_ENVELOPES=true
   ```

4. Recreate only `autotrader-worker`.
5. Verify the autotrader container sees the flag.
6. Re-run:

   ```powershell
   python scripts\d-phase5k-live-path-parity-probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   ```

7. Compare direct PDT counts through both relations again.
8. Watch fresh autotrader logs for PDT query errors or unexpected entry blocks.

## Rollback

Set:

```text
CHILI_PHASE5K_PDT_USE_ENVELOPES=false
```

Then recreate only `autotrader-worker`.

## Acceptance

- Autotrader sees `CHILI_PHASE5K_PDT_USE_ENVELOPES=true`.
- Phase 5K-A remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.
- Direct PDT counts match through compatibility view and envelope table.
- No fresh PDT query errors in autotrader logs.
