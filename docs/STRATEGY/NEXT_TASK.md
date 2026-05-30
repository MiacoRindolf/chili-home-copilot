# NEXT_TASK: f-position-identity-phase-5k-f-portfolio-risk-reader-flag

STATUS: PENDING

## Goal

Cut over the portfolio-risk open-exposure reader with the same single-reader
default-off pattern.

This reader already matched in the Phase 5K parity probe:

```text
CHECK_PORTFOLIO_RISK_OPEN=OK old_rows=2 new_rows=2
```

## Current State

- Coinbase cap reader is live on `trading_management_envelopes`.
- PDT reader is live on `trading_management_envelopes`.
- Cohort-promote realized reader is live on `trading_management_envelopes`.
- Pattern-quality realized reader is live on `trading_management_envelopes`.
- Phase 5K-A parity remains `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remains `COMPLETE_POSITIVE`.

## Implementation Shape

1. Locate portfolio-risk open-exposure reads that still use `trading_trades`.
2. Add a default-off reader switch.
3. Preserve all filters, exposure formulas, and risk thresholds exactly.
4. Add focused tests for OFF/ON relation selection.
5. Run Phase 5K-A and a direct function-level old/new check.
6. Commit/push the default-off code.
7. Run a narrow flag soak only if parity remains green.

## Guardrails

- Do not touch broker/order/stop/reconcile write paths.
- Do not change risk formulas, caps, thresholds, or drawdown logic.
- Do not run DB-backed pytest against live Postgres.
- Stage only files intentionally touched by this brief.

## Rollback

The new flag must default false. If the live soak misbehaves, set it false and
recreate only the affected consumer worker(s).
