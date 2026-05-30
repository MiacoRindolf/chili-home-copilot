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

## Live Soak Result

Phase 5K-D was promoted after the default-off source commit.

The live flag is now enabled in `.env` and visible inside `autotrader-worker`:

```text
CHILI_PHASE5K_PDT_USE_ENVELOPES=true
```

Post-flip validation:

```text
Phase 5K-A parity probe: COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_PDT_DAY_TRADES=OK old_rows=2 new_rows=2

Phase 5I post-rename probe: COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0

Direct PDT function:
PDT_COMPAT_COUNT 3
PDT_ENVELOPE_COUNT 3
```

Fresh autotrader logs showed normal ticks and monitor cycles. No PDT query
errors, relation errors, or new rollback signals appeared. Observed warnings
were pre-existing provider/auth or tick-budget noise, not PDT-reader defects.

## Next Step

Continue the single-reader cutover sequence:

1. promotion/pattern-quality realized aggregate readers
2. portfolio-risk open-exposure reader
