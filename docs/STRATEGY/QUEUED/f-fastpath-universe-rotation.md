# f-fastpath-universe-rotation

STATUS: QUEUED
SLUG: fastpath-universe-rotation
PROPOSED: 2026-05-07
REQUESTED_BY: operator (after live `fast_path` was found unprofitable)

## TL;DR

Replace the hardcoded 5-pair fast-path universe (`BTC-USD, ETH-USD,
SOL-USD, AVAX-USD, DOGE-USD`) with a **data-driven mid-tier rotation**
selected hourly from the live Coinbase universe by liquidity-adjusted
score. Add a **cost-aware admission gate** that refuses to trade any
signal whose calibrated edge is smaller than `2 × (taker_fee +
median_spread)`. Together these two changes turn the question "is the
strategy profitable?" from a structural problem (wrong pairs) into an
empirical one (does any pair clear the cost gate?).

## Why

`fast_signal_decay` after 6 days and 44,656 alerts shows realized
forward-return on the current 5 pairs of **0.06–0.5 bps at 1–60s and
−0.6 to −2.8 bps at 1h**. Coinbase Advanced Trade taker fee at retail
tier is **60 bps**; round-trip 120 bps. The realized edge on BTC/ETH is
**~1000× smaller than the trading cost** — no signal-quality work can
close that gap. The pairs are simply too efficient: institutional HFT
has already extracted the microsecond imbalance edge before the
`scanner` 250 ms tick fires.

