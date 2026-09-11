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

## 14. Micro-pullback depth, print proof and observed-dip sizing ([1], 2026-09-11)

<a id="micro-pullback-reload-proof"></a>

The retired depth cap and positive-flow floors were anti-selective in the recorded
onset population. Depth is reported; the reload requires a trade-print reclaim.
The primary entry prices the observed dip through its stop distance. No new flag
or fitted threshold controls either change.

### 14.1 Evidence and its limits

The original measurements were bounded reads of `chili`, 2026-09-09/10:

| Quantity | Measurement | Population/source |
|---|---|---|
| Dip depth | onset p50 0.02099, p90 0.04252, p95 0.05403; control p50 0.0118 | `retracement_at_onset.csv`, 832 onset / 15,916 control / 38 symbol-day clusters |
| Depth cap 0.04 | refuses 12.3% onset / 2.2% control; clustered AUC 0.710 on 37 paired clusters | Same print-indexed population |
| OFI floor +0.30 | refuses 82.0% onset / 74.9% control; onset p50 -0.2226, control +0.0047 | `ofi_at_onset.csv`, 956 onset / 16,524 control / 53 clusters; pooled AUC 0.370, clustered estimates 0.386-0.400 depending on paired-cluster subset |
| Reload flow refusals | 18 all-time; veto=true in zero; OFI p50 -0.0074, trade_flow p50 -0.1629 | `live_micro_pullback_reentry_blocked`, reason=flow |
| Reload fills | 0 observed in the audited history | submitted/fill event inventory |
| Provider delay | TPET available-minus-event p50 900.44s; SKYQ/SUNE 0.27s | 2026-09-10 13:20-14:00Z |

These are detection/flow measurements, not a primary-entry P/L experiment or a
proof that newly admitted dips are profitable. The primary shelf is derived
from overlapping bars, so it is **not** claimed as an independent deep-dip knife.
The reload's persisted, ratcheted shelf remains a separate structural condition.

There are **18** historical detected rows, but only **4** have replayable tape:
RKTO session12050 x8 and JZXN12663 x6 have 0 rows in the corresponding historical
windows. Their recorded dips were 2.2-3.3%, below the old cap. The available four
rows cover only SUNE and SKYQ:

| Detection | Quote-mid high (reported only) | Break-bar high print | Highest print after break | Historical ladder result |
|---|---|---|---|---|
| SUNE20774 09-09 09:31:14 | 3.01 | 3.02 in [09:31:00,09:31:10) | 3.00 | reclaim_wait |
| SKYQ21591 09-10 13:52:17 | 3.715 | 3.72 in [13:52:00,13:52:10) | 3.69 | reclaim_wait |
| SKYQ21591 09-10 13:52:22 | 3.715 | 3.72 | 3.69 | reclaim_wait |
| SKYQ21591 09-10 13:52:30 | 3.715 | 3.72 | 3.71 | reclaim_wait |

These pins test the ladder using measured prices. They do not certify exact
historical commit visibility or substitute for a sealed ReplayV3 prefix. Any
re-run must separately establish publication eligibility at its decision frontier.

### 14.2 Reload decision and receipt

`micro_pullback_print_evidence` reads both sides at one as-of frontier. The
break reference is the highest trade print in the detector's completed break
interval; reclaim evidence is the highest print since that interval. An arrived
print at/after the interval's end provides the same provisional sealing convention
used by [59]. Missing or unsealed break data is re-read on the next tick and
produces `break_reference_unreadable`; there is no quote-mid fallback.

The SQL read requires event time <= as-of and **both** received_at and available_at
known and <= the UTC publication frontier. This is named
`conservative_received_and_available_as_of`, not an exact commit watermark.
`break_ref_observed_px`, `break_ref_sealed`, kind and counts expose incomplete reads.

The executable ladder preserves this order:

1. `_entry_flow_veto` -> `flow_veto`.
2. Missing, nonfinite or stale tape -> `tape_unreadable`.
3. Missing/nonfinite break reference -> `break_reference_unreadable`.
4. Reclaim high not strictly above break reference -> `reclaim_wait`.
5. Nonpositive acceleration -> `tape_not_confirming`.
6. Otherwise -> `proof`, followed by the existing admission and in-flight guards.

