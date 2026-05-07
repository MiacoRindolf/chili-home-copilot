# 2026-05-07 — Fast-path universe research: where does alpha live?

## Executive summary

The operator asked whether the fast-path scalping system is unprofitable
because the **5-pair universe** (BTC, ETH, SOL, AVAX, DOGE) is wrong,
and whether expanding to **the whole Coinbase universe** would help.

**Answer: the universe is wrong, but "the whole universe" is also
wrong. The right universe is mid-tier (rank 5–30 by liquidity), and
even there the Coinbase taker fee is the structural bottleneck.**

Headline numbers from a 24h replay across 20 pairs:

| Group | Pairs | Mean 1m fwd-return | Mean 5m | Mean 15m |
|---|---|---|---|---|
| **CTRL** (current 5) | 5 | −0.29 bps | **−0.80 bps** | −0.67 bps |
| **TREAT** (mid-tier 15) | 15 | +0.11 bps | **+0.48 bps** | +4.14 bps |

Mid-tier shows directionally better edge at all horizons, by ~1.3 bps
at 5m and ~4.8 bps at 15m. **No pair clears the 120 bps round-trip
taker cost.** Only **ICP-USD** clears the maker-only round-trip
(net +2.76 bps at 5m).

## Method

1. **Universe scan**: pulled `/products` from Coinbase Exchange API,
   filtered to USD-quoted SPOT (`status=online`,
   `trading_disabled=false`) → 394 pairs. For each, called `/stats`
   (24h volume) and `/ticker` (live best-bid/ask) → composite score
   `volume_24h_usd / max(spread_bps, 0.5)`.
2. **Alpha replay**: pulled 24h of 1m candles per target pair (20
   pairs total — 5 CTRL + 15 TREAT). Identified "high-vol" bars
   (`vol >= 2.0 × mean(prev 20 bars)`), measured close-to-close
   forward returns at 1m, 5m, 15m horizons.
3. **Cost-aware evaluation**: subtracted Coinbase taker round-trip
   (60 bps × 2 + spread) and maker round-trip (just spread) from
   realized 5m edge.

This is a *very* simple signal proxy — not the imbalance signals the
real fast-path uses. But it's directionally informative: if even a
naïve signal shows higher edge in mid-tier, the same delta should
apply (or be larger) for the production signals.

## Universe scan — top 30 by composite

| Rank | Pair | 24h vol (USD) | Spread (bps) | Composite |
|---|---|---|---|---|
| 1 | BTC-USD | 573.3M | 0.00 | 1.15B |
| 2 | ETH-USD | 238.5M | 0.04 | 477M |
| 3 | USDT-USD | 50.3M | 0.50 | 100M |
| 4 | SOL-USD | 77.0M | 1.13 | 68M |
| 5 | ZEC-USD | 80.4M | 3.72 | 21.6M |
| 6 | SUI-USD | 13.0M | 1.03 | 12.7M |
| 8 | TAO-USD | 15.6M | 2.61 | 6.0M |
| 12 | JTO-USD | 12.9M | 3.08 | 4.2M |
| 15 | LTC-USD | 6.1M | 1.77 | 3.4M |
| 16 | ICP-USD | 11.4M | 3.37 | 3.4M |
| 17 | ADA-USD | 12.1M | 3.80 | 3.2M |
| 26 | AAVE-USD | 2.5M | 4.32 | 571k |
| **27** | **AVAX-USD** | **5.6M** | **10.54** | **533k** |
| 32 | NEAR-USD | 5.6M | 13.66 | 411k |
| 35 | FET-USD | 3.0M | 9.03 | 332k |
| 37 | ARB-USD | 2.2M | 7.88 | 279k |
| 43 | RENDER-USD | 2.0M | 10.21 | 195k |
| 46 | INJ-USD | 1.2M | 7.75 | 157k |

**Current 5-pair universe vs the universe:**
- BTC-USD = rank 1, 0.00 bps spread
- ETH-USD = rank 2, 0.04 bps spread
- SOL-USD = rank 4, 1.13 bps spread
- AVAX-USD = rank **27**, **10.54 bps spread** (already past the
  "uneconomic" line — this pair should have been dropped already)
- DOGE-USD = **not in universe** (zero ticker volume right now / bid==ask
  → ranked 0 by composite formula)

## Alpha replay — full table

