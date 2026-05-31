# NEXT_TASK: f-phase5aa-b-market-data-anchor-reader-conversion

STATUS: QUEUED

## Goal

Convert `market_data._resolve_implausibility_anchor(...)`'s database fallback
from the `trading_trades` compatibility view / `Trade` ORM path to the physical
`trading_management_envelopes` semantic source.

## Evidence

Phase 5AA parity probe:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
ANCHOR_TICKERS=8
ANCHOR_MISMATCHES=0
```

The current old source and candidate new source matched exactly for the live
open ticker universe.

## Scope

- Change only the database fallback used after the in-memory known-good cache
  misses.
- Preserve cache-first behavior.
- Preserve failure-open behavior: on DB errors, return `None` and allow quote
  acceptance/seed behavior as today.
- Preserve explicit rollback/close handling to avoid idle-in-transaction leaks.

## Guardrails

- No provider routing changes.
- No quote threshold/math changes.
- No broker/order/close/reconcile changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.

## Exit Criteria

- Focused market-data tests pass.
- Phase 5AA parity probe remains `COMPLETE_POSITIVE` after the swap.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.

