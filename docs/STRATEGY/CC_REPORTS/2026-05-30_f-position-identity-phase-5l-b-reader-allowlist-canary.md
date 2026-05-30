# Phase 5L-B: Reader Allowlist Canary

**Date:** 2026-05-30
**Status:** SHIPPED
**Commits:** `84fc229`, merge `3ce746c`

## Summary

Added a focused test canary that prevents new raw live-reader SQL from drifting
back to the legacy `trading_trades` compatibility view.

The canary scans runtime app Python files for:

```text
FROM trading_trades
JOIN trading_trades
```

and only allows the exact currently-known compatibility-reader lines. Migrations
are skipped intentionally; they are historical compatibility contracts, not live
runtime readers.

This is not a behavior change and does not touch any live broker/order/reconcile
path.

## Why This Matters

After Phase 5H, `trading_management_envelopes` is the real table and
`trading_trades` is only a compatibility view. Phase 5K and Phase 5L-A moved the
highest-signal readers to the semantic relation. The remaining problem is drift:
future code can accidentally add another direct reader against the old name.

The canary makes that visible immediately.

## What Is Allowed Today

The allowlist is exact by file and normalized SQL line, not a broad directory
exception. It currently covers:

- autotrader trade-id semantic readers
- bracket-reconciliation trade-id semantic readers
- Coinbase orphan adoption reconcile join
- known evidence/model readers that still need contract migration

If another direct live-reader line appears, the test fails.

## Verification

```text
python -m pytest tests\test_phase5l_reader_allowlist.py tests\test_phase5_remaining_trade_refs.py -q

4 passed

python scripts\d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_MISMATCHES=0

python scripts\d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

The remaining-reference classifier from the remote commit was preserved in the
merge and ran successfully. It is intentionally broad and still reports many
`unclassified_trade_surface_reference` files because it includes `Trade` symbol
surface area and docs/scripts/history. That is useful for planning, but the
Phase 5L-B canary is the practical runtime guardrail.

## Merge Note

While this slice was being pushed, the remote branch advanced with
`2b0db55 Classify remaining trade surface references`. I preserved that work and
merged it with the local canary commit. The merge commit is `3ce746c`.

## Next

Continue Phase 5L with a second evidence/model reader slice. Recommended first
candidates are clean direct SQL readers:

- `crypto/pattern_miner.py`
- `options/portfolio_budget.py`

Dirty local candidates (`pattern_regime_ledger.py`,
`pattern_survival/features.py`, `pattern_survival/training.py`) should be read
carefully before any commit, because the worktree already has unrelated edits in
those files.
