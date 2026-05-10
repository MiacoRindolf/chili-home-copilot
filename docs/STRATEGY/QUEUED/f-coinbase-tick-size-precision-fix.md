# f-coinbase-tick-size-precision-fix

## Background

After f-coinbase-bracket-coverage-fix (commit 77d9a5e) shipped, the
reconciler now correctly creates intent rows for all 9 open Coinbase
trades and routes `place_missing_stop` to the Coinbase adapter via
`place_stop_limit_order_gtc`. But Coinbase REST is rejecting every
order placement.

**Production log evidence (2026-05-10 19:50:46-58 UTC, post-deploy)**:

```
intent=252 ticker=ALEPH-USD: "Too many decimals in order price"
intent=249 ticker=FIDA-USD: UNKNOWN_FAILURE_REASON
intent=250 ticker=COTI-USD: UNKNOWN_FAILURE_REASON
intent=251 ticker=ACH-USD: UNKNOWN_FAILURE_REASON
intent=253 ticker=AERGO-USD: UNKNOWN_FAILURE_REASON
intent=254 ticker=1INCH-USD: UNKNOWN_FAILURE_REASON
intent=255 ticker=ACX-USD: UNKNOWN_FAILURE_REASON
intent=256 ticker=RARE-USD: UNKNOWN_FAILURE_REASON
intent=239 ticker=ACS-USD: UNKNOWN_FAILURE_REASON (after qty-cap to 0.269169)
```

ALEPH-USD's error is the smoking gun — the adapter is sending a
`stop_price` and/or `limit_price` with more decimals than Coinbase's
product-level `quote_increment` allows. The `UNKNOWN_FAILURE_REASON`
on the other 8 is most likely the SAME class of error (Coinbase API
returns generic when its own validation fails).

Each Coinbase product has three precision constraints:
- `base_increment` — minimum lot size (e.g. 0.01 base units)
- `quote_increment` — minimum price tick (e.g. 0.0001 USD)
- `min_market_funds` — minimum order notional

The adapter currently sends `stop_price` and `limit_price` as-is from
the DB row's `stop_loss` field, which is whatever the brain computed
(e.g. `0.00786416` for ACH-USD with 8-decimal precision). Coinbase
rejects anything finer than the product's `quote_increment`.

**Real-money exposure**: 9 positions still NAKED at the venue —
~$2,700.

## Root cause

`app/services/trading/venue/coinbase_spot.py::place_stop_limit_order_gtc`
sends user-provided prices directly without quantizing to the product's
`quote_increment`. Same likely applies to size vs `base_increment` and
notional vs `min_market_funds`.

## Scope

Single-file fix (likely) in the Coinbase venue adapter:
- Cache product info (tick_size, base_increment, quote_increment) per
  symbol with TTL.
- Quantize stop_price and limit_price DOWN to nearest `quote_increment`
  for sells, UP for buys (or matching direction).
- Quantize size DOWN to nearest `base_increment`.
- Reject placement (with explicit log line) if quantized notional <
  `min_market_funds` rather than letting Coinbase return a generic
  rejection.

## Plan-gate protocol

CC writes `plan.request.md` covering:

(a) Files to modify with absolute paths + `wc -l`. Likely just
    `app/services/trading/venue/coinbase_spot.py` + new tests.
(b) The product-info caching strategy (in-process dict with TTL? per-
    request? piggy-back on an existing cache?).
(c) The quantization helper signature + direction semantics. Stop-loss
    sells should round price DOWN (more conservative for longs); buys
    round UP. Tests must cover both.
(d) `min_market_funds` enforcement: reject placement vs cap-to-min vs
    error-out. Recommend reject + log.
(e) Tests in `tests/test_coinbase_tick_size_precision.py` (NEW) covering
    each Coinbase product class (high-decimal, low-decimal,
    sub-penny like ACS-USD).
(f) Hot-fix path for the 9 currently-naked trades: nothing extra
    needed — once the adapter quantizes correctly, the next
    reconciler sweep will retry and place stops.
(g) Verification queries identical to the bracket-coverage-fix CC_REPORT
    (intent_state, broker_stop_order_id NOT NULL).

## Hard constraints

- Coinbase venue adapter only. Do NOT modify Robinhood adapter or any
  bracket reconciliation / writer / stop_engine code.
- For files >500 lines, use Write not Edit (truncation hazard).
- No magic-fallback values. If product info fetch fails, raise — don't
  guess at a tick_size.
- Coinbase Phase 6 LIVE soak active — do NOT disable existing safety
  gates. The fix is purely additive (price quantization before submit).
- Plan-gate protocol active.

## CC step-by-step

1. Read `CLAUDE.md`, `PROTOCOL.md`, `COWORK_ADVISOR_BRIEF.md` (§2),
   `NEXT_TASK.md`, this brief.
2. Read `app/services/trading/venue/coinbase_spot.py` end-to-end (it's
   the SDK wrapper; understand the full surface).
3. Read Coinbase REST docs for `/products/{product_id}` endpoint —
   the response shape determines our cache structure.
4. Read existing tests for the Coinbase adapter (any
   `tests/test_coinbase_*.py`) to understand fixture conventions.
5. Write `plan.request.md` covering (a)–(g) above.
6. Wait for `plan.response.md`. APPROVED → proceed.
7. Implement: product-info cache + quantize helpers + place_stop_limit
   call-site update + tests.
8. WIP commits per logical chunk.
9. Run pytest. Final commit. Push.
10. Write CC_REPORT. Update NEXT_TASK to STATUS: DONE.
