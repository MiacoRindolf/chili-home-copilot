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

## Live Soak Result

Phase 5K-E was promoted after the default-off source commit.

The live flags are now enabled in `.env` and visible inside both `chili` and
`autotrader-worker`:

```text
CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=true
CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=true
```

Post-flip validation:

```text
Phase 5K-A parity probe: COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_PROMOTION_REALIZED=OK old_rows=30 new_rows=30
CHECK_PATTERN_QUALITY=OK old_rows=30 new_rows=30

Phase 5I post-rename probe: COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0

Direct pattern-quality function:
PATTERN_QUALITY_OLD_ROWS 30
PATTERN_QUALITY_NEW_ROWS 30
PATTERN_QUALITY_MATCH True

Direct cohort-promote function:
COHORT_PROMOTE_OLD_COUNT 9
COHORT_PROMOTE_NEW_COUNT 9
COHORT_PROMOTE_MATCH True
```

Fresh logs from the restarted consumer workers showed no relation, query, or
Phase 5K reader errors.

## Next Step

Ship the portfolio-risk open-exposure reader under its own default-off flag and
repeat the same proof/soak cycle.
