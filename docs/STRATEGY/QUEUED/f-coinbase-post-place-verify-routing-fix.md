# f-coinbase-post-place-verify-routing-fix

## Background

After f-coinbase-tick-size-precision-fix (commits e5a6deb + 4501169 +
5f6576a, deployed 2026-05-10 21:07 UTC), Coinbase REST is now ACCEPTING
the stop orders — order IDs are returned successfully. But the writer's
post-place verification step is calling Robinhood's API to verify
Coinbase orders, getting 404, marking the intent 'unverified', and
never persisting `broker_stop_order_id` in the DB.

**Production log evidence** (broker-sync-worker, 2026-05-10 21:08-21:11):

```
404 Client Error: Not Found for url:
  https://api.robinhood.com/orders/b13e8058-177b-4b74-a1e8-66436ce08d49/

[bracket_writer_g2] place_missing_stop UNVERIFIED intent=253
  ticker=AERGO-USD order=b3c14ef6 last_observed_state=None
  — verify window expired without the order leaving 'unconfirmed'.
  Treating conservatively: arming post-place cooldown, NOT transitioning
  state. Next sweep will re-check broker truth.
```

The `b13e8058-...` UUID was assigned by Coinbase — `api.robinhood.com`
has never seen it.

## Bug

The post-place verification step in `bracket_writer_g2.py` (function
that handles UNVERIFIED branch around `place_missing_stop`) is
hardcoded to call Robinhood's order-status API regardless of
`broker_source`. Same pattern of bug as Bug C in
f-coinbase-bracket-coverage-fix — venue routing exists for `place_*`
but not for `verify_*`.

## Scope

Single-file fix likely:
- `app/services/trading/bracket_writer_g2.py` — find the post-place
  verify path; route to Coinbase's `get_order_status` (or equivalent)
  for `broker_source='coinbase'`, Robinhood for `'robinhood'`.
- May need to add a `get_order_status` primitive on the Coinbase
  adapter if missing (`coinbase_spot.py`).
- Tests in `tests/test_coinbase_post_place_verify.py` (new) covering:
  Coinbase order-id verify hits Coinbase API not Robinhood; writer
  transitions intent_state correctly when Coinbase returns 'open' for
  the order; writer correctly marks 'unverified' when Coinbase API is
  down (not 404 from wrong host).

## Real-money urgency

9 Coinbase positions remain DB-NAKED. But the actual venue side may have
4-9 stops sitting there from each sweep's placement attempts. Need to:
1. Fix verify routing so the next sweep marks them confirmed
2. Operator to check Coinbase open orders manually to see if there are
   duplicate stops piling up (cooldown should be preventing this but
   verify if any positions have multiple stops)
3. After fix lands + deploy, the next sweep should backfill
   `broker_stop_order_id` correctly

## Plan-gate protocol

Active. CC writes plan.request.md covering:
(a) Files to modify (likely just `bracket_writer_g2.py` + Coinbase
    adapter + new test file)
(b) The verify-routing change. Mirror the place-side `_SUPPORTED_VENUES`
    pattern.
(c) `get_order_status` primitive on Coinbase adapter — does it already
    exist? If not, what shape should it return? (Must mirror
    Robinhood's `get_order_status` return shape so the writer's
    state-machine logic doesn't need to fork.)
(d) What to do with the 8+ currently-stale "unverified" intents post-
    deploy: should the next sweep auto-recover them, or do we need a
    one-shot reconcile pass that re-fetches from Coinbase using the
    saved `broker_stop_order_id`?  (Important: the writer DOES log the
    Coinbase order-id when it places — but does it persist it
    anywhere before going to "unverified"? If not, those orders are
    permanently orphaned at the venue.)
(e) Tests covering Coinbase verify, Robinhood verify, and the orphan-
    detection scenario.

## Hard constraints

- Only `bracket_writer_g2.py` and Coinbase adapter (`coinbase_spot.py`).
  No reconciler / stop_engine / autotrader / Robinhood adapter changes.
- Edit-tool truncation discipline.
- Phase 6 LIVE soak active — purely additive. The fix is a routing
  branch addition, not a removal of any existing logic.
- No magic-fallback values. If Coinbase API is unreachable, raise.
- Plan-gate protocol active (interactive Cowork will adjudicate
  broker-adapter writes per established pattern).