The architectural fix is to move to pairs where the cost-to-edge ratio
is achievable:
- 24h volume ≥ $10M (so $25–$250 paper notional doesn't move the book)
- Median spread ≤ 10 bps (so round-trip cost stays below realistic edge)
- Top-of-book size ≥ $5k (so taker fills don't slip)
- ≥ 1k trades / 24h (so a signal has a base rate to learn from)

Coinbase rank ~10–50 by 24h volume should yield 15–30 names that fit
(LINK, UNI, AAVE, ATOM, ARB, OP, NEAR, INJ, SUI, APT, RUNE, FET, SEI,
RENDER, JTO, TIA, FIL, AVAX, …). These names retain enough retail
behavioral flow that a 1m bar can capture momentum that lasts >1m.

## Scope

### In scope

1. **`fast_path_universe` table** (new, mig 230+):
   ```sql
   CREATE TABLE fast_path_universe (
     ticker varchar PRIMARY KEY,
     status varchar NOT NULL,          -- active | candidate | retired
     rank_score double precision,      -- composite (volume_24h_usd / spread_bps)
     volume_24h_usd double precision,
     median_spread_bps double precision,
     top_book_size_usd double precision,
     trade_count_24h integer,
     promoted_at timestamp,
     last_evaluated_at timestamp,
     evaluation_metadata jsonb         -- raw stats snapshot
   );
   ```

2. **Universe rotator** (`fast_path/universe_rotator.py`, new):
   - Scheduler job, every 60 min.
   - Calls Coinbase Exchange `/products` and `/products/{id}/stats` +
     `/ticker` for all USD-quoted SPOT products.
   - Computes composite score: `volume_24h_usd / max(spread_bps, 0.5)`.
   - Picks top-N (default 25) that pass the gates above.
   - Writes to `fast_path_universe`; demotes pairs that drop out.
   - Triggers WS subscription update on `ws_client`.

3. **Subscription manager update** (`fast_path/ws_client.py`):
   - Read `fast_path_universe WHERE status='active'` instead of
     `settings.FAST_PATH_TICKERS`.
   - Diff against current subscriptions; subscribe / unsubscribe.

4. **Cost-aware admission gate** (`fast_path/gates.py`,
   `gate_cost_aware_admission`):
   - For each `(ticker, alert_type, score_bucket)` in
     `fast_signal_decay`, compute `min_required_return = 2 * (
     taker_fee_bps + median_spread_bps_for_ticker)`.
   - Reject if `mean_return < min_required_return` at the best-Sharpe
     horizon, **regardless of statistical significance**.
   - This is stricter than the current `gate_calibrated_tradeability`
     (which uses `2 × trading_cost` as a floor, but `trading_cost` is
     a single global constant — not per-ticker, not fee-aware).

5. **Operator visibility**:
   - `/api/trading/fast-path/universe` endpoint returning current
     active set + recent rotations.
   - `fast_path_universe.evaluation_metadata` keeps the last 7 days of
     stats so we can audit *why* a pair was admitted/demoted.

### Out of scope (separate briefs)

- Maker-only execution mode (`f-fastpath-maker-only`).
- Hyperliquid perps integration (`f-fastpath-hyperliquid-perps`).
- Multi-exchange basis signals (`f-fastpath-cross-exchange-basis`).
- Toxic-flow / order-book-depletion-rate features
  (`f-fastpath-microstructure-features-v2`).

## Risks / hazards

1. **Coinbase rate limits**: public REST is 10 req/s. Universe scan is
   ~250 products × 2 calls = 500 calls = ~50 s if naively serial.
   Mitigate: batch with backoff; cache for 60 min between scans.

2. **WS subscription churn**: rotating 5+ pairs every hour means
   constant subscribe/unsubscribe traffic. Coinbase WS is tolerant but
   `ws_client.py` reconnect counters will tick up. Mitigate: rotate
   only when a pair drops out of top-N by ≥3 positions (hysteresis).

3. **Calibration cold-start**: a freshly admitted pair has 0 rows in
   `fast_signal_decay` → no historical edge data → all signals
   `gate_calibrated_tradeability` denied. Need a **shadow learning
   window** (e.g., 24–48 h) where alerts fire and decay miner builds
   stats but executor is denied. Memory entry confirms `decay_miner`
   already does cold-start backfill — reuse that path.

4. **Cost-aware gate over-blocks**: if `median_spread_bps_for_ticker`
   spikes during a thin-book moment, the gate goes stale. Mitigate:
   compute spread from a 30-min rolling median, not last tick.

5. **Universe size vs decay-miner load**: 25 pairs × 4 alert_types × 3
   score_buckets × 8 horizons = 2,400 cells in `fast_signal_decay` per
   pair, vs the 518 we have for 5 pairs. Mostly fine but worth
   benchmarking the Welford updates under load.

## Acceptance criteria

1. `fast_path_universe` populated with ≥10 active pairs that satisfy
   the four gates.
2. WS client subscribes to the active set; `fast_path_status` shows
   the new pairs streaming.
3. `fast_signal_decay` accumulates rows for the new pairs over a
   48 h soak.
4. `gate_cost_aware_admission` is logged in `fast_executions.gates_json`
   for every alert; rejection rate is reported in
   `/api/trading/fast-path/realized-stats`.
5. **Empirical proof point**: at least one (ticker, alert_type) cell
   in `fast_signal_decay` shows `mean_return > 2 × cost` at horizon
   ≤300s with `sample_count ≥ 30`. If after 72 h none does, the
   hypothesis fails and the next brief is `f-fastpath-maker-only` (the
   only remaining lever).

## Dependencies

- Coinbase Exchange API public read access (no auth needed).
- Migration ID > 229 (next is 230).
- No conflicts with the prediction-mirror authority contract or with
  the bracket writer (these are autotrader-side; fast-path is a
  separate lane).

## Sequencing

1. ~~Write the universe scan + replay research script first~~ DONE
   2026-05-07. Research output:
   `scripts/research-fastpath-universe-2026-05-07-output.txt`. Full
   writeup: `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md`.
   **Findings**:
   - Mid-tier (rank 5–30) shows **directionally better edge** than the
     current 5: CTRL 5m mean −0.80 bps vs TREAT 5m mean **+0.48 bps**;
     15m mean −0.67 vs **+4.14 bps**.
   - Top realized 5m+15m edge: **ICP, RENDER, ARB, INJ, TAO, FET**
     (5m edge 2.5–6.6 bps, Sharpe 1.2–2.1). Drop candidates: AVAX,
     SOL, DOGE, SUI, ETH (negative or anti-predictive in this window).
   - **Cost gate verdict**: NO pair clears taker round-trip (120 bps).
     Only ICP-USD clears the maker round-trip (+2.76 bps net). This
     promotes `f-fastpath-maker-only` from "follow-up" to "**hard
     prerequisite for live activation**". The universe-rotation brief
     stands, but its acceptance criterion #5 (an empirical proof that
     a pair clears `2 × cost`) cannot be met until maker-only mode
     ships.
2. Migration + table.
3. Universe rotator.
4. Cost-aware gate.
5. WS client integration.
6. 48 h soak in shadow mode (paper-only on new pairs).
7. Activation review.

## Files likely to change

- `app/migrations.py` (mig 230)
- `app/services/trading/fast_path/universe_rotator.py` (new)
- `app/services/trading/fast_path/ws_client.py` (subscription read path)
- `app/services/trading/fast_path/gates.py` (`gate_cost_aware_admission`)
- `app/services/trading/fast_path/settings.py` (deprecate `FAST_PATH_TICKERS`)
- `app/services/trading_scheduler.py` (universe rotator job)
- `app/routers/trading_sub/fast_path.py` (status endpoint)
- `tests/test_fastpath_universe_rotator.py` (new)
- `tests/test_fastpath_cost_aware_gate.py` (new)
