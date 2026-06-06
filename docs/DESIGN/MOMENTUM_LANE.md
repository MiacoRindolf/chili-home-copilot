# Momentum Lane — a Ross-Cameron-style momentum/catalyst selection + execution flow

Status: **DESIGN (M0)** · Owner: trading-brain · Created 2026-06-06

## 1. Why this exists (the problem)

CHILI barely trades, and when alerts fire they are overwhelmingly rejected with
`non_positive_expected_edge` / `cost_gate` / `coinbase_below_fee_threshold`. A
code audit (2026-06-06) showed this is **not** a broken gate — it is a
**selection** problem:

- CHILI's decision spine is **pattern-certification-FIRST**: mine a technical
  `ScanPattern` → certify (CPCV/OOS or realized-PnL) → fire when conditions
  near-trigger → pass confidence + **expected-value** gates → execute.
- The composite ranking score (`opportunity_scoring.compute_composite_score`)
  is **100% technical/statistical** — no relative-volume, gap, float, or
  catalyst term.
- The one Ross-style filter (`auto_trader_rules._stock_momentum_context_gate`)
  is (a) **stocks-only**, (b) only active when the candidate queue is full,
  (c) **exempt for certified patterns** (the ones that actually trade). **Crypto
  gets no catalyst/RVOL/float filtering at all.** Float is a soft scoring nudge,
  never a gate; short interest is absent.

A discretionary momentum day-trader (Ross Cameron / Warrior Trading) inverts
this. His **edge is the selection filter**, not the chart pattern:

> **The 5 pillars (non-negotiable entry universe):** up ≥10% (gapping) ·
> price $2–$20 · **RVOL ≥5×** · **news catalyst** · **float <20M shares**.
> Then a *simple* continuation pattern (bull flag / micro pullback), a tight
> stop under the pullback, and a 2:1+ target. ~71% win rate; avg winner ≈4.4×
> avg loser (cuts losers fast).

The patterns are generic; they work because the **instrument is explosive**.
CHILI looks for the right *shapes* on *average* instruments → small expected
move → correctly cost-gated. **The fix is to select explosive instruments
FIRST, then trade a simple momentum trigger — Ross's actual flow.**

## 2. Decision

Build a **new Momentum Lane**: a rule-based, momentum-FIRST selection +
execution flow that runs **alongside** (not inside) the existing
pattern-certification lane. We do **not** bolt momentum onto the pattern engine
(different DNA — it requires certified patterns; Ross does not). The existing
EV/cost gates are sound and are **reused unchanged** as the final capital
protection.

Rule-based (not the existing `momentum_neural` ML engine) because Ross's method
is explicit and transparent; we want debuggable, auditable selection. We *reuse*
the existing momentum data plumbing where it exists (`scanner.py:1502-1617`
float/gap/gainer scoring; `vol_ratio`/`gap_pct` features; Massive screens).

