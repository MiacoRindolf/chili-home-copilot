# f-coinbase-autotrader-enablement-phase-4-bracket-writer-path

**Owner**: Cowork → Claude Code
**Status**: PENDING
**Risk**: MEDIUM-HIGH (touches bracket coverage; if broken, Coinbase positions go naked)
**Time budget**: 3-4h CC scope (single session, design + implementation + tests + paper-test)

## Goal

Add the **Coinbase stop primitive** to `coinbase_spot.py` adapter
and wire it through `bracket_writer_g2.py` so Coinbase entries get
the same stop-loss coverage RH entries get. Without this, flipping
`CHILI_COINBASE_AUTOTRADER_LIVE=1` would leave any Coinbase
position naked on the downside — the same class of failure as the
R-31/R-32 wipeout cascade we closed for crypto reconciler.

## Why now

Phase 3 (broker selector) shipped 2026-05-09 (commits `bcf9ea0` +
`9c02e37`). It routes RH-unsupported crypto tickers to Coinbase but
the broker call is **gated behind `CHILI_COINBASE_AUTOTRADER_LIVE`**
(default OFF) precisely because no bracket writer Coinbase path
exists yet. Phase 4 is the hard prerequisite before LIVE flips.

Phase 1 audit (`docs/STRATEGY/CC_REPORTS/2026-05-09_f-coinbase-autotrader-enablement-phase-1-audit.md`)
established that:

- `venue/factory.py` exists with `get_adapter(venue_name)` factory.
- `coinbase_spot.py` adapter has `place_market_order` and
  `place_limit_order` but **NO stop primitive**.
- Risk infra (kill switch / drawdown breaker / pdt guard) is
  venue-blind, so Phase 4 doesn't need to expand any of those.
- Phase 2 (auth) and Phase 3 (selector) both proved end-to-end —
  Coinbase Advanced Trade SDK round-trips work.

## Operator-locked design constraints (binding from Phase 1)

These continue to apply (not new in Phase 4 — re-stated for
inertia):

1. Cross-venue position cap: SEPARATE per-venue caps.
2. Kill switch: GLOBAL.
3. Selector preference for both-listed: RH-first.
4. Fast-path overlap: skip-on-fast-path-active.

## The change (4 components)

### Component A — Stop primitive in `coinbase_spot.py`

Add method:
```python
def place_stop_limit_order_gtc(
    self,
    *,
    product_id: str,                # e.g., 'SUI-USD'
    side: str,                       # 'sell' for protective stop
    base_size: str,                  # quantity to sell
    limit_price: str,                # marketable limit (e.g., 1% below stop)
    stop_price: str,                 # trigger
    stop_direction: str = 'STOP_DIRECTION_STOP_DOWN',
    client_order_id: str | None = None,
) -> dict:
    """
    Places a stop-limit GTC order via Coinbase Advanced Trade
    using OrderConfiguration.stop_limit_stop_limit_gtc.

    Returns {ok, order_id, raw} matching the existing
    place_market_order response shape so callers can route
    polymorphically.
    """
```

Notes:
- Coinbase Advanced Trade uses `stop_direction` enum:
  `STOP_DIRECTION_STOP_DOWN` for protective stops on long
  positions.
- `limit_price` should be set marketable (e.g., 1% below
  `stop_price` for a SELL stop on a long) so the stop converts
  to a limit immediately on trigger and likely fills.
- Cancel via existing `cancel_order_by_id` (Phase 2 verified).

### Component B — `bracket_writer_g2.py` Coinbase splice

`bracket_writer_g2.py` currently writes RH stops via
`broker_service.place_sell_stop_loss_order`. Add a venue-routed
splice:

```python
venue = trade.venue or _infer_venue_from_ticker(trade.ticker)
if venue == 'coinbase':
    res = coinbase_adapter.place_stop_limit_order_gtc(
        product_id=trade.ticker,
        side='sell',
        base_size=str(trade.quantity),
        stop_price=str(trade.stop_price),
        limit_price=str(round(float(trade.stop_price) * 0.99, 2)),
        client_order_id=client_order_id,
    )
elif venue == 'rh':
    # existing path -- byte-identical
    res = broker_service.place_sell_stop_loss_order(...)
```

Same RH-byte-identical guarantee as Phase 3: existing RH stop
placement unchanged.

The `trade.venue` field may not exist; fall back to
`_infer_venue_from_ticker` heuristic (USD-quoted crypto bases
not on RH whitelist → coinbase; everything else → rh).
Ideally the autotrader splice from Phase 3 already records the
venue on the Trade row at entry time — verify in CC scan and
add if missing (small write).

