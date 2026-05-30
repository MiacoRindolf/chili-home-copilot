# NEXT_TASK: f-position-identity-phase-5k-h-alpha-portfolio-gate-reader-flag

STATUS: PENDING

## Goal

Audit and, if clean, add a default-off envelope reader flag for the remaining
`alpha_portfolio_gate.py` live capital reader that still has a direct
`trading_trades` SQL reference.

## Current State

The six Phase 5K-A parity groups are now live or promoted:

- Coinbase cap
- PDT
- Cohort-promote realized
- Pattern-quality realized
- Portfolio-risk drawdown
- Position-integrity

Phase 5K-A remains `COMPLETE_POSITIVE`, and Phase 5I remains
`COMPLETE_POSITIVE`.

## Implementation Shape

1. Read `app/services/trading/alpha_portfolio_gate.py` around the remaining
   `FROM trading_trades` query.
2. Decide whether the reader is live capital gating, reporting-only, or a
   compatibility contract.
3. If it is a safe reader cutover:
   - add `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=false`
   - preserve every filter/formula exactly
   - add focused OFF/ON relation tests
   - run direct old/new function-level checks
   - commit/push default-off code
   - flip/soak only if parity is exact
4. If it is not a safe cutover, write a closeout note and leave the
   compatibility view in place.

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change portfolio-gate math, caps, thresholds, or rejection semantics.
- Do not absorb unrelated dirty worktree files.
- Do not remove the `trading_trades` compatibility view.

## Rollback

Any new flag must default false. Rollback is setting it false and recreating
the affected consumer worker(s).
