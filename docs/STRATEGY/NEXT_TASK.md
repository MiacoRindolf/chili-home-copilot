# NEXT_TASK: f-position-identity-phase-5k-h-alpha-portfolio-gate-flag-soak

STATUS: PENDING

## Goal

Flip and soak the default-off alpha-portfolio gate realized-reader flag shipped
in Phase 5K-H.

```text
CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=false  # default-off code shipped
```

## Current State

- Coinbase cap reader is live on `trading_management_envelopes`.
- PDT reader is live on `trading_management_envelopes`.
- Cohort-promote realized reader is live on `trading_management_envelopes`.
- Pattern-quality realized reader is live on `trading_management_envelopes`.
- Portfolio-risk drawdown reader is live on `trading_management_envelopes`.
- Position-integrity reader is live on `trading_management_envelopes`.
- Alpha-portfolio gate realized reader has a default-off flag.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Direct old/new alpha-portfolio gate rows match (`446/446`).

## Soak Shape

1. Set `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=true` in `.env`.
2. Recreate consumer workers that run alpha-portfolio maintenance/scans:
   - `chili`
   - `scheduler-worker`
   - `brain-work-dispatcher` if it invokes maintenance
3. Verify runtime flag visibility.
4. Re-run:
   - direct old/new `_load_pattern_rows` comparison
   - Phase 5K-A parity probe
   - Phase 5I post-rename soak probe
5. Scan logs for alpha-portfolio / relation / query errors.
6. If clean, promote Phase 5K-H.

## Guardrails

- Do not change alpha score math, recert rules, or lifecycle staging behavior.
- Do not touch broker/order/stop/reconcile write paths.
- Do not absorb unrelated dirty worktree files.

## Rollback

Set `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=false` and recreate the
same consumer worker(s).