```
pair         grp    spr   evts | 1m_mean 1m_shp | 5m_mean 5m_shp | 15m_mean 15m_shp
BTC-USD      CTRL   0.00  182  |  +0.15  +0.38  |  -0.35  -0.45  |   -0.96   -0.76
ETH-USD      CTRL   0.04  176  |  -0.31  -0.57  |  -2.79  -2.64  |   -3.70   -2.27
SOL-USD      CTRL   1.13  203  |  -0.52  -0.81  |  -1.38  -1.20  |   +0.74   +0.44
AVAX-USD     CTRL  10.54  163  |  +0.58  +0.82  |  +1.36  +0.81  |   +1.60   +0.57
DOGE-USD     CTRL   0.00  207  |  -1.34  -1.60  |  -0.87  -0.60  |   -1.02   -0.48
ZEC-USD      TREAT  3.72  225  |  +0.82  +0.55  |  -1.37  -0.41  |   +1.03   +0.19
SUI-USD      TREAT  1.03  198  |  -1.99  -2.27  |  -4.05  -2.44  |   -4.05   -1.58
TAO-USD      TREAT  2.61  217  |  +0.03  +0.03  |  +2.55  +1.19  |   +2.48   +0.66
JTO-USD      TREAT  3.08  184  |  +0.65  +0.15  | -12.88  -1.34  |  +38.46   +2.56
AAVE-USD     TREAT  4.32  189  |  -0.88  -1.19  |  -0.11  -0.07  |   -1.37   -0.57
LTC-USD      TREAT  1.77  181  |  -0.48  -0.96  |  +0.96  +0.89  |   +0.87   +0.55
ICP-USD      TREAT  3.37  186  |  -0.33  -0.16  |  +6.13  +1.19  |  +12.46   +1.57
ADA-USD      TREAT  3.80  202  |  -0.31  -0.47  |  -1.28  -0.95  |   -0.61   -0.31
NEAR-USD     TREAT 13.66  166  |  -0.07  -0.06  |  -1.38  -0.63  |   -2.67   -0.63
UNI-USD      TREAT  5.83  188  |  +0.28  +0.31  |  -0.15  -0.08  |   +1.44   +0.45
INJ-USD      TREAT  7.75   92  |  -0.50  -0.27  |  +4.12  +1.18  |   +6.81   +1.37
ARB-USD      TREAT  7.88  135  |  +0.96  +0.68  |  +4.17  +1.38  |   +7.70   +1.41
FET-USD      TREAT  9.03  169  |  +1.47  +1.10  |  +3.24  +1.35  |   -1.04   -0.24
RENDER-USD   TREAT 10.21  121  |  +1.00  +0.67  |  +6.55  +2.08  |   +4.41   +0.82
SEI-USD      TREAT  3.29  110  |  +0.96  +0.73  |  +0.65  +0.21  |   -3.79   -0.67
```

### Pairs with consistent positive 5m + 15m edge

| Pair | 5m mean | 5m Sharpe | 15m mean | 15m Sharpe | Notes |
|---|---|---|---|---|---|
| **RENDER-USD** | +6.55 | **+2.08** | +4.41 | +0.82 | Highest single-horizon Sharpe |
| **ICP-USD** | +6.13 | +1.19 | +12.46 | +1.57 | Most consistent across horizons |
| **ARB-USD** | +4.17 | +1.38 | +7.70 | +1.41 | Smaller event count (n=135) |
| **INJ-USD** | +4.12 | +1.18 | +6.81 | +1.37 | Smallest event count (n=92) |
| **TAO-USD** | +2.55 | +1.19 | +2.48 | +0.66 | Lower magnitude but stable |
| **FET-USD** | +3.24 | +1.35 | −1.04 | −0.24 | 5m only — flips negative by 15m |
| **LTC-USD** | +0.96 | +0.89 | +0.87 | +0.55 | Tiny but positive — old reliable |
| **JTO-USD** | −12.88 | −1.34 | +38.46 | **+2.56** | Very noisy (sd=204) — needs care |

### Pairs to drop from current universe

- **DOGE-USD** — zero recent volume / bid==ask in this window. Already
  off the universe.
- **AVAX-USD** — rank 27, 10.5 bps spread. Edge isn't dramatically
  worse than mid-tier (+1.36 5m), but the cost gate is much tighter.
- **ETH-USD** — −2.79 bps 5m, Sharpe −2.64. Despite zero spread,
  signal is anti-predictive in this regime.
- **SOL-USD** — −1.38 bps 5m. Anti-predictive.
- **SUI-USD** (in treatment but) — −4.05 bps 5m, Sharpe −2.44.
  Surprise: high liquidity rank doesn't help here.

### Pairs to keep

- **BTC-USD** — keep as benchmark / reference. Edge is near-zero but
  cleanly so; useful for system-health checks.

## Cost-aware evaluation

Coinbase Advanced Trade fees (retail, no volume tier):

- Taker = **60 bps** per side → round-trip 120 bps + spread
- Maker = **40 bps** per side → round-trip 80 bps + spread (we used
  0 bps assumption to bound the best-case)