### Component C — Missing-stop repair sweep parity

The Phase G.2 missing-stop repair sweep (round 23) lives in
`scheduler-worker` and currently only repairs RH positions.
Extend to:

1. Detect Coinbase open positions without resting stops.
2. Repair via the new `place_stop_limit_order_gtc` adapter
   method.
3. Same cooldown + reject-handling as the RH path (1h
   per-intent cooldown after broker-rejection; 5min
   post-placement cooldown).

Reuse the existing `bracket_writer_g2.py` repair logic; just
make the broker call branch on venue.

### Component D — Test parity + paper-test

1. **Unit test**: `tests/test_coinbase_stop_primitive.py` — mock
   the Coinbase SDK; verify `place_stop_limit_order_gtc` builds
   the correct `OrderConfiguration` payload.
2. **Unit test**: `tests/test_bracket_writer_venue_routing.py`
   — verify `bracket_writer_g2` routes by venue correctly.
3. **Paper-test (CC, single live)**: place a stop-limit on
   `SUI-USD` (or any small-position long-tail crypto operator
   already holds) at 50% below current price; immediately
   cancel; confirm zero residual via `get_recent_orders` filter
   for `stop_limit_stop_limit_gtc` orders. **Operator approval
   required** — same pattern as Phase 2 redux paper-test.

## Acceptance criteria (10-item list)

1. **`coinbase_spot.py` stop primitive shipped** with
   `place_stop_limit_order_gtc` method matching the response
   shape `{ok, order_id, raw}`.
2. **`bracket_writer_g2.py` venue-routed splice** with RH path
   BYTE-IDENTICAL (parity unit test as in Phase 3).
3. **Missing-stop repair sweep** extended to Coinbase positions;
   1h reject + 5min placement cooldown logic reused.
4. **Trade.venue field populated** at entry time by the Phase 3
   selector splice (small `auto_trader.py` write if missing).
5. **Unit tests** in
   `tests/test_coinbase_stop_primitive.py` +
   `tests/test_bracket_writer_venue_routing.py` cover the new
   paths.
6. **Paper-test (CC)** places + cancels a stop-limit on
   Coinbase via the adapter directly. Operator approval
   required for placement; cancel runs in `try/finally` with
   10s hard timeout. Zero residual stops post-cancel.
7. **Multi-process verification**: `docker exec` into all 4
   workers and confirm `coinbase_adapter.place_stop_limit_order_gtc`
   importable + callable.
8. **No regressions on RH stop path**: full
   `test_bracket_writer_g2.py` test suite passes; idle-in-tx
   probe (per FIX 46 cookbook) shows no new connection leaks.
9. **Cost log preserved**: stop-limit placements log to
   `bracket_intent` table with venue=`coinbase`.
10. **CC report at**:
    `docs/STRATEGY/CC_REPORTS/<YYYY-MM-DD>_f-coinbase-autotrader-enablement-phase-4-bracket-writer-path.md`.

## Brain integration (read + write)

**Read-only:**
- `app/services/trading/bracket_writer_g2.py` — find RH stop
  placement callsite + capture call signature for parity test.
- `app/services/coinbase_service.py` — confirm
  `place_buy_order` / `cancel_order_by_id` shape (verified in
  Phase 2 redux).
- `app/services/trading/venue/coinbase_spot.py` — current
  adapter; identify where to add the stop primitive.
- `app/services/trading/venue/factory.py` — `get_adapter` already
  used by Phase 3 broker_selector splice.

**Write:**
- `app/services/trading/venue/coinbase_spot.py` — add
  `place_stop_limit_order_gtc` method.
- `app/services/trading/bracket_writer_g2.py` — venue-routed
  splice; preserve RH path verbatim.
- `app/services/trading/auto_trader.py` — populate
  `trade.venue` at entry time if not already (small write).
- `tests/test_coinbase_stop_primitive.py` — NEW file.
- `tests/test_bracket_writer_venue_routing.py` — NEW file.
- (optional) `app/migrations.py` — if `Trade.venue` column
  needs to be added or extended; check existing schema first.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **RH stop path BYTE-IDENTICAL.** Verified by parity unit test.
- **No flipping of `CHILI_COINBASE_AUTOTRADER_LIVE=1`** during
  Phase 4. The stop primitive must be unit-tested + paper-tested
  via the adapter directly; LIVE flip stays operator-controlled.
- **No autotrader entry-side changes** beyond `Trade.venue`
  population at entry time. Phase 4 is bracket-side only.
