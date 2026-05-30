# NEXT_TASK: f-position-identity-phase-5k-g-position-integrity-flag-soak

STATUS: PENDING

## Goal

Flip and soak the default-off position-integrity reader flag shipped in Phase
5K-G.

```text
CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false  # default-off code shipped
```

## Current State

- Coinbase cap reader is live on `trading_management_envelopes`.
- PDT reader is live on `trading_management_envelopes`.
- Cohort-promote realized reader is live on `trading_management_envelopes`.
- Pattern-quality realized reader is live on `trading_management_envelopes`.
- Portfolio-risk drawdown reader is live on `trading_management_envelopes`.
- Position-integrity reader has a default-off flag.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Direct old/new position-integrity audit and dry-run repair checks match.

## Soak Shape

1. Set `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=true` in `.env`.
2. Recreate consumer workers that call position-integrity helpers:
   - `chili`
   - `broker-sync-worker`
   - `autotrader-worker` only if runtime imports require it
3. Verify runtime flag visibility.
4. Re-run:
   - Phase 5K-A parity probe
   - Phase 5I post-rename soak probe
   - direct old/new position-integrity audit + dry-run repair comparison
5. Scan logs for position-integrity / relation / query errors.
6. If clean, promote Phase 5K-G and choose the next live-path reader.

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change integrity verdict semantics.
- Do not run DB-backed pytest against live Postgres.
- Do not absorb unrelated dirty worktree files.

## Rollback

Set `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false` and recreate the
same consumer worker(s).
