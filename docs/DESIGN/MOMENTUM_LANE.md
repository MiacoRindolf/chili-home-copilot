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

**2026-09-10 [21] — opinion sites ARM, the tape exits.** The paragraph above describes
the pre-[21] behaviour. Of the 13 `_transition_to_bailout` call sites none read a print,
and four of the ON ones are *opinions* — this fast-bail (bid vs level on a wall clock),
the lost-VWAP flatten (1m bar + bid), the close-below-structure exit (closed bar) and the
topping-tail runner exit (15-min candle). Once any of them set `BAILOUT` the tick exit
(`momentum_break_stop`, evaluated only in `ENTERED`/`TRAILING`) was never consulted again.
Measured over 7 live days (own exit sweep excluded from the tape, tick exit judged on the
prod 10-s frame): the 15 legs those four ended realised −$521.78; held to
deadman-or-tick-exit they realise −$456.19 (deadman = entry − `sizing.stop_distance`) or
−$336.47 (deadman = the stop actually resting). So the four sites now call
`_arm_opinion_exit` instead: `le["opinion_exit_armed"]` is stamped, a
`live_opinion_exit_armed` receipt carries the inputs the bailout used to carry, and the
session STAYS held — the existing tick-exit verdict (unchanged, no new threshold) or the
deadman stop is the only way out. The tick exit's receipt carries `opinion_exit_armed`
(reason, seconds armed). The USD risk caps (`max_loss_per_trade`, `max_loss_circuit`) are
untouched, and the viability-floor bailout is KEPT: the same measurement says holding its
7 legs is worse (−$50.88 → −$214.89 / −$186.73, 6 of 7 worse).

**2026-09-10 [57] — the close-below-structure (BOS) site is retired, not armed.**

*What was deleted, on which clock.* The live held tick read the **last closed bar** against
the last *confirmed* swing low (`entry_gates._compute_confirmed_swing_low_last`,
`lookback=10` bars on **each** side) with a 30 bps buffer, and since #1377 it ARMED the tick
exit on a close below that level. The frame was
`chili_momentum_pullback_entry_interval`, whose default is **`1m`** (`app/config.py`, flipped
5m→1m deliberately in WAVE-4 ITEM-0) and which **no `.env` on this host pins** — so the live
site read **1-minute** bars and its pivot was confirmed **~10 minutes** after it formed. (An
earlier draft of this note and of the code receipt said "5m / ≥ 50 minutes"; that was **5×
wrong** and is corrected here and at the retired site. It changed no decision, but it is the
number the next reader would inherit.) The paper twin read **15m** bars — the same level,
~2.5 h old by construction.

*What the tail numbers actually measured — a different predicate.* The evidence that opened
the question is a **print-indexed** pivot-low ratchet
(memory `project_shelf_break_is_a_stop_not_a_profit_taker_0909`, full extended July tape):
13 legs with peak ≥ 1 R, actual **+47.03 R → −1.57 R**, 11/13 cut at every **k ∈ 3..50
prints**; VRAX 07-09 **+25.58 R → −0.29 R**; on the body 6 of the 10 best legs cut
(+6.66 R → +3.96 R); 86% of shelf breaks trap/noise (median depth 2.90%, then reclaim).
That rule is **not** the rule that was deleted, on four axes — **(1)** `k` counts *prints*,
not bars (on a 1M-print name, k=50 is seconds; the deleted site was 10×1m bars ≈ 10 minutes
per side); **(2)** the proxy **ratchets** upward, while `_compute_confirmed_swing_low_last`
returns the *latest* confirmed pivot and can step **down**; **(3)** the proxy has **no
buffer**, the deleted site had 30 bps; **(4)** the proxy exits on the **first print** below
the shelf, the deleted site on a **bar close**. The measured harm also *shrinks* as k grows
(−1.57 @k=3, −1.45 @k=8, −1.81 @k=20, **+1.04** @k=50), so extrapolating to a slower,
buffered, bar-close rule runs *against* the measured trend. **The R gap is therefore not
what buys the deletion.**

*What does buy the deletion.* (a) A **bar close is not a print** — on a HELD tick this lane's
answer comes from the tape, and the tick exit (`momentum_break_stop`) already owns that
verdict; (b) the site is **effectively inert** — 1 fire in 28 days live, 0 in 14 days paper;
(c) the **faster analog of the same level destroys the tail**, so this level has no forward
path on the *reward* side. A shelf is a better **STOP**: the level keeps its place on the
**risk** side (the deadman / pullback-low stop) and gets no reward-side exit.

*Honest note — the one direct measurement points the other way.* The site's single live fire
(BIAF 09-04, **−$1.63** actual vs **−$21.84** held) was **right**; over 28 days the deletion
is **−$20.21** on the only decision it ever made. One leg, and it is (a)+(b)+(c) that buy the
change, not the aggregate. Note also that the "**162** ticks held back by the 30-s structure
floor" is *not* 162 near-misses: `_opinion_exit_suppressed` runs **before** the shelf
predicate is evaluated, so that count is the population of held ticks younger than 30 s —
identical (162) to `lost_vwap_flatten` in the same query.

