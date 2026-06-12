# ORDER LEDGER — broker-truth-first state inversion

**Status: DESIGN (2026-06-11). Implementation deliberately NOT rushed — this is
heart surgery and the SpaceX session comes first. Target: design review with the
operator, then a phased build next week.**

## The problem (why four patches in one day weren't enough)

2026-06-11 produced four live incidents from ONE root cause:

| Incident | Patch shipped |
|---|---|
| CPSH/SNDG fills raced ack-timeout cancels → generic brackets | #611 45s patience → #614 event-driven lifecycle |
| KMRK: dead session's GTC buy filled hours later into a −21.9% dump | #613 day-TIF + session-death order sweep |
| AAOG: adopted long written a stop ABOVE entry, dumped in 51s | #613 stop-geometry clamp |
| INDP: cancel silently failed, order open-with-fills, 612sh unmanaged 17min; then 8 phantom flatten retries | #616 adopt-fills-not-states + #620 RH broker-zero reconcile |

Each patch is correct. The CLASS persists because of the architecture: **the
session's in-memory/JSON state (`le` = `risk_snapshot_json["momentum_live_execution"]`)
is the primary record of orders and positions, and broker truth is consulted
only at checkpoints** (ack polls, sweeps, broker_sync every 2min). Every gap
between checkpoints is a window where reality and our record diverge — and
markets put money in every gap.

## The inversion

> **The broker is the ledger. CHILI keeps a continuously-reconciled projection
> of it, and SESSIONS BECOME FOLLOWERS of that projection — never owners of
> their own order/position truth.**

Concretely: a single `venue_order_ledger` table + one sync loop own every fact
about orders and positions. The momentum lane (and autotrader, and any future
lane) read the ledger and attach METADATA (which session owns which order, what
the intent was); they never write order state.

## Schema

```sql
CREATE TABLE venue_order_ledger (
    id BIGSERIAL PRIMARY KEY,
    venue VARCHAR(24) NOT NULL,              -- robinhood | coinbase | alpaca
    order_id VARCHAR(64) NOT NULL,           -- broker order id (authoritative key)
    client_order_id VARCHAR(120),
    symbol VARCHAR(24) NOT NULL,
    side VARCHAR(8) NOT NULL,
    order_type VARCHAR(16),                  -- limit | market | stop
    time_in_force VARCHAR(8),
    limit_price NUMERIC,
    stop_price NUMERIC,
    quantity NUMERIC NOT NULL,
    -- BROKER TRUTH (only the sync loop writes these):
    venue_state VARCHAR(24) NOT NULL,        -- verbatim broker state
    filled_quantity NUMERIC NOT NULL DEFAULT 0,
    average_fill_price NUMERIC,
    venue_updated_at TIMESTAMP,              -- broker's own timestamp when available
    last_synced_at TIMESTAMP NOT NULL,
    -- LIFECYCLE RESOLUTION (sync loop derives; monotonic):
    resolution VARCHAR(24) NOT NULL DEFAULT 'live',
        -- live | filled | cancelled | rejected | expired | orphaned
    -- ATTRIBUTION (lanes write ONCE at placement; never order state):
    owner_kind VARCHAR(24),                  -- momentum_session | autotrader | bracket | operator
    owner_id BIGINT,                         -- session id / trade id
    intent_json JSONB,                       -- entry/exit/stop intent snapshot
    UNIQUE (venue, order_id)
);
CREATE INDEX ix_vol_owner ON venue_order_ledger (owner_kind, owner_id);
CREATE INDEX ix_vol_unresolved ON venue_order_ledger (venue, resolution) WHERE resolution = 'live';

CREATE TABLE venue_position_ledger (
    id BIGSERIAL PRIMARY KEY,
    venue VARCHAR(24) NOT NULL,
    symbol VARCHAR(24) NOT NULL,
    quantity NUMERIC NOT NULL,               -- broker truth, signed
    average_cost NUMERIC,
    last_synced_at TIMESTAMP NOT NULL,
    owner_kind VARCHAR(24),                  -- attribution claim (nullable = unclaimed!)
    owner_id BIGINT,
    UNIQUE (venue, symbol)
);
```

## The sync loop (one writer)

A single `ledger_sync` worker (inside the exec-critical process; see
SCHEDULER_SPLIT) with two cadences:

1. **Fast lane (2–5s)**: every ledger row with `resolution='live'` →
   `adapter.get_order` → update truth columns; derive `resolution` monotonically
   (a row never goes filled→live). Plus every symbol with an open attribution →
   position refresh.
2. **Discovery sweep (30–60s)**: `list_open_orders` + open positions from the
   broker → any order/position NOT in the ledger becomes a row with
   `owner_kind=NULL` → **orphan alarm** (event + log). Unclaimed reality is the
   #1 red flag this design exists to surface.

Writes are idempotent upserts keyed on `(venue, order_id)`. The loop is the ONLY
writer of truth columns — single-writer eliminates the race classes by
construction.

## How the lanes change

* **Placement**: lane calls `adapter.place_*` exactly as today, then inserts the
  ledger row (attribution + intent) in the same transaction as its own state
  change. From that point the lane never polls the broker for this order — it
  reads the ledger row.
* **The pending-entry handler** becomes: `SELECT resolution, filled_quantity
  FROM ledger WHERE owner...` — fills adopt the moment the sync loop sees them
  (one place), cancels confirm the moment the broker confirms them. The
  event-driven lifecycle triggers (#614) stay — they just act through the ledger
  (`request_cancel` marks intent; the sync loop confirms).
* **Exits/flattens**: qty always read from `venue_position_ledger` (the INDP
  phantom-flatten class dies structurally).
* **`le` JSON** keeps lane-local state (FSM step, watch levels, trail HWM) but
  loses ALL order/position truth fields. Migration: the fields stay readable
  for one phase (dual-read, ledger-preferred), then are dropped.

## Invariants (testable)

1. Sum of lane-attributed position quantities per symbol == broker position
   quantity (else orphan alarm within one discovery sweep).
2. No order resolution ever regresses (monotonic state machine).
3. A session may not die while it owns a `resolution='live'` order (the #613
   death sweep becomes a ledger constraint, not a best-effort hook).
4. Any fill (any state, any time) reaches an owner or an orphan alarm in
   ≤ fast-lane cadence seconds. **"Shares are ours the moment they exist."**

## Rollout phases

* **L1 — shadow**: ledger + sync loop run, lanes keep current logic; a
  comparator logs every divergence between `le` beliefs and ledger truth.
  (Expect it to light up; that's the point. Soak ≥ 3 sessions.)
* **L2 — read-inversion**: pending-entry / exit / flatten paths read the ledger
  (le as fallback + divergence log). The four 06-11 patch sites collapse into
  ledger reads.
* **L3 — write-cleanup**: drop order/position truth from `le`; death-sweep and
  raced-fill machinery delete (ledger invariants subsume them).
* Rollback at any phase = flip the read flag back to `le` (writes never stop).

## Non-goals

* Not a new execution engine — adapters/place/cancel calls are unchanged.
* Not the prediction mirror (Hard Rule 5 untouched).
* Not multi-account.

## Open questions for the operator

1. RH `get_order` rate limits vs the 2–5s fast lane on busy days (cap concurrent
   unresolved rows? batch endpoint?).
2. Should bracket_intent_writer/bracket_reconciler migrate onto the ledger in
   L2 (one truth for stops too) or stay on broker_sync until L4?
3. Coinbase fills WS channel could feed the fast lane push-style — worth wiring
   in L1 or keep poll-only first?
