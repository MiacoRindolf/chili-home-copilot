# NEXT_TASK: f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing

STATUS: DONE

## Goal

Phase 5 of the Coinbase enablement initiative. Make the autotrader
**fee-aware** when routing to Coinbase. Coinbase Advanced Trade
Tier 1 is **60bps taker / 40bps maker**, so a 120bps round-trip
burns silently into edge if the min-edge gate doesn't account for
it. Phase 5 closes that gap and codifies per-venue notional caps +
the USD/USDC buying-power contract surfaced in Phase 2 G1.

The full brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing.md`
— **read it first.** ~3-4h CC scope. MEDIUM risk
(touches sizing/gate chain).

## Why now

Phase 4 (bracket writer Coinbase path) shipped 2026-05-09 (commits
`e70e80f` + `aca780d`):

- `place_stop_limit_order_gtc` shipped + venue-routed in
  `bracket_writer_g2.py`. RH stop path BYTE-IDENTICAL.
- 35/35 tests PASS in 10.58s. Cowork-direct verification
  confirmed import sanity + setting pickup across all 4
  workers.
- `_SUPPORTED_VENUES = {robinhood, coinbase}`.

Phase 5 is the **last hard prerequisite** before flipping
`CHILI_COINBASE_AUTOTRADER_LIVE=1` for paper soak (Phase 6).

## The change (4 components)

1. **Coinbase buying-power resolver** —
   `resolve_coinbase_buying_power()` returns `{usd, usdc, total,
   last_updated}`. Reads `cash` (USD wallet) + USDC quantity from
   `get_positions()`. 30s cache.
2. **Cost-aware min-edge gate** — runs BEFORE broker selector.
   For RH equity: fee=0 (no behavior change). For Coinbase:
   `expected_edge_bps >= fee_bps_round_trip + buffer_bps` else
   block.
3. **Per-venue notional caps** — `CHILI_COINBASE_MAX_NOTIONAL_USD=50`
   (conservative paper-soak default) +
   `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=3`. Independent from
   RH cap per design constraint #1.
4. **Autotrader splice + tests** — gate ahead of selector; cap
   check at routing decision; RH path BYTE-IDENTICAL (parity
   test); ≥6 gate cases + 2 cap cases.

## New settings (4)

```
CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP = 120  # 60+60 round-trip
CHILI_MIN_EDGE_SAFETY_BUFFER_BPS = 30          # cushion above fee
CHILI_COINBASE_MAX_NOTIONAL_USD = 50           # paper-soak default
CHILI_COINBASE_MAX_CONCURRENT_POSITIONS = 3
```

## Operator-locked design constraints (from Phase 1, still binding)

1. Cross-venue position cap: SEPARATE per-venue caps.
2. Kill switch: GLOBAL.
3. Selector: RH-first for both-listed.
4. Fast-path overlap: skip-on-fast-path-active.

## Acceptance criteria (10-item list)

See full brief. Headlines:

1. `resolve_coinbase_buying_power` shipped with documented shape.
2. Cost-aware gate shipped; RH equity behavior identical
   (fee=0).
3. Coinbase fee defaults set per Tier 1.
4. Per-venue notional caps set conservatively.
5. RH equity path BYTE-IDENTICAL (parity test).
6. ≥6 gate test cases + 2 cap test cases green.
7. No regressions on RH stop path.
8. Multi-process verification (4 workers).
9. Cost-gate audit log preserved.
10. CC report at canonical path.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **RH equity path BYTE-IDENTICAL**. Parity unit test gates it.
- **No new entry signal logic.** Phase 5 is sizing-side only.
- **No changes to `coinbase_spot.py` adapter** — Phase 4 shipped
  that.
- **NO flip of `CHILI_COINBASE_AUTOTRADER_LIVE=1`** during
  Phase 5. Stays operator-controlled.
- **No paper-soak.** Phase 6's job.
- **Edit-tool truncation discipline (HARD).** `auto_trader.py`
  is 1743 lines. `wc -l` + `git diff --stat` + AST-parse after
  every edit.

## Out of scope (Phase 5 — later phases)

- Paper-trade soak (Phase 6).
- Live with capital ramp (Phase 7).
- Coinbase Pro / different fee tiers.
- Maker-only routing.
- USDC-quoted (`-USDC`) ticker support.
- Dynamic universe rotation.

## Sequencing

1. Truncation scan on `auto_trader.py`, `broker_selector.py`,
   `coinbase_service.py`.
2. Read autotrader to find min-edge gate callsite + capture RH
   equity pre-state for parity.
3. Write `resolve_coinbase_buying_power`.
4. Write `cost_aware_min_edge_gate`.
5. Write tests (fail first, then green).
6. Add 4 settings to `app/config.py`.
7. Splice into `auto_trader.py`.
8. Run full pytest.
9. Force-recreate workers; verify multi-process pickup.
10. CC report.
11. Commit + push.

## Rollback plan

- Cost gate misbehaves → `git revert` autotrader splice; gate
  module + tests stay. RH path returns to pre-Phase-5 (no-op).
- Notional cap blocks legit entries → raise setting in `.env`
  + force-recreate.
- Buying-power resolver hangs → cache fallback to last value;
  >5min stale = CRITICAL + skip (conservative). Operator can
  also flip `CHILI_COINBASE_AUTOTRADER_LIVE=0`.

## What CC should do if unsure

See full brief. Key one:

> **RH equity parity test fails**: STOP. RH equity path is
> byte-identical or nothing ships.

> **Coinbase fee tier higher than 60bps taker**: read actual
> tier from operator (Coinbase UI shows this) and adjust
> default. Document in CC report.
