# f-position-identity-phase-5k-d-pdt-reader-flag

## Summary

Phase 5K-D shipped the next narrow reader cutover candidate: the PDT
day-trade counter.

The live behavior remains default-off unless the operator enables:

```text
CHILI_PHASE5K_PDT_USE_ENVELOPES=true
```

OFF reads the legacy compatibility view:

```text
trading_trades
```

ON reads the physical semantic table:

```text
trading_management_envelopes
```

The broker-confirmed PDT filters are unchanged: equities only, same-day
round trips only, broker order present, `last_fill_at` present, reconcile
artifact exit reasons excluded, and the true 5-business-day cutoff retained.

## Verification

Focused unit tests:

```text
python -m pytest tests\test_pdt_guard_phase5k_reader_flag.py tests\test_phase5k_live_path_parity_probe.py -q
13 passed
```

Compile check:

```text
python -m py_compile app\services\trading\pdt_guard.py scripts\d-phase5k-live-path-parity-probe.py
passed
```

Live parity probe:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 live-path aggregate checks matched
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_PDT_DAY_TRADES=OK old_rows=2 new_rows=2
```

Direct live PDT count through the production function:

```text
PDT_COMPAT_COUNT 3
PDT_ENVELOPE_COUNT 3
```

## Architect Read

This is exactly the kind of reader that should be cut over before broader
Phase 5 live-path cleanup: small blast radius, high operational importance,
and already covered by the Phase 5K parity probe.

The code is intentionally not a bulk rename. It leaves the compatibility view
as the default source and introduces a single, reversible switch for the PDT
reader. If the live flag soak is clean, the next reader can follow the same
pattern.

## Next Step

Run a narrow live flag soak:

1. Confirm Phase 5K-A and Phase 5I are green.
2. Set `CHILI_PHASE5K_PDT_USE_ENVELOPES=true`.
3. Recreate only `autotrader-worker`.
4. Verify the container sees the flag.
5. Confirm direct PDT counts still match and no new autotrader/PDT errors
   appear during a short soak.
