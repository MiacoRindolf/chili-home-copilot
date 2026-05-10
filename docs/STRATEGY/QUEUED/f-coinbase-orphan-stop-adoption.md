# f-coinbase-orphan-stop-adoption

## Background

After f-coinbase-post-place-verify-routing-fix (commits `21ce9ee` →
`7def71b` → `c8a3ff3`) deployed at 2026-05-10 15:25 PT, the
verify-routing bug is sealed: Robinhood-404 hits dropped from 8+ to 0,
unverified count from 8+ to 0. But the deployment surfaced a separate
problem.

**Production log evidence (broker-sync-worker, 2026-05-10 22:25 UTC):**

```
[bracket_writer_g2] place_missing_stop broker error intent=255:
  Insufficient balance in source account
[bracket_writer_g2] place_missing_stop broker error intent=256:
  Insufficient balance in source account
```

These trades (ACX, RARE, 1INCH, AERGO) have stops sitting at Coinbase
from earlier sweeps that returned order IDs (`d1b91a9c`, `b13e8058`,
`545eeffe`, `b3c14ef6`) but were marked 'unverified' due to the old
Robinhood-routing bug. Those orders are now reserving qty at the
venue, so new placements hit "Insufficient balance".

The DB has no record of those order IDs because the prior code
short-circuited at the 'unverified' state without persisting them.

## Real-money state

- AERGO, 1INCH, ACX, RARE: **protected at the venue by live orphan
  stops, but DB-naked** (broker_stop_order_id NULL).
- FIDA, COTI, ACH, ALEPH: no orphan stops; should get fresh stops on
  next sweep but may hit the same loop if any sub-position is
  reserved by another order.
- ACS-USD #1842: separate qty-divergence issue (DB=1.5M, broker=0.27).

## Goal

Build a one-shot adoption pass that:

1. Calls Coinbase API to list OPEN orders for the configured account.
2. Filters to `side=SELL`, `type=STOP_LIMIT` (or whatever Phase 4
   uses for the stop primitive).
3. For each open Coinbase stop order, matches by ticker (and ideally
   quantity) to an existing `trading_bracket_intents` row where
   `broker_source='coinbase'`, `broker_stop_order_id IS NULL`,
   `intent_state IN ('intent', ...whatever pre-confirmed states
   exist...)`.
4. On match: UPDATE `broker_stop_order_id`, transition intent_state
   to the appropriate confirmed/reconciled state per the state
   machine, write a `bracket_intent_writer` log line for audit.
5. Skip and log if ambiguous (multiple intents per ticker, multiple
   open orders per ticker, qty mismatch).

## Where it runs

Two options for CC to weigh:

**A: One-shot script** in `scripts/dispatch-coinbase-orphan-adopt.ps1`
that the operator runs once. Pro: simple, auditable, no recurring
risk. Con: needs operator to fire it.

**B: Integrate into reconciler** as a "first-time-adopt-orphans" pass
on top of `_stage_backfill_missing_intents`. Pro: runs automatically
on next 60s sweep, no operator action. Con: adds permanent
overhead to every sweep + risk of false adoptions if matching logic
is sloppy.

Plan should pick one and justify. My lean: **A** because it's a
one-shot historical cleanup, not a recurring need (the verify-routing
fix prevents new orphans).

## Scope

- New file: `app/services/trading/venue/coinbase_orphan_adopt.py`
  with the adoption pass logic (or merged into a logical neighbor).
- New file: `scripts/dispatch-coinbase-orphan-adopt.ps1` if going
  with option A.
- New tests in `tests/test_coinbase_orphan_adopt.py`.
- Edge cases: paper trade with `broker_source=None`, ticker match
  but qty mismatch (skip with warn log), no Coinbase orphan order
  for an intent (no-op, leave intent_state alone), Coinbase API
  unreachable (raise, don't silently swallow).

## Hard constraints

- Coinbase venue adapter + new adoption module + new test file.
  Do NOT modify reconciler / writer / stop_engine / autotrader /
  Robinhood adapter (the adoption logic CAN live in a new file
  imported by reconciler IF option B is chosen, but the existing
  reconciler code should not be touched in ways that change current
  behavior).
- Edit-tool truncation discipline.
- Phase 6 LIVE soak active — purely additive.
- No magic-fallback values for qty/ticker matching. If ambiguous,
  log + skip, do NOT guess.
- Plan-gate protocol active.

## Verification

After adoption pass runs:

```sql
SELECT t.id, t.ticker, bi.intent_state, bi.broker_stop_order_id
  FROM trading_trades t
  JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status='open' AND t.broker_source='coinbase'
 ORDER BY t.id;
```

Expected: at least 4 rows show non-NULL `broker_stop_order_id`
(AERGO, 1INCH, ACX, RARE) with intent_state in the confirmed/
reconciled states. The remaining 5 (FIDA, COTI, ACH, ALEPH, ACS)
may still be NAKED but can be retried by the reconciler now that
the verify-routing fix is live.
