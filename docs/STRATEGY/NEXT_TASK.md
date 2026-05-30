# NEXT_TASK: f-position-identity-phase-5k-e-promotion-pattern-quality-reader-flags

STATUS: PENDING

## Goal

Continue Phase 5K with the next narrow live readers: promotion realized
statistics and pattern-quality realized aggregates.

These readers already matched in the Phase 5K parity probe:

```text
CHECK_PROMOTION_REALIZED=OK old_rows=30 new_rows=30
CHECK_PATTERN_QUALITY=OK old_rows=30 new_rows=30
```

Do not bulk-cut all remaining references. Add default-off reader switches for
the specific realized-aggregate functions only, run focused tests, then do the
same short live soak pattern used for Coinbase cap and PDT.

## Current State

- Phase 5K-C Coinbase cap reader is live on
  `trading_management_envelopes`.
- Phase 5K-D PDT reader is live on `trading_management_envelopes`.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remains `COMPLETE_POSITIVE`.

## Implementation Shape

1. Locate promotion and pattern-quality live readers still using
   `trading_trades`.
2. Add default-off source-selection flags.
3. Preserve filters and formulas exactly.
4. Add focused tests pinning OFF/ON relation selection.
5. Run Phase 5K-A parity and direct function-level old/new checks.
6. Commit/push the default-off code.
7. Only then run a narrow flag soak.

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change formulas, lookback windows, or eligibility filters.
- Do not change lifecycle promotion/demotion thresholds.
- Do not run DB-backed pytest against live Postgres.
- Stage only the files intentionally touched by this brief.

## Rollback

Each reader flag must default to false. If a live soak misbehaves, set the new
flag(s) false and recreate only the consuming worker.