The binding block carries requested prints, effective prints, gap trimming,
print age and bound, both reference prices, observed depth, its onset percentile,
the retired depth threshold and the retired flow thresholds/verdict. Retired flow
values preserve the old caller's effective `float(raw or default)` semantics,
including its zero-to-default behavior. These values only report the old rule.
The midpoint and midday lull remain telemetry; the lull no longer refuses reload.

| Binding | Value/policy | Derivation |
|---|---|---|
| Depth onset quantiles | p05 .00609, p10 .00886, p25 .01341, p50 .02099, p75 .03148, p90 .04252, p95 .05403, p99 .07867 | Original onset distribution; percentile is descriptive and saturates at the measured tail |
| Retired cap / flow defaults | .04 / .30 / .20, report only | Original config values and measurements above |
| Requested tape prints | 255 at current default | Existing [59] p50 15s print count at108 decision instants |
| Halt-gap trim | existing window_s/2, 7.5s at current default | Named legacy continuity policy; unchanged for [58], [59] and other consumers |
| Age bound | max(14.69, surviving-window gap_p99) | Existing [59] measured floor; with the default7.5s trim,14.69 binds |

Half-total-span gap rebasing was rejected in review: it retained multiple halts
and changed the existing [58] exit/[59] entry. This change restores their prior
continuity semantics and reports the effective sample. Task[29] owns the broader
print-window redesign. No adaptive-freshness benefit is claimed from this max().

### 14.3 Primary stop policy and risk

Only `micro_pullback_primary` and `micro_pullback_primary_tick_ok` use the observed
dip stop policy. They remain outside `STRUCTURAL_TRIGGER_REASONS`: starter sizing,
G4 reclaim, backside unbench and chase bypass semantics retain their previous
behavior. The stop is stashed separately; no breakout-level exit is enabled.

For those two reasons, a finite positive dip low below entry reaches
`structural_or_vol_floored_atr_pct` without the generic0.15 ATR cap. Existing
volatility/noise floors still apply. At a deep dip the risk distance is therefore
entry minus observed low, and the unchanged dollar budget buys fewer shares as
the dip deepens. Invalid primary stop inputs raise before admission; they do not
silently fall back to a depth-blind stop. Other triggers retain the generic cap.

The legacy runner and ReplayV2 pass the actual trigger reason. The DB-paper
producer and final runner recompute pass the captured gate reason, so the sealed
source and final executable stop agree. Captured Alpaca already binds the raw
candidate stop to its economic evidence and resolves quantity from that exact
stop; its immutable contract is unchanged. Regression tests exercise both the
captured factory and the complete DB-paper admission path, in addition to the
legacy stop/quantity functions and the unchanged starter multiplier.

### 14.4 Remaining scope

No successful reload fill or primary profitability result is claimed. Task[30]
owns add-order admission; its current branch must be checked rather than relying
on the older `builder_missing_capture_binding` explanation in archived notes.
The existing primary geometry still uses its supplied bars; converting detector
geometry is separate from this print-proof and stop-pricing change. The sibling
pullback-add path retains its existing guards and gains print-count, age and
basis receipts; stale/unknown tape falls to its existing named score fallback.

## 15. The re-entry chase gate is the TAPE, not the LEVEL ([46], 2026-09-11)
<a id="46-reentry-chase-is-the-tape"></a>

### 15.1 What the old gate asked

After a losing exit on a name, `live_runner` blocked any re-entry priced more than
`chili_momentum_reentry_chase_cap_r` (1.5) ATR above the prior losing tranche's
high-water mark. That is a question about a LEVEL — a line on a chart — not about
whether anyone is buying. It also carried a leader-ignition bypass
(`chili_momentum_chase_cap_leader_bypass_enabled`) intended to let a genuine new leg of
the day leader through.

### 15.2 What was measured (live `chili`, read-only, bounded)

Population: 177 `momentum_reentry_chase_blocked` rows, 2026-08-30 → 09-10, forming
**11 episodes** (TNON 77, PCLA 46, AHMA 21, SLE 7, BIAF 7, MIMI 7, BIAF 5, DLTH 4,
LIDR 1, WYHG 1, TPET 1). The CLUSTER is the unit of evidence, not the 77 TNON rows.

