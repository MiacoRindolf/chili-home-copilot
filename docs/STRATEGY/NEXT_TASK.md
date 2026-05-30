# NEXT_TASK: f-position-identity-phase-5k-e-promotion-pattern-quality-flag-soak

STATUS: PENDING

## Goal

Run the narrow live soak for the promotion realized and pattern-quality realized
reader flags.

Phase 5K-E source is already default-off safe:

```text
CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=false
CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=false
```

## Pre-Soak Evidence

- Phase 5K-A live-path parity: `COMPLETE_POSITIVE`.
- Promotion realized parity: `CHECK_PROMOTION_REALIZED=OK`.
- Pattern-quality parity: `CHECK_PATTERN_QUALITY=OK`.
- Direct production function checks:

  ```text
  PATTERN_QUALITY_OLD_ROWS 30
  PATTERN_QUALITY_NEW_ROWS 30
  PATTERN_QUALITY_MATCH True

  COHORT_PROMOTE_OLD_COUNT 9
  COHORT_PROMOTE_NEW_COUNT 9
  COHORT_PROMOTE_MATCH True
  ```

- Focused tests:

  ```text
  python -m pytest tests\test_phase5k_promotion_pattern_quality_reader_flags.py tests\test_phase5k_live_path_parity_probe.py -q
  14 passed
  ```

## Soak Steps

1. Confirm Postgres is healthy and workers are running.
2. Confirm Phase 5K-A and Phase 5I probes are green.
3. Set:

   ```text
   CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=true
   CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=true
   ```

4. Recreate the consumer worker(s), starting with `autotrader-worker`.
5. Verify the flags are visible inside the container.
6. Re-run Phase 5K-A, Phase 5I, and direct function-level checks.
7. Watch fresh logs for query/relation errors.

## Rollback

Set both flags false and recreate the affected worker(s).

## Acceptance

- Both flags visible inside the relevant worker(s).
- Phase 5K-A remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.
- Direct pattern-quality realized maps still match.
- Direct cohort-promote candidate IDs still match.
- No fresh query/relation errors.