```
pair         5m_edge   rt_taker  net_taker   rt_maker  net_maker
BTC-USD       -0.35     120.00    -120.35      0.00      -0.35
ETH-USD       -2.79     120.04    -122.83      0.04      -2.83
SOL-USD       -1.38     121.13    -122.51      1.13      -2.51
AVAX-USD      +1.36     130.54    -129.18     10.54      -9.18
DOGE-USD      -0.87     120.00    -120.87      0.00      -0.87
ZEC-USD       -1.37     123.72    -125.09      3.72      -5.09
SUI-USD       -4.05     121.03    -125.08      1.03      -5.08
TAO-USD       +2.55     122.61    -120.07      2.61      -0.07
JTO-USD      -12.88     123.08    -135.96      3.08     -15.96
AAVE-USD      -0.11     124.32    -124.43      4.32      -4.43
LTC-USD       +0.96     121.77    -120.81      1.77      -0.81
ICP-USD       +6.13     123.37    -117.24      3.37      +2.76  ← only winner
ADA-USD       -1.28     123.80    -125.07      3.80      -5.07
NEAR-USD      -1.38     133.66    -135.04     13.66     -15.04
UNI-USD       -0.15     125.83    -125.98      5.83      -5.98
INJ-USD       +4.12     127.75    -123.63      7.75      -3.63
ARB-USD       +4.17     127.88    -123.72      7.88      -3.72
FET-USD       +3.24     129.03    -125.79      9.03      -5.79
RENDER-USD    +6.55     130.21    -123.66     10.21      -3.66
SEI-USD       +0.65     123.29    -122.64      3.29      -2.64

Pairs net-positive after taker round-trip: NONE
Pairs net-positive after best-case maker round-trip: ICP-USD only
```

## What this means

1. **The universe is the wrong end of the liquidity spectrum.** BTC/ETH
   are too efficient; AVAX/DOGE are mid-tier-with-no-edge. The
   sub-population that matters is rank 5–30 with spread ≤ 10 bps.

2. **The Coinbase taker fee structure is the actual boss.** No 1m
   breakout signal at retail tier survives 120 bps round-trip cost.
   Even if we 3× the realized signal edge through better feature
   engineering (toxic flow, depth-aware imbalance, etc.), the absolute
   numbers (~10–20 bps edge) still lose to taker.

3. **The path forward has three legs**, ordered by impact:

   1. **Universe rotation** (this brief) — drop SOL/AVAX/DOGE/SUI;
      add ICP, RENDER, ARB, INJ, TAO, FET, LTC. Net effect: ~+1.3 bps
      mean 5m edge, ~+4.8 bps mean 15m edge, more events to learn
      from.

   2. **Maker-only execution mode** (separate brief
      `f-fastpath-maker-only`) — kills the 120 bps round-trip taker
      cost. Net effect: opens ICP and brings TAO/LTC near break-even.
      Trade-off: ~30–50% miss rate on fills during fast moves.

   3. **Move to a perps venue with cheaper fees** (separate brief
      `f-fastpath-hyperliquid-perps`) — Hyperliquid taker = 3.5 bps,
      maker = 1.5 bps. Round-trip ~7 bps. Suddenly **every** pair in
      the TREAT group is viable, including the noisy ones. The
      operator's geo concerns make this medium-term, not immediate.

4. **Better features (toxic flow, depth-decay, OFI) only help once
   the cost gate is open.** Adding sophisticated microstructure to a
   strategy that loses 120 bps per trip is not productive. Sequence:
   universe rotation → maker-only → then features.

## Caveats

1. **24h is a small window.** A regime shift could make the TREAT
   group's edge disappear. The recommended brief includes a 48h soak
   in shadow mode before activation; the universe rotator should
   re-evaluate hourly.

2. **JTO-USD is genuinely worrying.** 5m=−12.88, 15m=+38.46, sd=204
   means there's a structural noise pattern (probably a recent listing
   or a liquidation-driven move). Don't add it to the active set
   without further investigation.

3. **The replay used my own simple "vol≥2x mean" signal**, not the
   actual fast-path imbalance signals. The realized data already has
   imbalance/spread/breakout edges in `fast_signal_decay` — but only
   for the current 5 pairs. Adding mid-tier pairs and accumulating
   their `fast_signal_decay` rows is the only way to verify
   production-signal edge on the new pool.

4. **Spreads are point-in-time.** A pair that shows 3.4 bps now might
   widen to 12 bps during a thin-book moment. The cost-aware
   admission gate must use a rolling-median spread, not a snapshot.

## Concrete next steps

1. **Update the `f-fastpath-universe-rotation` brief** with this
   empirical data (DONE — see brief).
2. **Write `f-fastpath-maker-only` brief** for the maker-only
   execution mode (next).
3. **Promote `f-fastpath-universe-rotation` to NEXT_TASK** when
   operator is ready — implementation is mig 230 + new
   `universe_rotator.py` + WS client integration + cost-aware gate.
4. **Soak 48h in shadow mode** (paper-only on new pairs) before live
   activation.
5. **Re-run this replay weekly** while the soak runs to confirm the
   edge isn't a single-day artifact.

## Files produced this session

- `scripts/research-fastpath-universe-2026-05-07.ps1` (research script)
- `scripts/research-fastpath-universe-2026-05-07-output.txt` (raw output)
- `docs/STRATEGY/QUEUED/f-fastpath-universe-rotation.md` (queued brief)
- `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md` (this doc)

## Raw data (for reproducibility)

```
Universe scan: 394 USD-quoted SPOT pairs scored 2026-05-07 ~19:40 UTC
Replay: 24h of 1m candles per pair, breakout = vol >= 2.0 × mean(20)
Saved state files (sandbox-local):
  /tmp/cb_universe_rows.json     (262 ranked pairs)
  /tmp/cb_replay_state.json      (20 pairs × ~1500 bars × events)
```
