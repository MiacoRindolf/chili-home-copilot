# NEXT_TASK: f-position-identity-phase-5ak-trades-api-flag-posture

STATUS: QUEUED

## Goal

Decide and implement the durable runtime posture for
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES` after Phase 5AJ made `/trades`
cutover parity exact.

## Current State

Phase 5AJ evidence:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
  all exact_match=true
  open exact_match=true
  closed exact_match=true

Phase 5AG: COMPLETE_POSITIVE
Phase 5AE: COMPLETE_POSITIVE
Phase 5K:  COMPLETE_POSITIVE
Phase 5I:  COMPLETE_POSITIVE
```

The route flag is healthy under live data, but the current web runtime posture
is still special:

```text
CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true
chili web container is mounted from a clean Phase 5AH/5AJ worktree
live root D:\dev\chili-home-copilot remains dirty and behind
```

## Recommended Work Shape

1. Confirm the merged Phase 5AJ branch is the code mounted by the live `chili`
   web container.
2. Re-run live authenticated `/api/trading/trades` requests for all/open/closed
   with the flag on.
3. Re-run Phase 5AH, Phase 5AG, Phase 5AE, Phase 5K, and Phase 5I probes.
4. Choose one posture:
   - keep the flag on and document it as the current permanent route posture,
     or
   - turn it off and document why more soak is needed.
5. Resolve or explicitly document the source-of-truth risk: recreating `chili`
   from the dirty live root would roll back the route code.

## Guardrails

- Do not pull into, reset, or overwrite the dirty live root.
- Do not restart Postgres.
- Do not change broker/order/close/reconcile behavior.
- Do not public-rename `Trade`, `/trades`, `trade_id`, schema fields, or UI
  labels.
- Keep any runtime container recreate to `chili` only unless a separate health
  check proves a worker needs attention.
