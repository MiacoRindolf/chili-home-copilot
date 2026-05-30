# NEXT_TASK: f-position-identity-phase-5k-g-position-integrity-reader-flag

STATUS: PENDING

## Goal

Cut over the position-integrity open-position reader with the same single-reader
default-off pattern used in Phase 5K-C through Phase 5K-F.

This reader already matched in the Phase 5K parity probe:

```text
CHECK_POSITION_INTEGRITY_OPEN=OK old_rows=5 new_rows=5
```

## Current State

- Coinbase cap reader is live on `trading_management_envelopes`.
- PDT reader is live on `trading_management_envelopes`.
- Cohort-promote realized reader is live on `trading_management_envelopes`.
- Pattern-quality realized reader is live on `trading_management_envelopes`.
- Portfolio-risk drawdown reader is live on `trading_management_envelopes`.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remains `COMPLETE_POSITIVE`.

## Implementation Shape

1. Locate `position_integrity.py` reads that still use `trading_trades`.
2. Add a default-off reader switch:
   `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false`.
3. Preserve every filter, join, state predicate, and position-integrity verdict
   exactly.
4. Add focused tests for OFF/ON relation selection.
5. Run Phase 5K-A and a direct old/new function-level check.
6. Commit/push the default-off code.
7. If green, flip and soak the flag in the consumer worker(s).

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change integrity verdict semantics.
- Do not run DB-backed pytest against live Postgres.
- Do not absorb unrelated dirty worktree files.

## Rollback

The flag must default false. If the live soak misbehaves, set
`CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false` and recreate the affected
consumer worker(s).