*Deleted:* the live arming block and its per-tick bar fetch, the two
`chili_momentum_bos_exit_*` settings (no dark flag left), the paper lane's direct
`reason="bos"` exit (0 fires in 14 d), `entry_gates.bos_exit_triggered_long` (no caller
left; `_compute_confirmed_swing_low_last` **stays** for the G4 grind clamp and the
micro-pullback ratchet), and `paper_runner`'s now-unused module-level `fetch_ohlcv_df`
import. Three opinion sites arm; the 14-leg aggregate re-states as
−$520.15 → −$434.35 / −$314.63 by exact arithmetic on the pinned per-leg rows. The
**per-exit-mode split is deliberately not re-derived** — subtracting one row cannot recover
it, and the retired row's own exit mode is not known from the pinned data. **Still standing
elsewhere:** the backtest / `exit_evaluator` engine applies the same shelf-break rule and
still defaults it **ON** (`use_bos=True`), so promotion expectancy is still measured under
it; that is a different engine on a different bar clock and needs its own measurement, so it
is flagged, not folded in. Pinned: `tests/test_momentum_bos_exit_live.py`,
`tests/test_opinion_exits_ask_the_tape.py`.

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

## 10. Extended-hours trading window (2026-06-09)

**The gap.** The lane was hard-gated to RTH only — `market_open_now` returned
`570 <= minute_of_day < 960` (9:30–16:00 ET). But Ross's biggest low-float moves are
the **pre-market gap-and-go** (he streams at **7:00am ET**); by 9:30 the runner has
often already made its move and CHILI was arming the faded EOD corpse. Confirmed
against the 06-09 tape: PAVS ($1.55→$26.69→$1.02), CCTG, MTEN, NPT all peaked and
round-tripped around/before the open. The original RTH-only gate was correct **for
Robinhood** (PFOF can't reliably fill low-float pre-market), but the lane now also has
the **Alpaca** rail (limit orders rest on the book in extended hours) and RH itself
exposes an `extended_hours_override` path — so RTH-only is no longer the right ceiling.

**The model** (`market_profile.py`). `market_session_now(symbol)` →
`premarket | regular | afterhours | closed` (crypto → always `regular`, 24/7). The
**regular session (9:30–16:00 ET) is a fixed exchange fact** named once
(`_REGULAR_OPEN_MIN` / `_REGULAR_CLOSE_MIN`); the **only tunable bounds** are two
documented settings — there is NO separate on/off flag, the window itself is the
control:

| setting | default | meaning |
| --- | --- | --- |
| `CHILI_MOMENTUM_PREMARKET_START_ET` | `07:00` | pre-market open (Ross-time). Set `09:30` to disable pre-market. |
| `CHILI_MOMENTUM_AFTERHOURS_END_ET` | `20:00` | after-hours close. Set `16:00` to disable after-hours. |

**Gating.** `is_tradeable_now(symbol)` (= session in premarket/regular/afterhours) is
the gate the **auto-arm** (`auto_arm._symbol_market_open`), **live entry**
(`live_runner` E3), and **opportunities** surface now use. `market_open_now` is kept
as the regular-session label for display honesty.

**Order routing.** Outside-RTH entries are flagged `extended_hours=True` at placement
so the venue routes them instead of rejecting: **Alpaca** → limit + `DAY` tif +
`extended_hours=True` (Alpaca rejects extended GTC); **Robinhood** →
`market_hours_override="all_day_hours"` + `extended_hours_override=True`; **Coinbase**
→ ignored (24/7). Threaded through the `VenueAdapter.place_limit_order_gtc` contract
(`extended_hours: bool = False`). Tests: `tests/test_momentum_market_session.py`.

## 11. Deep-reclaim DIP-BUY (Ross "first reversal off the dip", 2026-06-14)

**The gap.** `_evaluate_deep_reclaim` (§ the EDHL fix) only fired on the **recovery
swing-high break** — price had to reclaim the 9-EMA, hold ≥2 bars, *then* break the
post-dip recovery high. That enters **well off the dip low = a chase** and never fits a
**vertical** reclaim (one bar from the dip to new highs). Ross instead **buys NEAR the
dip on the FIRST candle to tick its own pullback-bar high**, with a stop just under the
dip low. The CUPR 06/12 lesson: a real high-volume gapper shook out a structural stop,
then ran +92% on a vertical reclaim the recovery-high path (correctly) would not chase —
the money was in **buying the dip**, not the confirmed reclaim.

**The fix (additive, fail-open-to-existing).** Inside `_evaluate_deep_reclaim`, AFTER
the collapse guard (`depth ≤ _collapse_cap`, so >25% collapses stay rejected) and
BEFORE the recovery-hold loop, try the pure helper `_dipbuy_signals_ok(...)` →
`FIRE | ARM | PASS`. On `FIRE`/`ARM` it returns `deep_reclaim_dipbuy[_tick]_ok` /
arms `waiting_for_dipbuy_break` at the **dip bar's own pullback high** (NOT the recovery
high); on `PASS` (any decline / thin data / unaffordable runway) it falls through to the
existing recovery-high reclaim **byte-identically**. The 3-signal AND gate (web-research,
adversarially red-teamed) separates a buyable dip from a falling knife:

