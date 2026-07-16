# SS101 — Day Trading: Strategies & Scaling (Ross) — visual study (THE core course)

Studied 2026-06-24, BATCHED (104 videos): 19 named chart-pattern SETUPS studied individually with key-frames (wave 1, 10 pairs); the 81-video SCALING section batched (wave 2, 7 batches). 15 edges synthesized + adversarially verified vs the DEPLOYED etfrank worktree.

## Chart-pattern setups — CHILI ALREADY HAS MOST (verify = reject/already)
Bull/Bear Flags, ABCD, MA/VWAP pullbacks, Double Top/Bottom, Cup&Handle, Head&Shoulders, VWAP traps, HOD/LOD breakouts, **pivot-break** (low-beneath-recent-high line-in-sand), **flat-top/HOD-consolidation breakout**, **jackknife/bull-trap veto** (break-then-red-reverse), **multi-add pyramid + add-back-after-scale-out**, **easy-to-borrow curl-reclaim**, **move-magnitude → arm the obvious leading gainer** — all verified **already deployed** in CHILI (the 14-edge lane + prior work). SS101 VALIDATES the setup engine.

## Genuinely-NEW edges (CHILI is missing these) — BUILDING (ws327vn5o)
1. **Float-rotation gate** (HIGH, SHIP): `float_rotation = cumulative_volume / float`; ≥~5x EOD = sustainable; a move that never clears ~1x float fades (insider selling). CHILI ranks RVOL + float INDEPENDENTLY but never divides them — the #1 SS101 edge. → ross_momentum/features tilt.
2. **Gap/window geometry** (HIGH): unfilled-gap to-the-penny trigger (gap bottom = prior candle high) + clear-sky room (dist to gap top); window (intra-candle) = weaker. CHILI's daily_levels has levels but no gap detection. → daily_levels tilt.
3. **Reverse-split recency FIX** (HIGH): CHILI **penalizes** "reverse split" (weak-catalyst keyword) — but Ross TARGETS a recent (<~1mo) reverse split + real news + low post-split float as a low-float squeeze (573k float $2→$50). CHILI is actively de-boosting the exact names Ross trades. → catalyst/edgar fix.
4. **Private-placement sign FIX** (MED): PP at/above market = institutional confidence (bullish), not a dilutive offering — split out of the blanket weak-catalyst de-boost. → catalyst.
5. **Red-rejection-history de-rate** (MED): de-rate a level with a daily history of large upper-wick red rejections. → daily_levels.
6. **Blue-sky/recent-IPO** (MED): true-ATH breakout boost gated to recent IPOs (<2yr, no trapped longs). → daily_levels.
7. **Iceberg per-add probe** (MED): on each pyramid ADD, filled-through vs displayed-ask — refill = hidden seller = stop adding. → live_runner add-path.

## Blocked-on-data / skipped
- **Borrow/short-squeeze-fuel tilt** (HIGH) — short-interest/utilization/cost-to-borrow/SSR is pure selection alpha for the 100-1000% verticals (the lane's weak point), BUT needs an Ortex-style short-data feed (external). High-value future build once the feed exists.
- **Per-name result-gated size-up** (LOW) — SKIPPED: up-sizing because a name is green today edges toward the snowball/streak-sizing RH101 explicitly warns against; contradicts non-escalating-sizing discipline.

## Scaling section (81 videos)
Share size IS the compounding lever ("$55 vs $55,000 = just how many shares"); ladder-adds at successive ½/whole-dollar breaks with stop-ratchet (CHILI has the single risk-neutral add; multi-add ladder verify = mostly already there); add-back on the first 1-min new-high after scale-out (intraday re-entry — CHILI has micro-pullback re-entry).

**Conclusion:** SS101 was worth insisting on — it VALIDATED CHILI's pattern engine AND surfaced the genuinely-missing selection intelligence (float-rotation, gap geometry, borrow/squeeze fuel, reverse-split/PP catalyst fixes). 7 buildable edges shipping; squeeze-fuel is the top blocked-on-data future build.
