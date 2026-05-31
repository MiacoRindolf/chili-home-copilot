# Phase 5AK - Trades API Flag Posture

Date: 2026-05-31

## Summary

Phase 5AK promotes `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES` from an
experimental default-off flag to the default `/api/trading/trades` route
posture.

The flag remains available as an operator rollback switch: explicitly setting
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=false` sends the route back through
the legacy compatibility path.

## Why now

The route cutover has accumulated the required evidence:

```text
Phase 5AE: base /trades row shape parity COMPLETE_POSITIVE
Phase 5AG: open-row runtime adapter parity COMPLETE_POSITIVE
Phase 5AH: all/open/closed cutover probe COMPLETE_POSITIVE
Phase 5AI: live route trial healthy with the flag on
Phase 5AJ: tie-order caveat removed; all/open/closed exact_match=true
Phase 5K: live-path aggregate parity COMPLETE_POSITIVE
Phase 5I: post-rename soak COMPLETE_POSITIVE
```

Live HTTPS route checks against the Phase 5AJ runtime were healthy for both
guest and user-1 requests:

```text
guest /api/trading/trades               ok=True rows=50 suppressed=0
guest /api/trading/trades?status=open   ok=True rows=0  suppressed=0
guest /api/trading/trades?status=closed ok=True rows=50 suppressed=0
user1 /api/trading/trades               ok=True rows=50 suppressed=0
user1 /api/trading/trades?status=open   ok=True rows=5  suppressed=0
user1 /api/trading/trades?status=closed ok=True rows=50 suppressed=0
```

## Changes

- `Settings.chili_phase5af_trades_api_use_envelopes` now defaults to `true`.
- Tests now assert default-on behavior and explicit-env rollback to `false`.
- No schema, broker, order, close, reconcile, PDT, cash, capital, portfolio, or
  promotion behavior changed.

## Architect verdict

This is the right permanent posture. Keeping the flag default-off after exact
runtime parity would make production depend on a special `.env` override and
increase rollback-by-accident risk. Default-on with explicit false rollback is
the cleaner operational contract.

The remaining source-of-truth caveat still matters: the live root is dirty, so
web runtime should continue to be recreated from a clean merged worktree until
the root is reconciled.
