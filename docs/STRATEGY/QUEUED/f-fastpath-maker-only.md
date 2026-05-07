# f-fastpath-maker-only

STATUS: QUEUED
SLUG: fastpath-maker-only
PROPOSED: 2026-05-07
REQUESTED_BY: empirical research finding (research doc 2026-05-07)
PREREQUISITE_FOR: live activation of `f-fastpath-universe-rotation`

## TL;DR

Add a maker-only execution mode to fast-path so the executor places
post-only limit orders inside the spread instead of crossing it with
market orders. The 24h replay study found that **no pair on Coinbase
clears the 120 bps round-trip taker cost**. Maker-only mode (40 bps
maker → with rebate-eligible POST_ONLY orders, often effectively
0–10 bps) is the only path to economic positive-EV trading on
Coinbase at retail tier.

Trade-off: ~30–50% miss rate during fast moves (price walks away
before the limit fills). The signal must persist long enough to
justify the limit-resting time.

## Why

From `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md`:

| Pair | 5m edge | rt_taker | net_taker | rt_maker | net_maker |
|---|---|---|---|---|---|
| ICP-USD | +6.13 | 123.4 | −117.2 | 3.4 | **+2.76** |
| RENDER-USD | +6.55 | 130.2 | −123.7 | 10.2 | −3.7 |
| ARB-USD | +4.17 | 127.9 | −123.7 | 7.9 | −3.7 |
| INJ-USD | +4.12 | 127.8 | −123.6 | 7.8 | −3.6 |
| TAO-USD | +2.55 | 122.6 | −120.1 | 2.6 | −0.07 |

Even the best pairs lose 117 bps per round trip on taker. Maker-only
flips half of these from "structural loss" to "near break-even" or
"small positive". Combined with universe rotation, this opens the door
to the first real positive-EV scalping configuration.

## Scope

### In scope

1. **`fast_path/executor.py` — new `place_maker_only` path**:
   - Limit price = `best_bid + 1 tick` for long entries (sit at the
     bid+ε, hope to be filled by aggressive sellers).
   - Coinbase Advanced Trade flag: `post_only=true` (rejects if it
     would cross). Confirms the maker fee tier.
   - Cancel-on-timeout: 5–15 s default. If unfilled, cancel and
     either re-quote at new bid or abandon the alert.
   - Mirror logic for short entries (limit at `best_ask − 1 tick`).

2. **Mode flag** (`settings.FAST_PATH_EXECUTION_MODE`):
   - `taker` (current behavior, retained for benchmark)
   - `maker_only` (new)
   - `maker_first_then_taker` (try maker, fall back to taker after T s
     — operator-controlled compromise)

3. **Fill probability tracking** (`fast_path_maker_attempts` table):
   ```sql
   CREATE TABLE fast_path_maker_attempts (
     id bigserial PRIMARY KEY,
     alert_id bigint REFERENCES fast_alerts(id),
     ticker varchar,
     side varchar,
     limit_price double precision,
     placed_at timestamp,
     filled_at timestamp,
     cancelled_at timestamp,
     final_price double precision,
     fill_outcome varchar,  -- 'filled', 'cancelled', 'partial', 'replaced'
     time_to_fill_ms integer,
     spread_at_placement_bps double precision,
     spread_at_fill_bps double precision,
     mid_drift_bps double precision  -- mid price drift from placement to fill
   );
   ```

4. **Post-fill economics audit**:
   - Compute realized fee tier from broker fill confirmation.
   - Verify `post_only` flag is honored (no surprise taker fees).
   - Alert if any fill comes back at taker rate when maker was
     intended (broker-side failure).

5. **Replay/calibration**:
   - The current `fast_signal_decay` table assumes immediate fill at
     best price. With maker-only, *fills are biased*: filled trades
     are the ones where price came TO the limit, which is the
     *adverse-selection* tail. Forward returns look worse than
     no-friction backtest suggests.
   - Add a separate calibration table
     `fast_signal_decay_maker_filled` that only counts events where
     a maker order WOULD have filled (= price touched limit within T
     seconds before continuing in signal direction).