1. **The level itself has no edge.** Extension above the anchor in ATR units overlaps
   between episodes that went up and episodes that went down (p50 4.83 vs 6.07, p10 1.82
   vs 1.87, p90 7.01 vs 8.10). 1.5 is a symptom, not a measurement.
2. **The "ATR" is not an ATR.** `SELECT DISTINCT round(risk_unit_atr/prior_anchor_hwm*100,4)`
   over those 177 rows returns **one** value: `1.5000`. No blocked session's
   `regime_snapshot` carried an `atr_pct`, so `paper_execution.regime_atr_pct()` returned
   its hardcoded `0.015` every time. The "1.5R ceiling" was a fixed **+2.25 %** above the
   anchor — identical for $1.04 MIMI and $11.48 BIAF — and it was silent.
3. **The tape is readable at every block.** `signed_tape_accel_features(window_prints=255)`
   resolved at 177/177 block instants. Tape+ (`signed_tape_accel > 0` AND
   `buy_share_delta > 0`) held at 49/177 = 27.7 % of instants, and at episode level it
   splits the door correctly: it refuses DLTH / WYHG / SLE (zero tape+ instants, all three
   fell) and admits both genuine moves at their EARLIEST instant — TNON 13:37:45 @ 4.54
   (MFE +6.85 ATR, 4.54 → 4.98) and PCLA 14:03:43 @ 9.40 (+10.05 ATR, → 10.78).
4. **The leader-ignition bypass is dead machinery.** Zero
   `momentum_reentry_chase_leader_bypass` events in the entire history against 187 blocks
   (2026-07-06 → 09-10); 134/187 blocks carry non-structural triggers, so the bypass is
   structurally unreachable. Deleted with its flag.

### 15.3 The new decision (`risk_policy.reentry_chase_decision`, pure)

* **tape+ ⇒ ADMIT**, whatever the extension.
* **tape readable, not positive, AND above the band ⇒ WAIT** (`reentry_chase_tape_wait`).
  This is the only knife left. It re-checks every tick and releases the moment the tape
  proves it, so it is not a lockout.
* **tape unreadable / stale ⇒ NAMED fallback**, never silent:
  * `atr_pct_source` in (`regime`, `regime_meta`) ⇒ the old ATR ceiling decides
    (`reentry_chase_atr_ceiling_fallback`);
  * `atr_pct_source = fallback_0.015` ⇒ nothing on either side is a measurement of this
    name, so no magic number vetoes: the trade is admitted at the size floor and named
    (`reentry_chase_unmeasured_size_floor`).

### 15.4 Extension conditions SIZE, it does not veto

`risk_policy.reentry_chase_size_multiplier` follows the [62] shape: 1.0 at or below q50,
linear down to the floor at q90, floor above. Applied AFTER `paper_full_size_floor`
(beside `cycle_exhaustion_post_floor`) and cleared on every sizing pass.

| binding | value | derivation |
| --- | --- | --- |
| `REENTRY_CHASE_EXT_Q50` | 6.19 ATR | p50 of extension over the 49 tape-admitted instants (all 177: 4.98) |
| `REENTRY_CHASE_EXT_Q90` | 8.10 ATR | p90 of the same population (all 177: 8.01) |
| `REENTRY_CHASE_SIZE_FLOOR` | 0.6845 | continuation(high ext tercile)/continuation(low tercile) = (8/17)/(11/16); continuation = a print ABOVE the block price within the NEXT 255 prints |

**Honest caveat, reported not tuned.** The band is weak: the middle tercile breaks
monotonicity (0.6875 → 0.8125 → 0.4706) and the whole tape+ population is only 7 clusters;
tape+ continuation (0.6531) versus tape− (0.6250) is flat. The ADMISSION (the tape), not
the band, is the mechanism. The band is reported as binding so a later change is visible.

### 15.5 Counterfactual

Entering each of the 7 episodes at its first tape+ instant and exiting under variant G
(sell ALL at the earlier of accel rollover while ≥ entry, and the tick stop):
**+$420.74 / +1.08 R** under the lane's risk-first sizer ($390 risk canon), versus
**$0.00 actual** — all 177 instants were blocked and zero entries opened. At flat $5,000
notional the same 7 entries net **−$265.14**: the result is sizing-dependent, and
5 of 7 stop out at −1 R on a tick swing-low stop just under the entry print, while PCLA
alone pays +5.30 R.
