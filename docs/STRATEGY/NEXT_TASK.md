# NEXT_TASK: f-position-identity-phase-5k-i-live-path-closeout-audit

STATUS: PENDING

## Goal

Close Phase 5K with a fresh remaining-reference audit now that every safe
single-reader parity group has been flipped and soaked.

## Current State

Live on `trading_management_envelopes`:

- Coinbase cap
- PDT
- Cohort-promote realized
- Pattern-quality realized
- Portfolio-risk drawdown
- Position-integrity
- Alpha-portfolio gate realized aggregate

Both safety probes remain green:

```text
Phase 5K-A: COMPLETE_POSITIVE
Phase 5I: COMPLETE_POSITIVE
```

## Work Shape

1. Re-run a focused remaining-reference scan for:
   - direct `FROM trading_trades` / `JOIN trading_trades`
   - `Trade` ORM live reader/writer surfaces
   - compatibility-contract migrations/tests/scripts
2. Classify remaining references into:
   - compatibility contract
   - migration/test/history
   - live writer/order/broker/reconcile path
   - live reader still eligible for a flag
   - dirty local worktree candidate
3. Run Phase 5K-A and Phase 5I once more.
4. If no safe reader remains, write a Phase 5K closeout report and set the next
   phase to compatibility-contract hardening instead of more blind cutovers.

## Guardrails

- Do not remove the `trading_trades` compatibility view.
- Do not cut over broker/order/reconcile writer paths by search-replace.
- Do not absorb unrelated dirty worktree files.
- Treat `Trade` ORM surfaces as semantic code that needs separate design, not
  a mechanical table-name replacement.
