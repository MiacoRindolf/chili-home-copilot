# Current Plan: Fast Path Crypto Scalping

**Initiative owner:** Cowork (strategy) + Claude Code (execution).
**Last update:** 2026-05-01, after F5 first paper soak.

## Goal of the initiative

Build a parallel "fast lane" execution subsystem on top of CHILI's existing brain that can autonomously paper-trade Coinbase 1m crypto scalp setups, prove edge with realized P/L data, then graduate to live placement under explicit operator authorization.

**Edge proof bar:** > 50 round-trip paper exits across multiple sessions with positive total realized P/L and statistically defensible win rate, before live activation is even discussed.

## What's shipped (high level)

- **F1** — Coinbase WS bar ingestion → `fast_snapshots`. (`46b94c2` + `1522417`)
- **F2** — L2 order book mirror → `fast_orderbook` with imbalance / spread features. (`dda20d2`)
- **F3** — Momentum scanner → `fast_alerts` (volume_breakout, imbalance, spread_squeeze). (`80d1551`)
- **F4** — Paper-mode executor + 6 gates + mode interlock. (`1431cb9`)
- **F4 UI** — Autopilot page real-time paper P/L view. (`f420ea6`)
- **F4 live** — Real Coinbase placement code wired behind 8 safety belts. (`8f18be5`)
- **F5** — Exit manager (`exit_manager.py`) + migration 218 `fast_exits` + brain-derived stop/target via `stop_engine.compute_initial_bracket`. **NOT YET COMMITTED as of 2026-05-01.**

## First paper soak result (F5)

- 3 round trips closed. **0% win rate. -$0.27 total P/L on $75 traded notional.**
- All 3 exits were DOGE-USD `stop_hit` after ~43 min holding.
- 0 target_hit, 0 time_stop. 8 positions still open.
- Pipeline plumbing: ✅ healthy, sub-millisecond decision latency, brain integration captured `brain_json` per trade.
- Strategy: ❌ holding period and bracket sized for swing, not scalp. Imbalance signal predictive horizon is 1-5 seconds; we're holding 40+ minutes.

## Open architectural concerns (not yet addressed)

From the two-lens evaluation 2026-05-01:

**Algo:**
1. ~~No exit logic~~ → F5 shipped.
2. **Magic numbers in gates.py and exit_manager.py** (min_score=0.30, max_hold_s=14400, vol_breakout_mult=2.0, imbalance thresholds 0.65/0.35).
3. ~~No risk frame~~ → F5 wired stop_engine.
4. ~~Position sizing fixed $25~~ → still pending; deferred to F7.
5. **No correlation gate.** Could long all 5 crypto pairs at once.
6. **Pattern miner doesn't see fast lane.** `learning.py` mines equity 1d bars; needs an extension to mine 1m crypto from `fast_snapshots`/`fast_alerts`.

**Dev:**
1. **1Hz polling** instead of LISTEN/NOTIFY (executor + exit_manager).
2. ~~`_open_positions` in-memory~~ → resolved by F5 querying via LEFT JOIN fast_exits.
3. **No portfolio-level concurrency cap** (only per-pair).
4. ~~No realized P/L schema~~ → F5 shipped fast_exits.
5. **No per-asyncio-task watchdog.**
6. **Recency window 60s too wide** for scalp signals.
7. **Stale `last_error`** in fast_path_status persists.
8. **Autopilot UI 5s polling** vs. existing /ws/autopilot/live pattern.

## Findings — 2026-05-02 (F6 signal-decay miner)

> *This section supersedes parts of the older priority list below. Treat the older list as historical context — F6's empirical answers change which next moves are load-bearing.*

F6's cold-start backfill mined every `fast_alerts` row from the last 7 days against the `fast_orderbook` trajectory at 8 forward-return horizons (1s through 4h). Aggregated by `(ticker, alert_type, score_bucket, horizon_s)` into `fast_signal_decay`. The data is empirical, not theoretical; the conclusions below are facts about the system as it currently exists.

### What we now know

1. **`volume_breakout_long` is negatively predictive.**
   Across 2,004 observations (n=120 bucket rows), mean forward return is **−28.5 bps**. Per-bucket the worst is `SOL-USD med` at mean=−27 bps, upper-95%-CI=−21 bps. **The signal as currently constituted correlates with mean reversion, not breakout continuation, on Coinbase 1m crypto.** This is the load-bearing finding from F5's loss data (DOGE stop-hits) — not bad luck, but exactly what the signal does on average.
2. **Imbalance signals are too small to clear cost.**
   Best non-trivial bucket is `BTC-USD imbalance_long med` at mean=+9 bps over 60 obs. At any reasonable trading cost assumption (Coinbase Advanced Trade taker is ~40 bps single-direction; round-trip ~80 bps + spread), no scanner signal currently clears the bar. The brief's default 200 bps threshold blocks every fill — correct outcome of the brief's settings on the data we have.
