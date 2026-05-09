# NEXT_TASK: f-coinbase-autotrader-enablement-phase-4-bracket-writer-path

STATUS: DONE

## Goal

Phase 4 of the Coinbase enablement initiative. Add the **Coinbase
stop primitive** to `coinbase_spot.py` and wire it through
`bracket_writer_g2.py` so Coinbase entries get the same stop-loss
coverage RH entries get. Without this, flipping
`CHILI_COINBASE_AUTOTRADER_LIVE=1` would leave any Coinbase
position naked on the downside — same class as the R-31/R-32
wipeout cascade we closed for crypto reconciler.

The full brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement-phase-4-bracket-writer-path.md`
— **read it first.** ~3-4h CC scope. MEDIUM-HIGH risk
(bracket-writer touch; tests-pass-but-system-fails class is real).

## Why now

Phase 3 (broker selector) shipped 2026-05-09 (commits `bcf9ea0` +
`9c02e37`):

- Multi-process kill-switch pickup verified across all 4 workers
- Sample decisions correct (RH-first for both-listed; long-tail
  → Coinbase; empty-ticker skip)
- Coinbase path gated behind `CHILI_COINBASE_AUTOTRADER_LIVE`
  (default OFF = shadow-log only)

Phase 4 is the hard prerequisite before LIVE flips. Phase 1 audit
established `coinbase_spot.py` has NO stop primitive; that gap
must close first.

## The change (4 components)

1. **`coinbase_spot.py` stop primitive** — `place_stop_limit_order_gtc`
   method using Coinbase Advanced Trade
   `OrderConfiguration.stop_limit_stop_limit_gtc`. Returns
   `{ok, order_id, raw}` matching existing adapter shape.
2. **`bracket_writer_g2.py` venue splice** — RH path BYTE-IDENTICAL
   (parity unit test as in Phase 3); Coinbase path NEW.
3. **Missing-stop repair sweep parity** — extend the existing
   Phase G.2 repair sweep to Coinbase positions; reuse 1h
   reject + 5min placement cooldown logic.
4. **Trade.venue field populated** at entry time by Phase 3
   selector splice (small `auto_trader.py` write if missing).

## Operator-locked design constraints (from Phase 1, still binding)

1. Cross-venue position cap: SEPARATE per-venue.
2. Kill switch: GLOBAL.
3. Selector: RH-first for both-listed.
4. Fast-path overlap: skip-on-fast-path-active.

## Acceptance criteria (10-item list)

See full brief. Headlines:

1. `place_stop_limit_order_gtc` shipped with `{ok, order_id, raw}`
   shape.
2. RH stop path BYTE-IDENTICAL.
3. Missing-stop repair sweep extended to Coinbase.
4. `Trade.venue` populated at entry time.
5. Unit tests for stop primitive + venue routing.
6. Single live paper-test (place stop-far-below-price +
   cancel). **Operator approval required.**
7. Multi-process verification (4 workers).
8. No regressions on RH stop path; no new connection leaks.
9. `bracket_intent` table records venue=`coinbase`.
10. CC report at canonical path.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **RH stop path BYTE-IDENTICAL.** Parity unit test gates it.
- **NO flip of `CHILI_COINBASE_AUTOTRADER_LIVE=1`** during
  Phase 4. Stop primitive paper-tested via adapter directly;
  LIVE flip stays operator-controlled.
- **No autotrader entry-side changes** beyond `Trade.venue`
  population. Phase 4 is bracket-side only.
- **No new bracket strategies.** Port existing G2 stop-limit
  pattern; do NOT introduce trailing/OCO/etc.
- **Edit-tool truncation discipline (HARD).** `bracket_writer_g2.py`
  + `coinbase_spot.py` are sizable. `wc -l` + `git diff --stat`
  after every edit.

## Out of scope (Phase 4 — later phases)

- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6).
- Live verification + capital ramp (Phase 7).
- Trailing stops / OCO / market-on-stop on Coinbase.
- Coinbase WebSocket stop-fill notifications.

## Sequencing

1. Truncation scan on `bracket_writer_g2.py`,
   `coinbase_spot.py`, `coinbase_service.py`, `auto_trader.py`.
2. Read `bracket_writer_g2.py` RH stop callsite + capture call
   signature for parity test.
3. Read `coinbase_spot.py` current adapter for insertion point.
4. Write `place_stop_limit_order_gtc` in `coinbase_spot.py`.
5. Write `tests/test_coinbase_stop_primitive.py`.
6. Splice venue routing into `bracket_writer_g2.py`.
7. Write `tests/test_bracket_writer_venue_routing.py`.
8. Extend missing-stop repair sweep.
9. Populate `Trade.venue` at entry time if missing.
10. Run full pytest — RH parity gate held.
11. Force-recreate workers; verify multi-process import.
12. Single live paper-test (place + cancel). **Operator
    approval required.**
13. CC report.
14. Commit + push.

## Rollback plan

- Stop primitive misbehaves → `git revert` the bracket_writer
  venue splice. Adapter method + tests stay; RH unaffected.
- Coinbase stop placements rejected → emergency
  `CHILI_COINBASE_AUTOTRADER_LIVE=0` (stops new entries);
  manual-cancel open Coinbase stops.
- Adapter import error → revert `coinbase_spot.py` change;
  `docker compose up -d --force-recreate`.

## What CC should do if unsure

See full brief. Key one:

> **RH stop placement parity test fails**: STOP. RH stop path
> is byte-identical or nothing ships.

> **`OrderConfiguration.stop_limit_stop_limit_gtc` field shape
> uncertain**: surface SDK version + check Coinbase API docs.
> Do NOT guess; wrong field shape gets rejected at place_order
> time and we lose the live test.