**Crypto-first** (24/7 — usable this weekend), **stock-capable** (so we can
validate against Ross's real stock trades).

## 3. Architecture (additive, clean)

```
            ┌─────────────────────── MOMENTUM LANE (new) ───────────────────────┐
 universe → │ MomentumScreener → momentum-continuation trigger → Ross risk model │ → entry intent ─┐
            └────────────────────────────────────────────────────────────────────┘                │
                                                                                                    ▼
 (existing) pattern-imminent → composite score → ────────────────────────────── entry intent ─► EXECUTION + SAFETY
                                                                                                (kill switch, drawdown
                                                                                                 breaker, position/lane
                                                                                                 caps, cost/EV sanity,
                                                                                                 bracket writer, broker)
```

### 3.1 `MomentumScreener` (the selection edge)
For each instrument in the tradable universe, compute the Ross pillars,
**crypto-adapted**, and rank by a `momentum_quality_score`:

| Ross pillar (stocks) | Crypto-adapted signal | Source |
|---|---|---|
| Float < 20M (low float) | **Market cap / circulating supply** within an "explosive but liquid" band | market-data provider (CoinGecko/Massive) |
| RVOL ≥ 5× | **RVOL** = current vol / trailing-N average | OHLCV (`vol_ratio` exists) |
| Gap ≥ 10% / new HOD | **Momentum** = % move over lookback + proximity to recent high | OHLCV |
| News catalyst | **Catalyst proxy**: RVOL spike + ATR/volatility expansion (later: listing/social/news) | OHLCV + (later) news |
| Price $2–$20 | (n/a for crypto; use a min-liquidity floor instead) | — |

**Adaptive thresholds — NO magic numbers.** Thresholds are **percentile ranks
within the current universe** (e.g. require top-quartile RVOL AND positive
momentum AND market-cap in the explosive band), recomputed each tick. This keeps
the lane self-calibrating across regimes and avoids hardcoded `5×`/`10%`.

Output: a ranked shortlist of **explosive candidates** (+ each one's pillar
breakdown for auditability).

### 3.2 Momentum-continuation trigger (the execution)
On a screened candidate's recent bars, detect a continuation entry **directly**
(no certified `ScanPattern`):
- **New-high breakout**, **micro-pullback** (pull back to a short MA / prior
  micro-consolidation then resume), or **bull flag** (flagpole + tight
  consolidation + breakout).
- Entry trigger (Ross): price breaks the high of the first pullback (red) candle.

### 3.3 Ross risk model
- **Stop** just under the pullback / consolidation low.
- **Target** 2:1+ R:R (or scale-out at the prior high + trail).
- **Position size** by fixed-fractional risk per trade (small, defined).
- **Cut losers fast** — the tight stop is the mechanism; the asymmetry
  (winner ≫ loser) is half the edge.

### 3.4 Integration & safety (reuse, don't reinvent)
- New scheduled job `momentum_lane_tick` (crypto: ~30–60s, 24/7) in the
  autotrader-only container, **separate from** the `auto_trader` tick.
- Entry intents route through the **existing** execution path: kill switch +
  drawdown breaker (Hard Rules 1–2), portfolio/position limits, cost/EV sanity,
  bracket-intent writer, broker venue adapter. **No new execution or safety
  code** — the lane only produces an intent.
- Its own concurrency budget (`max_concurrent_momentum`) inside the global cap.
- Live + on (no dark flag), with conservative initial sizing + close
  observation, per operator work-style.
- Secondary, complementary change: add a `momentum_quality_score` term to the
  existing `compute_composite_score` so the **pattern lane** also prioritizes
  explosive setups.

## 4. Validation plan (must pass before/with go-live)
1. **Vs Ross's real trades:** replay his recent actual trades (research M1)
   through the screener on stock data — would it have flagged them? (Does the
   pillar profile match? precision on his universe.)
2. **Vs CHILI's rejections:** the recently cost-gated crypto setups should score
   **low** on `momentum_quality` (confirming they're generic), and the screener
   should surface **different, higher-quality** crypto candidates.
3. **Impact match (post-live):** the lane's actual fills should resemble the
   Ross profile (high RVOL, momentum, asymmetric win/loss), and realized PnL
   should be net-positive after costs. Compare directly to Ross's recent trade
   character.

## 5. Phases
- **M0** — this design doc.
- **M1** — Ross recent-trades ground-truth (research, in progress).
- **M2** — `MomentumScreener` (pillars + adaptive ranking), crypto + stock.
- **M3** — validate screener vs Ross trades + vs CHILI rejections.
- **M4** — momentum-continuation trigger + Ross risk model.
- **M5** — `momentum_lane_tick` + execution/safety integration + concurrency.
- **M6** — live, observe, compare impact to Ross; feed realized-PnL promotion.

## 6. Non-goals / guardrails
- Not loosening any existing gate (they correctly reject negative-EV trades).
- Not an ML engine; explicit rules for transparency.
- Not touching the prediction-mirror authority (Hard Rule 5) or reconciliation.
- Adaptive thresholds only — no hardcoded pillar cutoffs.

## 7. REVISED ARCHITECTURE (post `momentum_neural` audit, 2026-06-06)

A deep audit of `app/services/trading/momentum_neural/` (37 files) changed the
build calculus: **the execution engine AND the Ross signal already exist** — the
Ross signal is just discarded before scoring.

- `momentum_neural` is a mature, **crypto-first, 24/7** momentum-automation
  engine: FSM live runner (`live_runner.py:854`, `live_fsm.py`), full safety
  wiring (kill-switch/drawdown/lane-cap/notional-guard via `risk_evaluator.py`),
  Coinbase adapter, decision ledger, and `MomentumSymbolViability` /
  `TradingAutomationSession` structs. Live runner is OFF behind
  `chili_momentum_live_runner_enabled` (`config.py:2744`).
- **Its selection is NOT Ross-shaped:** `score_viability` (`viability.py:78`)
  ranks on regime + microstructure (spread/slip/fee/tape-z) across 10 generic
  families. RVOL/gap/float/catalyst are **absent** from its scoring.
- **The signal exists upstream but is thrown away:** `scanner.py:1480-1515`
  already computes RVOL bands, gap play, micro-float bonus, and news sentiment —
  but the scanner→viability bridge (`trading_scheduler.py:3088-3092`) passes
  **only ticker symbols**, discarding RVOL/gap/float/news before scoring.
- Risk model is ATR-symmetric stops + fixed-notional sizing (`paper_execution.py:86`,
  `portfolio_allocator.py:879-945`), R≈1.4-1.8 — below Ross's 2:1+, and not a
  structure stop / fixed-fractional-risk model.
- **Safety gap:** momentum live positions are NOT covered by the broker-sync /
  bracket-reconciler hardened in PR #435 (`live_runner.py:814` only warns) — same
  bug class as the ETC/SHIB weekend phantom.

**Refined decision:** build a thin Ross **selection + risk** layer ON TOP of the
reused execution+safety substrate. Do NOT fork the runner or gut `score_viability`.

**Reuse unchanged (high value, low risk):** the live FSM + exit/stop/trail/bailout
machinery (`live_runner.py:854`), the safety stack (`risk_evaluator.py:110`),
`MomentumSymbolViability`/`TradingAutomationSession` + `list_momentum_opportunities`
(`opportunities.py:261`), the decision ledger, and the Coinbase venue adapter.

**Build new (the 5 Ross gaps):**
1. **`RossMomentumScorer`** — rank by RVOL (rank, not 1.5× binary), gap/daily-change
   %, float/market-cap tier, catalyst; promote the signal `scanner.py:1480-1515`
   already computes by **un-discarding it at the bridge** (`trading_scheduler.py:3088`).
   Emit Ross viability rows the runner consumes. *(highest leverage — do first)*
2. **Continuation trigger** — wire bull-flag / micro-pullback / new-high
   (resurrect `entry_gates.py:115`) into `WATCHING_LIVE → ENTRY_CANDIDATE`,
   replacing the bare score crossing at `live_runner.py:1116`.
3. **Structure-based stops** — stop under the swing low
   (`entry_gates._compute_confirmed_swing_low_last:24` already exists), replacing
   the symmetric ATR stop.
4. **Fixed-fractional-risk sizing** — `size = risk_budget / (entry − stop)`, 2:1+
   target, replacing fixed-notional `portfolio_allocator.py:921`.
5. **Broker-truth reconciliation** for momentum live sessions — fold into the
   PR #435 broker-sync path so 24/7 fills aren't stranded.

**Revised phases:** M2 `RossMomentumScorer` + un-discard bridge signal → M3 validate
(vs Ross trades + vs CHILI rejections) → M4 trigger + structure stops + fixed-fractional
sizing → M5 broker reconciliation + go-live wiring → M6 live + compare impact to Ross.
