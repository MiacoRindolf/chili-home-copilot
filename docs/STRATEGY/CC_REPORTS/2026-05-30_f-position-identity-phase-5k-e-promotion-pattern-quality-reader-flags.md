# f-position-identity-phase-5k-e-promotion-pattern-quality-reader-flags

## Summary

Phase 5K-E shipped default-off reader flags for the next two realized-outcome
readers:

```text
CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=false
CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=false
```

When enabled:

- cohort-promotion realized eligibility reads `trading_management_envelopes`
  instead of the `trading_trades` compatibility view.
- pattern-quality realized PnL scoring reads `trading_management_envelopes`
  instead of the `trading_trades` compatibility view.

No formulas, filters, thresholds, lifecycle logic, or write paths changed.

## Verification

Focused tests:

```text
python -m pytest tests\test_phase5k_promotion_pattern_quality_reader_flags.py tests\test_phase5k_live_path_parity_probe.py -q
14 passed
```

Compile check:

```text
python -m py_compile app\services\trading\pattern_quality_score.py app\services\trading\pattern_cohort_promote.py scripts\d-phase5k-live-path-parity-probe.py
passed
```

Phase 5K-A live-path parity:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_PROMOTION_REALIZED=OK old_rows=30 new_rows=30
CHECK_PATTERN_QUALITY=OK old_rows=30 new_rows=30
```

Direct function-level checks:

```text
PATTERN_QUALITY_OLD_ROWS 30
PATTERN_QUALITY_NEW_ROWS 30
PATTERN_QUALITY_MATCH True

COHORT_PROMOTE_OLD_COUNT 9
COHORT_PROMOTE_NEW_COUNT 9
COHORT_PROMOTE_MATCH True
```

## Architect Read

This is the right next reader pair: these two paths influence which patterns
get observation and capital, but the cutover itself is pure read-source
selection. The old/new evidence matches exactly, so the next action is a short
live flag soak with both flags enabled together.

Do not roll this into portfolio-risk yet. Portfolio open-exposure is a separate
risk surface and should get its own flag, probe, and rollback lever.

## Next Step

Run the narrow live soak:

```text
CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=true
CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=true
```

Then recreate only the worker(s) that consume these readers and re-run the
same parity/function checks.