### Out of scope (separate briefs)

- Order book queue position estimation
  (`f-fastpath-queue-position-est`).
- Adaptive limit-tick offset based on book depth
  (`f-fastpath-adaptive-limit-offset`).
- Smart order routing across venues (`f-fastpath-smart-routing`).
- Hyperliquid perps (`f-fastpath-hyperliquid-perps` — different
  venue, different rules).

## Risks / hazards

1. **Adverse selection**: a maker fill happens when someone aggressive
   ate through the resting limit, often because they have information
   you don't. Realized post-fill returns are typically 30–60% worse
   than no-friction backtest. Mitigate: use `fast_signal_decay_maker_filled`
   for calibration, not the no-friction table.

2. **Cancel storms**: if signals fire faster than the cancel-on-timeout
   period, the system can build up dozens of stale resting limits.
   Mitigate: hard cap of 1 outstanding maker order per (ticker, side)
   in `executor.py`.

3. **Coinbase post-only rejection rate**: when book is thin or
   crossing, post_only orders are rejected at exchange. Track in
   `fast_executions.reject_reason='post_only_would_cross'`. If >10%
   of attempts reject, the limit-tick offset is too aggressive.

4. **Fill latency vs signal half-life**: many imbalance signals
   decay in 100–500 ms. A 5-second limit timeout means the maker
   order is filled into a stale signal. Mitigate: per-signal
   `maker_max_wait_s` calibrated from `fast_signal_decay` mean-decay
   horizon.

5. **Partial fills**: maker orders frequently fill 30–80% of size
   then sit. Position-sizing math must handle partials gracefully.
   Mitigate: bookkeeping in `fast_executions.quantity` already
   supports this; verify the `exit_manager.py` reads partials
   correctly.

## Acceptance criteria

1. `executor.py` has a working `mode='maker_only'` path that places
   `post_only=true` limit orders, tracks them in
   `fast_path_maker_attempts`, and either fills or cancels.
2. `fast_signal_decay_maker_filled` has ≥ 30 samples in at least one
   `(ticker, alert_type, score_bucket)` cell after a 48 h soak.
3. Realized fill rate per pair is logged hourly. Pairs with fill rate
   < 25% are flagged as "uneconomic for maker-only" and excluded from
   the active universe.
4. **Empirical proof**: at least one (ticker, alert_type) cell shows
   `fast_signal_decay_maker_filled.mean_return > 2 × maker_round_trip`
   with `sample_count ≥ 30`. If after 72 h none does, the next brief
   is `f-fastpath-hyperliquid-perps`.

## Dependencies

- `f-fastpath-universe-rotation` — must ship first, so we have a
  curated pool to test maker-only on. Otherwise we're testing
  maker-only on BTC/ETH where the signal edge isn't there anyway.
- Coinbase Advanced Trade `post_only` order parameter (already
  supported by Coinbase's API per their docs).
- Migration ID: 231 (after universe rotation's 230).

## Sequencing

1. Universe rotation ships (mig 230, prerequisite).
2. Migration 231 + `fast_path_maker_attempts` + `fast_signal_decay_maker_filled`.
3. `executor.py` maker_only path + post-fill audit.
4. `mode='maker_first_then_taker'` for staged rollout.
5. 48 h soak in shadow mode (paper-only on the new universe with
   maker_only enabled). Compare:
   - Fill rate per pair
   - Realized vs no-friction edge (the adverse-selection tax)
   - Net P&L vs taker baseline
6. If fill rate < 25% on any pair, drop it from the active universe.
7. Activation review.

## Files likely to change

- `app/migrations.py` (mig 231)
- `app/services/trading/fast_path/executor.py` (new mode)
- `app/services/trading/fast_path/settings.py` (mode flag)
- `app/services/trading/fast_path/calibration.py` (new
  `fast_signal_decay_maker_filled` reader)
- `app/services/trading/fast_path/decay_miner.py` (write the new
  table when it observes post-fill outcomes)
- `tests/test_fastpath_maker_only.py` (new)
- `tests/test_fastpath_partial_fill_handling.py` (new)