1. **Rising trend + structure** — VWAP-proxy slope > 0 over `vwap_lookback` bars (the
   20-bar rolling proxy is a *trend-MA* filter, not anchored VWAP), intact higher-high /
   higher-low, the run into the peak held the 9-EMA (first pullback), and clear runway.
2. **Volume dry-up → return** — dip-bar mean volume < `dryup_ratio` × the prior
   trend-push mean (sellers absent), then the trigger bar's volume returns. Heavy dip
   volume = the falling-knife signature → reject.
3. **First reversal new-high** — a strong-close completed bar that ticks the dip bar's
   own high back (FIRE), else arm a tick-watch (the tick path adds a thrust buffer in
   **every** session via `_dipbuy_tick_thrust_ok`, since the dip-bar high is the tightest
   level the lane arms).

**Stop / sizing.** The gate emits ONLY the **dip-low anchor** (`dip_low × (1 − buf)`,
`buf = max(stop_buffer_bps, 0.25·ATR%)`); the authoritative `structural_or_vol_floored_atr_pct`
layer widens it to the vol floor and caps it (INVARIANT A lives there, identical for
live/paper/replay). The **runway affordability** check uses a **measured-move
continuation target** (`run_high + (run_high − dip_low)`) — a dip-buy targets new highs,
and the bare peak would make any near-dip-high entry sub-1:1; it is **depth-independent**
(risk and reward both scale with the dip depth). If even the measured move is < the
class R:R floor → PASS (never tighten the stop to manufacture R:R).

**Knobs** (`config.py`, default ON / one adaptive base + Ross-discipline floors):
`chili_momentum_deep_reclaim_dipbuy_enabled` (kill-switch → byte-identical to today),
`…_vwap_lookback` (12, THE base), `…_dryup_ratio` (0.85), `…_pullback_bars` (3),
`…_stop_buffer_bps` (10). Class-aware (crypto inherits the 24/7 reclaim window + crypto
R:R); only helps the **buyable-depth class** (≤25% dips). Tests:
`tests/test_dipbuy_deep_reclaim.py`.

## 12. Ross TOP-GAINER CONCENTRATION (2026-08-23)

**Doctrine (Ross 08-20 recap, his own post-mortem):** momentum setups work ONLY on the
day's top-2/3 leading gainers; on dispersed-attention names they lose. Evidence: the
08-21 morning logs armed rank #11/#16 names on bare rel_vol (SDOT/COIW — both losers)
while every profit in the time-of-day audit came from premarket A-names.

**Mechanism.** Outside PREMARKET (the measured profitable window, deliberately exempt),
a **NEW equity live arm** must be one of the day's **top-N market-wide %-gainers**
(`chili_momentum_top_gainer_concentration_n`, default 3, THE one knob; 0 = kill-switch).
Enforced at the live-pick stage (`_live_armable`) and mirrored in the rank-displacement
newcomer walk (never evict a watcher for a newcomer the arm stage would then block).
Membership sources, all zero-new-network, in order: (1) the full-market snapshot already
fetched for Ross-universe proof, ranked by change-pct; (2) the candidate board's own
per-row scanner signals aggregated across rows (persistence subsets `ross_signals` to
each row's own symbol — a single row's dict must never rank alone); (3) the persisted
batch-level `top_market_gainers` membership meta (top-5). Unknown data ⇒ **fail-open**.

**Concentration, NOT starvation** (CLRO lesson 2026-07-02, #1036): the board TOP-2
(hoisted symbol-of-day leader + the armed-first it displaced — the same primitive as the
cooldown exemptions) always pass; and a **structural bypass** admits a name whose OWN
persisted Ross/5-Pillars evidence clears the tick-scalp shape gate AND whose viability is
at/above the board's within-pass p90 (the A1 adaptive-percentile primitive — no second
magic number). Bare rel_vol / high score without shape evidence never bypasses — that is
exactly the churn class being removed. Paper shadow arms, exits, stops, and open-position
management are untouched by construction.

**Decision-level A/B (7d of live arms, 08-17→08-21, read-only):** all 1,093 live equity
arms that week were `cancelled_pre_entry` churn (zero fills lost by blocking). Outside
premarket, even the most conservative membership bound (batch top-5) hard-blocks 109/693
arms (16%) with another 255 gated behind the structural-p90 bypass; exactly one batch-#1
leader proxy would have been membership-blocked and the TOP-2 exemption passes it (zero
leader starvation). Live full passes use the snapshot source at true N=3 — stricter.
Tests: `tests/test_top_gainer_concentration.py`.

