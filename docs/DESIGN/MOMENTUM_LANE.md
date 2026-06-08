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

### 3.5 Session-level risk: daily-loss cap + profit-giveback halt
Two equity-relative session circuit-breakers gate **new arming** for the rest of
the daily window (00:00 UTC reset, the same `date.today()` window
`_daily_realized_pnl` sums). Both are enforced as a **two-layer pattern**: a cheap
early-out in `auto_arm` (Guards 4 + 5) and the authoritative re-enforcement in
`risk_evaluator.evaluate_proposed_momentum_automation` (which `begin_live_arm` /
`confirm_live_arm` honor). Both surface on the Monitor card.

- **Daily-loss cap (downside).** Halts when today's realized PnL falls to
  `-(equity × daily_loss_fraction)` (fallback `chili_momentum_risk_max_daily_loss_usd`).
- **Profit-giveback halt (upside, Ross's rule).** Ross: *"I have a rule that I give
  back 50% of my profits once I reach a certain threshold... easier to remember half
  than 40%"* (warriortrading.com/7-day-trading-rules, confirmed in the 2026-06-07
  research). Once today's **peak** realized PnL (high-water mark, computed live from
  `momentum_automation_outcomes` — no extra state) reaches an **activation threshold**
  AND current realized PnL has fallen to `peak × (1 − giveback_fraction)` or below, the
  lane stops arming for the day (locks in the green day instead of round-tripping it
  back to flat/red). The **single documented knob** is
  `chili_momentum_profit_giveback_fraction` (default `0.5`; `0` disables). The
  activation threshold is **equity-relative with no second magic number** — it reuses
  the equity-relative daily-loss-cap magnitude (a green day worth protecting is, by
  symmetry, one that exceeds the day's max tolerable red). Decision helper:
  `risk_evaluator.evaluate_profit_giveback_halt`. Tunable follow-up flagged to Cowork:
  whether the activation should be the full daily-loss-cap magnitude or a fraction of
  it, once soak shows how often it arms.

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

## 8. Ross RECENT (post-book) entry-quality refinements (2026-06-07)

Three of Ross Cameron's recent, live-practice evolutions — beyond the book rules —
to cut false breakouts and faded-move entries on the `pullback_break` trigger. Each
is a documented, adaptive knob (no magic numbers) and was validated with an OHLCV
dry-run BEFORE the defaults were set (the keystone-fix discipline). Code:
`entry_gates.py` (gates), `live_runner.py` (wiring + fast exit), `config.py` (knobs).

### #1 Break-AND-retest (vs raw first break)
> Ross: *"I almost never buy the first break anymore. Too many wick out and reverse
> instantly. Instead I wait for the break AND the retest."*

`pullback_break_confirmation(require_retest=True)` anchors a STABLE breakout level on
the consolidation that ends `retest_lookback_bars` back (so the level doesn't slide
across the runner's per-tick re-evaluations), then requires, in the tail: a break
above it, a shallow pullback that retests it (dips to ~level within
`retest_tolerance`), the level HOLDING on closes, and the current bar RECLAIMING it.
EMA-9 support is checked at the base (not the current bar) so a strong continuation
doesn't reject a valid retest.

### #2 Breakout-or-bailout fast exit
> Ross (flat-top rule): *"if the stock cannot hold the breakout level after entry,
> exit IMMEDIATELY"* — rather than waiting for the structural stop.

`breakout_failed_to_hold(...)` + a held-position check in `live_runner`: within
`breakout_bailout_max_bars` (× entry-interval) seconds of a `pullback_break` entry,
if the bid falls back below the broken level (minus `breakout_bailout_buffer_pct`),
transition to `BAILOUT` and flatten. The broken pullback HIGH is stashed as
`le["breakout_level_price"]` at the entry-candidate transition. Caps the loss on a
failed breakout well inside the structural pullback-low stop. Guarded so it never
fights the normal stop/target: only with a recorded level (not the momentum_volume
fallback), only while plainly `ENTERED`, only inside the early window.

### #3 Sustaining-volume gate (the ESTR guardrail)
> Ross on his biggest loss (ESTR −$30,942.84): the move had *"almost none of the
> characteristics I look for"* and *"not enough volume to carry it beyond its initial
> surge."*

`pullback_break_confirmation(require_sustained_volume=True)` checks that, at the entry
TICK, recent rel-vol (mean `volume_ratio` over `sustain_lookback_bars`) is still above
`sustained_rvol_floor` — so a faded 24h mover (hot at selection, dead by entry) is
rejected. Self-relative per instrument (rel-vol vs its own trailing average), so the
floor is adaptive (a FLOOR the system can raise), not a fixed share count. Also
tightens the selection↔entry alignment the audit flagged. Fails OPEN on thin data.

### Knobs (all in `config.py`, defaults below)
| Setting | Default | Meaning |
|---|---|---|
| `chili_momentum_pullback_require_retest` | `True` | #1 require break+retest+hold |
| `chili_momentum_pullback_retest_tolerance` | `0.002` | retest/hold band around the level (20 bps) |
| `chili_momentum_pullback_retest_lookback_bars` | `4` | bars reserved for break+retest+reclaim |
| `chili_momentum_pullback_volume_spike_multiple` | `1.5` | rel-vol floor on the trigger bar |
| `chili_momentum_entry_require_sustained_volume` | `True` | #3 reject faded movers at entry |
| `chili_momentum_entry_sustained_rvol_floor` | `1.0` | min mean rel-vol over the sustain window |
| `chili_momentum_entry_sustain_lookback_bars` | `5` | bars averaged for sustained rel-vol |
| `chili_momentum_breakout_bailout_enabled` | `True` | #2 enable the fast bail |
| `chili_momentum_breakout_bailout_max_bars` | `2.0` | fast-bail window in entry-interval bars |
| `chili_momentum_breakout_bailout_buffer_pct` | `0.001` | wick buffer below the level (10 bps) |

### Dry-run validation (`scripts/dryrun-momentum-entry-refinements.py`)
Walk-forward replay over recent crypto OHLCV (10 symbols, 5d), each bar treated as the
live "current" tick; per-fire outcome uses the lane's own risk model (structural stop,
2:1 target, 24-bar horizon). Captured 2026-06-07:

| Variant | 5m win-rate | 5m avg-ret | 1m win-rate | 1m avg-ret |
|---|---|---|---|---|
| baseline (raw) | 29.6% | −0.18% | 28.6% | −0.03% |
| +retest (#1) | 41.7% | −0.05% | 50.9% | +0.13% |
| +sustain (#3) | 25.0% | −0.24% | 31.6% | −0.01% |
| **+both** | **44.1%** | **−0.03%** | **54.3%** | **+0.16%** |

- **#1 retest** lifts win-rate hard on both timeframes (and `+both` is best on every
  metric) — the clearest quality win; defaulted ON.
- **#2 breakout-bailout** on 5m (the live timeframe) cut the fast-bail-eligible losers'
  aggregate loss ~23% (−4.81% → −3.72% over the same 27 fires, triggered on 37%). On
  noisier 1m it was marginally negative (−0.33%, 8% triggered) — the window is short in
  real time and 1m single-bar dips revert; tune `..._buffer_pct` up / `..._max_bars`
  for 1m. Defaulted ON (lane runs 5m).
- **#3 sustaining** is roughly neutral-to-slightly-negative ALONE in a 5-day sample but
  improves the combined config and exists for ESTR-class tail risk (a faded mover that
  won't show up in 5 days of aggregate win-rate). Defaulted ON.

Tests: `tests/test_pullback_break.py` (retest fire / no-retest / failed-hold / raw
unchanged / sustaining block+off / bailout helper). Raw-mode behavior is byte-identical
when the knobs are off, so existing callers are unaffected.

## 9. Asymmetric exit structure (M4 — shipped 2026-06-07)

The single highest-leverage item from the 2026-06-07 Ross research. Ross's edge
(avg winner ≈4.4× avg loser) is the EXIT structure, not win-rate. CHILI's lane
did a **2:1-then-flat** exit — it dumped 100% at the first target (live) or 1/3 at
the 1R-halfway then the rest at target (paper) — capping the upside and forgoing
the tail. Verified Ross rule (warriortrading.com, adversarially confirmed 3-0):

> "I will sell 1/2 when I hit my first profit target … I then adjust my stop to
> my entry price on the balance of my position" — and (micro-pullback) "I usually
> sell 75% of my position into strength and hold the rest for the next breakout
> level. Once partial profits are taken, I move my stop to breakeven."

**Implemented** (live `live_runner.py` + paper `paper_runner.py`, parity-shared):
1. **First-target partial.** At the 2:1 target (`STATE_*_SCALING_OUT`), sell
   `chili_momentum_scale_out_fraction` of the **original** size (default 0.5 =
   "sell 1/2"; the lane learner can raise it). The 2:1 reward:risk for the first
   target is unchanged (verified correct).
2. **Breakeven on the balance.** The runner's stop moves to the entry price
   (derived, no knob; ratchet-only, never loosens).
3. **Hold + trail the runner.** Transition to `STATE_*_TRAILING` and trail the
   stop up via a **chandelier off the high-water mark** at the same ATR distance
   the initial stop used (`atr_pct × stop_atr_mult`, derived from the frozen entry
   ATR — no new magic number). Replaces the old static `entry × trail_floor_return`
   floor that never actually ratcheted. The first-target partial fires from
   ENTERED **or** TRAILING (price can drift past trail-activate before the target),
   guarded by `partial_taken` so it fires once.

**One knob, everything else derived:** `chili_momentum_scale_out_fraction` (the
fraction). Breakeven = entry. Trail = chandelier off the frozen entry ATR. A
position too small to leave a venue-sellable runner falls back to a flat exit at
target (never strands un-sellable dust).

**Parity contract.** The exit math lives in `paper_execution.py`
(`scale_out_fraction`, `breakeven_stop_after_partial`, `scale_out_quantity`,
`runner_trail_stop`) and BOTH runners import the identical functions — backtest
and live take the same structural decision by construction.

**Persistence note.** `_commit_le` / `_commit_pe` now call `flag_modified` on
`risk_snapshot_json`. The scale-out commits twice in one tick around an
intervening event-emit flush; the reassigned snapshot can compare EQUAL to the
flush-pinned baseline (shared nested refs), so SQLAlchemy would silently skip the
second UPDATE and lose the breakeven move. `flag_modified` forces it.

Manifests only once the lane (now in `pullback_break` entry mode) actually enters —
watch the first post-keystone entries for the scale-out → breakeven → runner path.
