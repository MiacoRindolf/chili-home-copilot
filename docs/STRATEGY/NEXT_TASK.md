# NEXT_TASK: f-position-identity-phase-5k-f-portfolio-risk-flag-soak

STATUS: PENDING

## Goal

Flip and soak the default-off portfolio-risk drawdown reader flag shipped in
Phase 5K-F.

The implementation intentionally targets the concrete raw SQL reader surface in
`portfolio_risk.py`: drawdown/closed-PnL breaker math. The earlier brief called
this "open exposure", but open exposure is still an ORM `Trade` path and is not
a safe one-line relation switch.

```text
CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=false  # default-off code shipped
```

## Current State

- Coinbase cap reader is live on `trading_management_envelopes`.
- PDT reader is live on `trading_management_envelopes`.
- Cohort-promote realized reader is live on `trading_management_envelopes`.
- Pattern-quality realized reader is live on `trading_management_envelopes`.
- Portfolio-risk drawdown reader has a default-off flag.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remains `COMPLETE_POSITIVE`.
- Direct old/new drawdown checks match for global account and user 1.

## Soak Shape

1. Set `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=true` in `.env`.
2. Recreate the consumer workers (`autotrader-worker`, plus `chili` for API
   risk checks).
3. Verify the flag is visible in runtime env.
4. Re-run:
   - Phase 5K-A parity probe
   - Phase 5I post-rename soak probe
   - direct old/new drawdown helper comparison
5. Scan logs for portfolio-risk / drawdown / relation errors.
6. If clean, promote the soak and update CURRENT_PLAN.

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change risk formulas, caps, thresholds, or drawdown logic.
- Do not run DB-backed pytest against live Postgres.
- Do not absorb unrelated dirty worktree files.

## Rollback

Set `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=false` and recreate the same
consumer worker(s).