3. **Calibrated max-hold is sub-10s, not 4 hours.**
   Sharpe-best horizon for high-score buckets falls at the 1–5s end of the spectrum. Matches quant-lit consensus on order-book-imbalance predictive horizon. The hardcoded `MAX_HOLD_S_DEFAULT = 4 × 3600` in `exit_manager.py` was three orders of magnitude wrong on the same signals. F6.5 added an execution-latency floor (`CALIB_EXEC_FLOOR_S = 10s`) so calibrated max-hold below ~10s gets floored — anything below that is below round-trip placement latency anyway.

### Edge-proof bar — superseded by empirical answer

The original criterion (top of this file: *"> 50 round-trip paper exits across multiple sessions with positive total realized P/L"*) is **superseded** by the F6 finding. F6 has answered the same question across hundreds of pre-trade alert trajectories without needing to wait for the soak window: **the existing scanner signals do not produce edge that beats trading cost at any reasonable threshold.**

We're not going to reach 50 round trips with positive P/L on these signals. The math says so directly.

### Load-bearing next move: F8 (signal redesign)

F8 — design and prototype new signals — is now the load-bearing next move. The current three signals (`volume_breakout_long`, `imbalance_long`, `imbalance_short`, plus the `spread_squeeze` n=14 sample we can't yet judge) don't have edge. F8 candidates worth exploring:

- A **fade volume_breakout** signal — explicitly trade against the existing signal since it's reliably wrong.
- **Microstructure signals** that don't decay in 1–5s — order-flow imbalance against trade-flow, queue dynamics on the deeper book, large-print detection.
- **Cross-pair lead-lag** — BTC moves predict ETH/SOL with measurable lag; encode that as an alert.
- **Time-of-day / session signals** — most quant lit uses session boundaries; we don't.

All require operator design input — F8 is collaborative, not pure execution.

### F7 (Kelly sizing) deferred

F7 sizes positions for a tradeable signal. F6 has shown we don't have one yet. Sizing a no-edge signal with Kelly produces zero or negative Kelly fractions; it's wasted work. **F7 stays paused until F8 produces a signal with empirical edge.**

### What we don't yet know

- **Whether scalp-credible edge exists on Coinbase 1m crypto data at all.** The signals we tried don't have it. That's not the same as proving no signal does. F8 is the experiment.
- **Whether the calibrated max-hold floor of 10s is the right number.** Set by execution latency reality (~200-500ms × 10–50× headroom); could be tighter or looser once we observe live placement timing.
- **Whether `spread_squeeze` would have edge with more data.** Currently n=14, mean +5 bps — promising but statistically inconclusive. Scanner cooldown may be limiting fire rate; out-of-scope for F6.

## Direction for next 3-5 tasks (subject to operator approval)

> *This list is the pre-F6 priority order. F6 + F6.5 have shipped (and superseded #1 above); the remaining items below stay relevant but are reordered behind the new F8 work — see the 2026-05-02 findings section above for the load-bearing next move.*

In order of expected impact:

1. **F6 — Signal half-life mining brain node.** Replaces the hardcoded `max_hold_s=14400` with a per-(pair, alert_type) value derived from observing how long it takes price to mean-revert past entry on `fast_alerts` history. This is the user's "no magic numbers, let chili learn it" principle applied to the most important magic number we have. **Output:** new table `fast_signal_decay`, populated by a learning_cycle_step extension. Exit manager reads from it.

2. **F7-precursor — Position sizing via `position_sizer_model.compute_proposal()`.** Replaces fixed $25 notional with Kelly-fraction sizing using stop distance + ATR. Brain-derived, no magic numbers.

3. **Switch executor + exit_manager from poll to LISTEN/NOTIFY.** Drops decision latency floor from up to 1000ms to <10ms. Pattern lifted from `app/services/trading/price_bus.py`.

4. **Portfolio + correlation gates.** Use `app/services/trading/correlation_budget.py` bucketing. Cap simultaneous opens across all pairs.

5. **Watchdog task in supervisor.** Per-asyncio-task heartbeat; logs WARN if scanner sees no books for 30s while WS reports streaming.

After these five, we'll have realized P/L data over many soaks at proper scalp timeframes — then we can have the "is there edge?" conversation honestly.

## Out of scope right now

- **Live Coinbase placement.** Wired but gated. Not even a candidate until F1-F8 are stable AND > 50 paper round trips show edge AND operator explicitly authorizes per the contract in `docs/FAST_PATH_HANDOFF.md`.
- **Adding new alert types.** Three signals (volume_breakout, imbalance, spread_squeeze) is enough surface area to validate the architecture. New signals are F8+.
- **Web UI improvements beyond the existing autopilot section.** SSE/WebSocket conversion is on the dev-architect list but cosmetic until edge is proven.