## 13. RE-ENTRY RAMP — the bar is the previous leg's HIGH PRINT, at EVERY level ([59], 2026-09-10)

**Doctrine (operator, repeated):** sell the pullback, then buy again *when the level is
reclaimed* — "print sa itaas ng high ng nakaraang leg na may `signed_tape_accel > 0`".
Cooldowns are human concepts; the machine waits for a tape condition.

**Mechanism.** One helper, two doors (`live_runner._g4_reentry_escalation_check`, called by
the trigger path and by the momentum-continuation fire), one pure decision
(`risk_policy.reentry_escalation_decision`). Whenever the name has a prior leg on the
symbol-day (`le["g4_prior_trade"]`, written on every confirmed exit including green, seeded
into a new session by `same_day_escalation_seed` **without** needing a level > 0):

| level | reference | price compared | margin | tape hold |
|---|---|---|---|---|
| 0 (after a GREEN leg / profit decay) | prior leg's HIGH PRINT (`entry_gates.prior_leg_high_print`; HWM / exit fallbacks, named) | the tape's **last PRINT** (`last_print` from `signed_tape_accel_features(window_prints=255)`; `tick.ask` a named fallback, `price_kind`) | **0 R, `>=`** — the same comparison every other rung makes, so the rung after a GREEN banked round is the LOOSEST one (`reclaim_form=level0_new_high_print`) | `signed_tape_accel > 0` **and** `buy_share_delta > 0` (print-count halves); unreadable ⇒ skipped |
| ≥ 1 (after a RED leg) | same | same | `(level−1)·R` of the failed leg, `>=` (unchanged, #1376) | same (+ structural class / substitute, leader ignition bypass — unchanged) |

A pass at level 0 is `reclaim_met_level0`. Refusal is a **WAIT** (`reclaim_of_prior_leg_high_wait` at level 0, `reclaim_not_met` /
`tape_not_confirming` at level ≥ 1) re-checked every tick with the receipt
`g4_reentry_escalation_blocked`; a pass writes `g4_reentry_reclaim_proven` (reference,
reference_kind, print, price_kind, accel, buy_share_delta, prints_since_high, window,
`spread_bps` from the L1 the print printed against, and the `binding` block: window_prints
255 = p50 of the 15-s print count at 108 live decision instants; margin_r; reclaim_form;
spread policy). The anti-chase cap stays `was_loss`-only. No new knob, no cooldown.

### 13.1 Review fixes (2026-09-10, same PR)

Seven defects were found reviewing the first form of this bar and are fixed here.

* **The deciding print must be ALIVE.** The tape window is bounded by COUNT (`LIMIT 255`),
  not by time, and the halt-gap trim only inspects gaps *inside* the window, so the
  TRAILING gap (halted right now, or a stopped bridge) is invisible and `last_print` can be
  arbitrarily old — a ten-minute-dead burst satisfies BOTH halves of the bar on data the
  market no longer offers. `_signed_tape_features` now reports `gap_p99_s` (the window's own
  inter-print gap p99) alongside `last_ts`, and the helper refuses
  `reentry_tape_source_stale` when the newest print is older than
  `max(gap_p99_s, chili_momentum_g4_reentry_max_print_age_seconds = 14.69 s)`. The floor is
  the p99 of 96,360 inter-print gaps over the 8 names we traded on 2026-09-10 13:30–20:00Z
  (p50 0.004 / p90 1.329 / p99 14.693 / p99.9 92.489 / max 686.59 s). Precedent in-tree:
  `first_dip_tape_source_stale`. An UNREADABLE tape stays fail-open. `price_age_s` and
  `price_age_bound_s` are on every receipt.
* **The level-0 bar is a WAIT, not a day-long lockout.** Level ≥ 1 has four release valves
  (profit-recycle decay, green-banked reset, the non-structural substitute, the day-leader
  ignition bypass); the level-0 branch had none, the reference is only ever overwritten by
  the NEXT confirmed exit — which the bar itself prevents — and `same_day_escalation_seed`
  carries it into every later session of the ET day. The release is a TAPE condition: the
  market gets as many prints since the leg's exit as the leg itself consumed
  (`level0_bar_prints_budget = prior_leg_high_print_n`, counted with a bounded
  `OFFSET/LIMIT` probe, `entry_gates.prints_since_exceeds`) ⇒
  `level0_bar_expired_new_tape`. Per-name, per-leg, print-indexed, no new constant, no
  clock. Fail-CLOSED: an unreadable probe keeps the bar.
* **Equality no longer inverts the ramp.** Strict `>` at level 0 made the rung after a
  GREEN leg STRICTER than level 1 (margin 0, `>=`): the same name at the same reference was
  refused after winning and allowed after losing. Level 0 now uses `>=`.
* **`reclaim_proven` decides the pass receipt, not a reason STRING.**
  `leader_ignition_bypass` is set in the branch where the price is BELOW `required`, and the
  step-3 `tape_majority_buy_confirms` overwrite could erase `no_reclaim_reference` — both
  were being written to the book as `g4_reentry_reclaim_proven`. A pass that skipped or
  bypassed the bar now emits `g4_reentry_pass_unproven`, and both receipts are deduped by
  the deciding values (level / reason / price / reference) instead of firing on every tick
  from both doors.
* **The cached high print must be SEALED.** `iqfeed_trade_ticks` is written after the fact
  (SKYQ 2026-09-10 13:40–14:10 `available_at − observed_at` p50 0.27 s / p95 0.64 s / max
  4.04 s; TNON p99 3.75 s / max 6.49 s) and the bridge has a documented silent-hang, while
  the first post-exit trigger arrives at p10 7.76 s / p25 10.57 s — so the first read can
  return a partial max, and the first form cached it for the whole session and reported it
  as the binding reference. `prior_leg_high_print` now returns `(high, n, sealed)`; the
  cache is written only once a print NEWER than the exit exists, otherwise the read repeats
  and self-heals as it did before the cache.
* **The same-day seed names the session the bar's height came from**
  (`prior_trade_session_id`; `source_session_id` falls back to it), `prior_trade_seeded`
  keeps its original meaning ("a prior trade was found") and the new
  `prior_trade_applied` / `prior_trade_reference_only` carry the rest. A reference-only seed
  is tagged `seeded_reference_only` and the anti-chase cap ignores those stashes, so the
  cap's firing population is genuinely unchanged rather than merely its code.
* **Crypto does not inherit the bar.** A `-USD` name has no `iqfeed_trade_ticks`, so at
  level 0 the bar would degenerate to `tick.ask` vs the quote-mid HWM — a refusal with zero
  tape proof, resting on exactly the quote-mid opinion this section replaces. Level 0 on a
  `-USD` symbol short-circuits to `no_escalation_crypto_no_tape` before any read.

The receipts carry the binding VALUES; the derivation sentences live here and are
referenced by `binding.derivations` instead of being embedded in every emitted row.

**Measured (14 d live to 2026-09-10, read-only).** The bar at the 45 live re-entry instants:
prior=GREEN 15 legs = −$105.23, refused all 15 (12 no_reclaim −$86.41, 3 tape_neg −$18.82),
allowed 0; prior=RED 30 = −$745.53, allowed 1. **The automatic re-buy is refuted at L1**
(`h59_reclaim_spread_cost`, 78 legs, IQFeed L1 attached to every print — 100 % coverage,
TNON 09-10 13:00–13:30 = 57,630/57,630): [59]-form re-entry at the ASK of the reclaim print,
exit at the BID of the next G-all trigger, n=58: print +$49.91 → **L1 −$336.72**; spread at
reclaim p25 27.6 / **p50 52.1** / p75 82.3 / p90 130.2 bps; no ex-ante split (spread bucket,
buy_share_delta, accel, timing) is positive at L1. Hence the reclaim is a correct **refusal**
inside the normal entry path (viability + trigger + ramp + chase cap), never a mechanical
re-buy; `spread_bps` is reported, not enforced. Tests:
`tests/test_reentry_bar_level0_prior_leg_high.py`, `tests/test_reentry_bar_is_the_tape.py`,
`tests/test_continuation_fire_cannot_bypass_g4.py`, `tests/test_g4_same_day_seed.py`.

## 14. MICRO-PULLBACK RE-LOAD — depth is evidence, the proof is a print ([1], 2026-09-10)

<a id="micro-pullback-reload-proof"></a>

**Doctrine (operator, repeated for weeks):** *ibenta ang spike, bumalik sa pullback.* The
micro-pullback re-load is that doctrine in code. It has **never filled, not once**: across
the whole book there are 0 `live_micro_pullback_reentry_submitted` / `_fill` rows.

### 14.1 What was actually holding it shut

Two gates, and **both measured inverted** against our own tape.

| gate | where | what it refused | measured |
|---|---|---|---|
| `dip_pct > max_dip_pct` → `dip_too_deep` | `entry_gates.micro_pullback_reentry_detect` | a pullback deeper than 4 % of the bounce-high | dip depth at the onset of a clean run is **deeper** than at random controls at *every* quantile — onset p50 0.0210 / p90 0.0425 / p95 0.0540 vs ctrl p50 0.0118 / p90 0.0260; pooled AUC 0.733, **clustered AUC 0.710** (37 symbol-day clusters). Cap 0.04 refuses **12.3 % of onsets vs 2.2 % of controls = 5.6×**. No cap level is selective (0.02: 52.9/20.2; 0.03: 27.6/6.3; 0.06: 3.7/0.4; 0.10: 0.4/0.0). |
| `ofi >= 0.30 AND trade_flow >= 0.20` → `reason=flow` | `live_runner` re-load block | every re-load whose book/tape was not "turning up" | OFI at onset is **lower** than at controls — onset p25 −0.629 / p50 −0.2226 / p75 +0.118 vs ctrl p50 +0.0047; pooled AUC 0.370, **clustered AUC 0.400**. The +0.30 floor refuses **82.0 % of onsets vs 74.9 % of controls**; every floor tried is anti-selective (+0.10: 74.4/59.6; 0.00: 65.4/48.7; −0.30: 44.6/22.7; −0.60: 26.8/9.1). |

Sources: `retracement_at_onset.csv` (832 onset / 15,916 control / 38 clusters, print-indexed)
and `ofi_at_onset.csv` (Cont/Kukanov/Stoikov L1 over `iqfeed_depth_snapshots`, 956 onset /
16,524 control / 53 clusters), both bounded read-only on `chili`, 2026-09-09.

**The knife never fired.** All-time `reason=flow` blocks = 18, **`veto=true` in zero of
them** (ofi p50 −0.0074, trade_flow p50 −0.1629). `_entry_flow_veto` — the real
never-buy-into-selling knife — has never once been the refuser here. The positive-confirm
was 100 % of the blocker, and the `trade_flow` floor's own config description called itself
"a guessed constant — calibrate in replay before any live reliance". It never was.

The depth cap is not re-load-only: `micro_pullback_primary_confirmation` reuses the same
detector, so `dip_too_deep` was refusing the **primary** micro-pullback entry too (an open
path, flag default on).

### 14.2 What it is now

**Depth is REPORTED.** The detector returns `dip_pct`, `dip_pct_onset_pctl` (position in the
onset distribution above, piecewise-linear over an embedded 8-point quantile table) and
`would_have_blocked_at` / `would_have_blocked` — the old 0.04 as a **named fallback on the
receipt**, not a refusal. The structural knives are untouched: `dip_below_shelf` (the
ratcheting higher-low — the one real knife on depth), `ema_not_rising`, `no_dip_after_high`,
`last_bar_undercut_dip`, `frame_too_sparse`. `max_dip_pct` stays a keyword-only parameter so
every caller's signature is byte-identical; it now feeds only the receipt.

**The proof is a print** — the same form as §13, and (after the 2026-09-10 review) a
print on **both sides of the comparison**:

```
reclaim_high_px > break_ref_px    the tape printed ABOVE the micro-break AFTER the dip
signed_tape_accel > 0             the signed push is still running
```

* `break_ref_px` = the **highest PRINT inside the micro-break bar** (`high_print_in_window`
  over that bar's own bucket). It is *not* `bounce_high`. `bounce_high` comes from
  `_build_micro_bar_df`, which buckets **NBBO midpoints** (`micro_bars._row_ts_mid`), so
  comparing a print to it mixes two price bases — the exact defect §13 was written to remove
  ("ang PINAKAMAHINANG anyo ng bar … isang opinyon"). When the bar's prints cannot be read
  the reference falls back to `bounce_high` and the receipt says so in `break_ref_kind`
  (`break_bar_high_print` | `quote_mid_micro_bar`) — a **named** fallback, never silent.
* `reclaim_high_px` = the highest print **since that bar ended**, up to the as-of frontier.
  Not the last tick (the side of one tick is noise, not mechanism) and not the window's own
  `window_high_px` — that window contains the break itself, so the comparison would be
  almost always true. Measured: at SUNE 09-09 09:31:14 the "window high 3.02 above
  `bounce_high` 3.01" **is the break bar's own print** (119 prints in `[09:31:00, 09:31:10)`),
  made before the dip.

The tape window is print-indexed (`signed_tape_accel_features(window_prints=255)`), reusing
`chili_momentum_g4_reentry_tape_window_prints` — a derived value, not a new literal.

**The window must not be a clock *anywhere* in the read.** In `window_prints` mode the
halt-gap restriction inside `_signed_tape_features` used to drop everything before the last
inter-print gap larger than `chili_momentum_l2_confirm_window_s / 2 = 7.5 s` — a wall clock
inside a print-indexed path. Measured on `chili`, 2026-09-10 11:00–13:30Z: gaps > 7.5 s =
SUNE **86 of 4,418** (max 635 s), TPET 18 of 42,196, SKYQ 6 of 16,771 — at SUNE's rate a
255-print window is essentially always truncated, and a truncated window returns `None` (⇒
`tape_unreadable` ⇒ permanent WAIT) or an accel computed from 3 prints. In print mode the
threshold is now **half the span actually read** — the same rule on the tape's own clock.
Measured at the four real detections that half-span is 1.31 s (SKYQ, 255 prints in 2.62 s)
to 46.01 s (SUNE, 92.02 s): stricter on a fast tape, looser on a slow one. Ordinary cadence
cannot trigger it — a gap must exceed ~127× the window's mean gap. The receipt carries
`gap_split_s`, `gap_restricted` and the **effective** `n_ticks` in `binding`, so a truncated
window can never again be reported as the 255 that were requested.

**Print age is load-bearing here, on both tape call sites.** The window is bounded by
*count*, so it has **no lower time bound at all** (`WHERE symbol = :s AND observed_at <=
:as_of ORDER BY observed_at DESC LIMIT :n`) and the trailing gap is invisible — and **37
names have no real-time NYSE entitlement**. Measured 2026-09-10 13:20–14:00Z on live `chili`:
TPET `received_at − observed_at` **p50 900.23 s** (min 899.95, max 900.73, n = 21,560) —
exactly 15 minutes — against SKYQ p50 **0.068 s**. A "reclaim" proven by a 15-minute-old
print is not proof, it is history.

Bound = `max(chili_momentum_g4_reentry_max_print_age_seconds = 14.69, the window's own
gap_p99)` — reused, not invented. **Derivation honesty:** under the old 7.5 s halt rule that
`max()` was *inert by construction* (the restriction removed every gap above 7.5 s, so
`gap_p99 <= 7.5 < 14.69` always). With the half-span rule it can genuinely exceed the floor on
a slow tape; measured at the four real instants it does not (gap_p99 0.08 / 0.08 / 0.09 /
4.62 s), so the **floor** binds there. Both numbers are on the receipt.

**Unknown age counts as OLD.** `tape_stale` is `None` when there is no `last_ts` or the age
cannot be computed; the ladder tests `tape_stale is not False`, so an unknown age WAITS. The
earlier `is True` form let an un-aged print satisfy the proof while `print_age_s` — the one
field that would have shown it — was emitted blank.

| outcome | receipt |
|---|---|
| pass | `live_micro_pullback_reentry_proof` |
| `_entry_flow_veto` tripped | `live_micro_pullback_reentry_blocked reason=flow_veto` |
| no readable / stale / un-aged print, or no accel | `… reason=tape_unreadable` |
| the break bar's reference price cannot be read at all | `… reason=break_reference_unreadable` |
| the tape has not printed above the reference since the dip | `… reason=reclaim_wait` |
| cleared, but accel <= 0 | `… reason=tape_not_confirming` |

The ladder is the pure function `entry_gates.micro_pullback_reload_proof` — not an inline
branch chain — so its order and its boundaries are executable from a test rather than
transcribed into one.

Every row carries `bounce_high`, `break_ref_px`, `break_ref_kind`, `break_ref_n_prints`,
`reclaim_high_px`, `reclaim_n_prints`, `last_print`, `price_kind`, `signed_tape_accel`,
`buy_share_delta`, `n_ticks`, `gap_restricted`, `tape_window_high`, `print_age_s`,
`print_age_bound_s`, `tape_stale`, the **reported** `ofi` / `trade_flow`, and a `binding`
block. An unreadable tape is a **WAIT** (fail-closed — an extra BUY needs proof).

**The midday lull reports, it does not refuse.** `in_midday_lull` is a pure 10:30–14:30 ET
wall-clock band and it used to refuse this block *before* the tape was read, emitting
`{"reason": "midday_lull"}` with no value at all (10 of 579 all-time re-load blocks). A clock
in front of a print proof is the same defect as the floors it replaces, so it is now carried
on the receipt (`midday_lull`, `midday_lull_band`, `midday_lull_policy=reported_not_enforced`)
and the print proof does the refusing — during a genuine lull the tape does not clear the
micro-break, and `reclaim_wait` says so with numbers.

### 14.3 Honest limit

After this change the re-load reaches
`live_runner.py builder_missing_capture_binding` (**[30]**) and is refused **there**, with a
receipt. [30] — every add path is explicitly unavailable on the paper Alpaca lane until a
CID-bound packet exists — is an operator decision and is not opened here. So this change's
immediate live effect is on (a) the **primary** micro-pullback entry, which is open, and
(b) receipt honesty on the re-load. Add-side fills still depend on [30].

**Depth on the primary path now reaches the sizing machinery.** `micro_pullback_primary` and
`micro_pullback_primary_tick_ok` emit `pullback_low` (= `dip_low`) and `pullback_high` under
the standard debug keys but were **never** in `STRUCTURAL_TRIGGER_REASONS`, so the structural
stash ran `le.pop("structural_stop_price")` on every fire and the placed stop fell back to
the vol-floored ATR stop — depth-blind, and `entry_stop_atr_pct` sized it depth-blind too.
With the free-standing 0.04 cap gone, depth *must* reach the machinery that prices it: both
reasons are now in the tuple, so a deeper dip widens the stop and therefore shrinks the size.
That is the "mechanism, not binary" form of the cap that went away.

### 14.4 What the four replayable detections say

**Coverage, stated honestly:** the live book holds **18** `live_micro_pullback_detected`
rows, not four — RKTO session 12050 x8 (2026-07-09 13:42–13:43), JZXN session 12663 x6
(2026-07-10 13:46–14:19), SUNE 20774 x1, SKYQ 21591 x3. Only **4 of 18 (22 %, two
symbol-days)** can be replayed at all: `iqfeed_trade_ticks` holds **0 rows** for RKTO
2026-07-09 13:30–13:50 and **0 rows** for JZXN 2026-07-10 13:40–14:25, so there is no tape to
run the proof against. Worth recording anyway: all 14 omitted detections carry `dip_pct`
between 2.2 % and 3.3 % (from their `bounce_high` / `dip_low` payloads) — i.e. **under** the
0.04 cap, so depth was not what refused them either.

At the four replayable instants the new proof also refuses — as `reclaim_wait`, and now
with a print on both sides (all values measured read-only on `chili`; the micro-break bar is
the 10 s bucket whose max mid equals the recorded `bounce_high`, which reproduces exactly in
all four cases):

| instant | `bounce_high` (mid) | break bar | `break_ref_px` (high PRINT) | `reclaim_high_px` since the bar | verdict |
|---|---|---|---|---|---|
| SUNE 20774 09-09 09:31:14 | 3.01 | `[09:31:00, 09:31:10)` | **3.02** (119 prints) | 3.00 (23) | `reclaim_wait` |
| SKYQ 21591 09-10 13:52:17 | 3.715 | `[13:52:00, 13:52:10)` | **3.72** (1,028) | 3.69 (735) | `reclaim_wait` |
| SKYQ 21591 09-10 13:52:22 | 3.715 | same | **3.72** | 3.69 (988) | `reclaim_wait` |
| SKYQ 21591 09-10 13:52:30 | 3.715 | same | **3.72** | 3.71 (1,573) | `reclaim_wait` |

This corrects the first version of this section, which read `window_high_px` as evidence that
"the tape paid up through the micro-break": it did not — the 3.02 / 3.72 prints **are** the
break, made before the dip.

**The machinery can fire.** At every one of the four instants the tape printed above
`break_ref_px` within seconds of the detection — SUNE 3.0205 at 09:31:39 (**+25.0 s**), SKYQ
3.7277 at 13:52:33 (**+15.5 s / +10.2 s / +2.4 s** from the three detections). The verdict is
WAIT-then-buy-the-break, not a disguised permanent refusal, and the three-minute high after
each detection (SUNE 3.05, SKYQ 3.80) is where the doctrine was pointing.

### 14.5 The sibling pullback-add read

The same commit moved `pullback_add_decision`'s front-side tape read to the 255-print window.
That read feeds **two** live gates — `buy_share_delta > 0` (`weak_front_side`) and
`high_print_position >= 0.75` (`dip_into_a_spent_move`) — and on a delayed name the count
window reaches straight past the 15-minute delay. Measured at the six TPET 21589
`weak_front_side` instants (2026-09-10 13:27:13 → 13:28:05), counting only rows **visible at
the decision instant** (`received_at <= T`):

| window | rows returned | newest print age | `high_print_position` |
|---|---|---|---|
| 15 s (before) | **0** at all six | — | `None` ⇒ basis `score` ⇒ fail-closed refusal |
| 255 prints (after, unbounded) | **255** at all six | 900.1 / 900.2 / 900.2 / 901.0 / 903.6 / 900.5 s | 0.906 / 1.000 / 0.972 / 0.996 / 0.992 / 0.972 |

Every one of those `high_print_position` values sits at or above the 0.75 `spent_position`
quartile, so the window switch alone would have turned a fail-closed `weak_front_side` into
a `dip_into_a_spent_move` **decided on 900-second-old prints**. The same age bound as the
re-load is therefore applied at this call site: stale (or un-aged) ⇒ the tape features are
dropped ⇒ the documented `front_side_strength` score fallback decides, exactly as before.

`tape_unreadable` on `live_pullback_add_vetoed` is derived from the **print age**, not from
`front_side_basis == "score"`. The basis proxy was invalidated by the window change in the
same commit: with a count-bounded window the delayed name's read is no longer empty, so the
basis becomes `"tape"` and the flag would have emitted `false` at precisely the six instants
offered as proof that the tape was unreadable. The receipt now carries `tape_print_age_s`,
`tape_print_age_bound_s`, `tape_stale`, `tape_n_ticks_effective`, `tape_gap_restricted`,
`tape_window_prints`, and the `midday_lull` band that refuses 8 of the 29 rows on this path
since 2026-09-09.

Tests: `tests/test_dip_gates_report_not_refuse.py`,
`tests/test_momentum_micro_pullback_reentry.py`,
`tests/test_micropullback_clock_is_measurement.py`.
