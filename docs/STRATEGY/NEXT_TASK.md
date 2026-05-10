# NEXT_TASK: f-coinbase-tick-size-precision-fix

STATUS: DONE

Completed 2026-05-10. See
`docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-tick-size-precision-fix.md`.

Commits: e5a6deb (cache + helpers) → 4501169 (quantize wire-up) →
5f6576a (18 new tests). 26/26 tests pass. The 9 currently-naked
Coinbase trades will be backstopped on the next reconciler sweep
(~5 min after deploy).

## Goal

The bracket-coverage fix (commit 77d9a5e) sealed Bugs A/B/C — every
open Coinbase trade now has an intent row, the reconciler routes to
Coinbase, and the writer calls `place_stop_limit_order_gtc`. But
Coinbase REST is rejecting all 9 stop placements:

```
ALEPH-USD: "Too many decimals in order price"
8 others: UNKNOWN_FAILURE_REASON
```

The Coinbase venue adapter doesn't quantize prices to product-level
`quote_increment` before submitting. Real-money exposure ≈ $2,700
remains.

## Brief (full)

`docs/STRATEGY/QUEUED/f-coinbase-tick-size-precision-fix.md`.

## Phases

Single-shot fix.

## Deliverables

- Product-info cache (tick_size, base_increment, quote_increment,
  min_market_funds) with TTL in `coinbase_spot.py`
- Price + size quantization before `place_stop_limit_order_gtc`
- Tests in `tests/test_coinbase_tick_size_precision.py` (new)
- CC_REPORT
- Updated NEXT_TASK to STATUS: DONE

## Hard constraints

- Coinbase venue adapter only. No bracket / reconciler / writer / stop_engine changes.
- Edit-tool truncation discipline (Write for files >500 lines).
- Coinbase Phase 6 LIVE soak active — don't weaken existing gates.
- No magic-fallback values for missing product info — raise, don't guess.
- Plan-gate protocol active.