- **No new bracket strategies.** Phase 4 ports the existing
  G2 writer's stop-limit logic to Coinbase; it does NOT
  introduce trailing stops, OCO, or any new bracket variants.
- **No paper-soak.** Phase 6's job. Phase 4's "live test" is a
  single tiny stop-limit-far-below-price + cancel, same pattern
  as Phase 2 redux.
- **Edit-tool truncation discipline (HARD).** `bracket_writer_g2.py`
  + `coinbase_spot.py` may exceed 2000 lines. After every edit:
  `wc -l` + `git diff --stat`; verify no silent truncation.

## Out of scope (Phase 4 — later phases)

- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6).
- Live verification + capital ramp (Phase 7).
- Coinbase market-on-stop (currently only stop-limit GTC is
  supported by the adapter).
- Trailing stops on Coinbase.
- Coinbase OCO (One-Cancels-Other) brackets — current adapter
  ports the existing single-stop-per-trade pattern.
- Coinbase WebSocket stop-fill notifications.

## Sequencing

1. Truncation scan on `bracket_writer_g2.py`,
   `coinbase_spot.py`, `coinbase_service.py`, `auto_trader.py`.
2. Read `bracket_writer_g2.py` to find RH stop callsite +
   capture call signature for parity test.
3. Read `coinbase_spot.py` current adapter to identify the
   right insertion point for `place_stop_limit_order_gtc`.
4. Write `place_stop_limit_order_gtc` method in
   `coinbase_spot.py`.
5. Write `tests/test_coinbase_stop_primitive.py`.
6. Splice venue routing into `bracket_writer_g2.py` (RH path
   byte-identical; Coinbase path NEW).
7. Write `tests/test_bracket_writer_venue_routing.py`.
8. Extend missing-stop repair sweep for Coinbase positions.
9. Populate `Trade.venue` at entry time in `auto_trader.py` if
   missing.
10. Run full pytest — RH parity gate held.
11. Force-recreate workers; verify multi-process import +
    callability.
12. **Single live paper-test** (operator approval required):
    place stop-limit-far-below-price + cancel.
13. CC report.
14. Commit + push.
15. **Operator decides whether to flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`**
    after reading CC report. If yes, Phase 5 (cost-aware sizing)
    can be queued in parallel; if no, Phase 5 first.

## Operator-side after Phase 4 ships

1. Read CC report.
2. Watch missing-stop-repair-sweep logs for any Coinbase
   repair attempts in shadow-mode (selector still emits
   `selector:coinbase_routing_shadow_log` because LIVE=0;
   should be no Coinbase positions to repair UNLESS the operator
   has manually opened positions).
3. **Decide**: flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`? If yes:
   - Force-recreate workers.
   - Watch first 24h carefully — any Coinbase entry triggers a
     bracket placement immediately; verify via
     `bracket_intent` table queries.
   - Promote Phase 5 (cost-aware sizing) to harden cost-edge
     gating.

## Rollback plan

- **Stop primitive misbehaves** (e.g., stops not resting at
  broker): `git revert` the `bracket_writer_g2.py` venue splice.
  Adapter method + tests stay (no invocation). RH path unaffected.
- **Coinbase stop placements rejected by broker**: emergency
  toggle `CHILI_COINBASE_AUTOTRADER_LIVE=0` to stop new entries;
  manual-cancel any open Coinbase stop orders via `cancel_order_by_id`
  helper or Coinbase UI.
- **Adapter import error breaks worker startup**: revert the
  `coinbase_spot.py` change; `docker compose up -d
  --force-recreate`.

## What CC should do if it's unsure

1. **Coinbase Advanced Trade SDK doesn't expose
   `OrderConfiguration.stop_limit_stop_limit_gtc`**: surface
   the SDK version installed; check Coinbase API docs for the
   correct path. Do NOT guess — the wrong field shape will be
   rejected at place_order time and we'd lose the live test.
2. **`bracket_writer_g2.py` callsite ambiguous** (multiple
   stop placements): pick the ONE used by autotrader-driven
   trades; surface the others in CC report; do NOT touch them.
3. **`Trade.venue` column missing entirely**: add a migration
   `_migration_NNN_trade_venue_column()` (idempotent;
   `ALTER TABLE trades ADD COLUMN IF NOT EXISTS venue VARCHAR(16)`).
   Default is `'rh'` for backfill.
4. **Paper-test stop placement raises**: STOP. Surface for
   operator. Cancel any partially placed stop via direct
   `cancel_order_by_id`; document the order_id.
5. **RH stop placement parity test fails**: STOP. RH stop path
   is byte-identical or nothing ships.
