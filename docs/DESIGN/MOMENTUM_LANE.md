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

**2026-09-11 [5] — the topping tail reads the LEG's own prints, not a 15-minute bucket.**
Since e91c18092 (2026-09-07) the runner site read `_entry_df` or a 15m wall-clock bar
fetched on every `TRAILING` tick, and that bucket holds prints from **before the position
existed**. WYHG 2026-09-08 09:09:04: bucket o/h/l/c 6.06/6.36/5.78/5.9294, and the 6.36
printed at 09:03:35 — five minutes before the 09:08:37 entry fill — so the bucket was a
topping tail (upper wick 51.7% of range) while the leg's own prints (5.89/5.93/5.8866/5.9294,
n=208, upper wick 1.4%) were not. Two of the three live
fires came from such a wick; across 35 `TRAILING` legs in 14 days the bucket fired 17 times,
7 of them (41%) with the high printed before the entry fill (0 by construction for the leg).
Now `entry_gates.leg_print_candle` (open = first print at/after the entry fill, close = the
last print at the as-of, high/low/count over every print between, the same
publication-eligibility predicate and replay-aware as-of as `high_print_in_window`) feeds
`candles.leg_topping_tail`. The 0.50 / 1.0 fractions are the candle's **definition**, not a
tuned value, and `n ≥ 3` is definitional (an upper wick needs a print above both open and
close). The window is the leg — no clock, no N. The arm receipt carries the leg candle,
`upper_wick_frac`, `wick_to_body` and `binding`.

*Review fixes (same PR, #1406).* **One leg per tick:** the candle is anchored on
`_exit_verdict_entry_at(le)`, the G/D verdict's anchor. Since #1385 `entry_filled_at_utc` is
a recycle key and every adoption path pops it, so it is this leg's fill or absent; absent is
the verdict's own named fallback `entry_fill_anchor_missing`, and crypto is `no_equity_tape`.
**One as-of per tick:** the read takes `tick_as_of`, the instant the bid and the G/D verdict
were read at, never a fresh clock later in the pass. **Bounded:** the read runs under
`bounded_fetchall` with the verdict's timeout (the loop's 2.0 s event-tick spacing), so a
cold or 2-hour leg cannot hold the row-locked session. **Fresh or not judged:** a close print
older than the shared print-age bound (14.69 s, the p99 of 96,360 inter-print gaps) is
`leg_candle_stale`. On a 15-minute-delayed feed the leg is `no_publication_eligible_prints`
for its first ~15 min and stale afterwards. **Named, not silent:** every leg on which the
flag cannot judge the candle gets one `live_topping_tail_unavailable` receipt per binding.
**Corrected population:** the scout's "28 leg fires" walked prints by `observed_at` only.
Re-run with the shipped publication predicate and freshness bound, at every instant a print
becomes available: **27 fires**. The TPET 09-10 leg drops out, because no print became
eligible during its whole 408 s. Sell-at-fire is +$241.53 against +$129.20 actual on those
legs; that is context only, because the arm is a receipt. The OFI lock's candle confirmer
now takes its topping tail from the same leg candle, not a cached 1m wall-clock bucket. Its
MACD rollover is still a 1m bar and is named `candle_macd_basis`.

**The arming pass no longer `return`s.** Since #1377 the arm is receipt-only, and the old
return skipped *everything* below it for that pass: the chandelier ratchet, the
velocity-persistence lock, the measured-move composite, the OFI lock, the [58] tape-accel
reversal, the anticipation remainder, the pyramid merge and add, the micro-pullback /
pullback / flag-breakout adds, and the stop-breach exit (`if bid <= stop_px:`). Dropping it
lets the protective paths run on the arming pass. The add paths may also act on it, where
#1377's sister sites keep a one-tick "no add on the opinion's tick" pre-empt. That is at most
one pass; whether an armed topping tail should *condition* adds is a separate mechanism
decision. `TRAILING` is reached by the early trail-arm seconds after the fill (AHMA 3.2 s,
SKYQ 3.5 s), so the candle is judged on young legs as well as runners. Pinned:
`tests/test_topping_tail_leg_prints.py` (including a real `tick_live_session` pass on an
equity leg reading real prints), `tests/test_g4_grind_tick_wiring.py`,
`tests/test_entry_df_fallback.py`.

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

### First partial = 0.7R, measured on the shape the lane actually runs ([27b], 2026-09-10)

Point 1 above said "the 2:1 reward:risk for the first target is unchanged (verified
correct)". That verification was of the **plan** — the A/B (#1271) that moved
`chili_momentum_risk_reward_risk_ratio` to 2.5 measured the whole trade shape, not the
**level at which the first piece is sold**. Asked directly of the tape, those are different
answers.

**The first sweep of this was wrong, and the review of #1393 proved it.** Three premises
failed, and every one of them gets worse as the level drops — the very axis being measured:

1. **No runner on the only live lane.** `execution_family` = `alpaca_spot` on 1737/1737
   sessions in 7 days. There `scaling` is False and `exit_qty = qty`: the **whole** position
   leaves at the target with `exit_reason='target'`. The swept arm was `0.5·T + 0.5·R_all`;
   the executed arm is `1.0·T`.
2. **Taking the partial arms the breakeven ratchet** (`_scale_out_to_runner`), so `R_all` is
   *not* invariant across the arms — the runner is floored at entry and trailed from the
   partial.
3. **The fill is not the touch.** The trigger fires at `bid ≥ target·(1 − 0.005)` and the
   market sell fills there, so realized ≈ `T − fill_floor_r`.

**Corrected sweep** (same 130 legs / 59 symbol-days; no-partial baseline −73.37 R;
shape-weighted 26 OCO-partial / 58 full-flatten from the lane's own 14-day counts):

| first partial | 0.50R | 0.65R | **0.70R** | 0.80R | 1.00R | **2.50R** |
|---|---|---|---|---|---|---|
| partial + breakeven ratchet | +18.31 | +20.65 | **+25.01** | +19.72 | +8.85 | −6.92 |
| full flatten (69% of entries) | +15.51 | +20.00 | **+20.45** | +15.60 | +7.00 | −15.32 |
| shape-weighted | +16.38 | +20.20 | **+21.86** | +16.88 | +7.58 | **−12.72** ← today |

* Both shapes peak at **0.70R**; 2.5R is worse than taking *no* partial at all.
* **Body** (peak < 5R, n = 125): +60.69 / +66.94. **Tail** (n = 5): −35.69 / −46.49 — the tail
  still wants no early partial. The two disagree; the net favours the low level because the
  body is 125 of 130.
* **Jackknife** over symbol-days: 0.70R is the argmax in **58/59** drops (98%). Largest single
  leg 27% / 34% (AUUD 09-01) — high, which is why the jackknife (dropping a whole symbol-day)
  is the real test, and it passes.

**The per-leg fill floor BINDS; it is not a label.** The sweep counts a print *touch*; the live
partial needs a **bid** at `target × (1 − PARTIAL_TRIGGER_TOLERANCE_FRAC)` and then a sell
across the spread. `paper_execution.fill_floor_r()` measures both per leg —
`(0.005 + spread_bps/10 000) / stop_pct` — and the placed level is
`max(base, min(floor, plan_rr))`. Across the 130 legs the floor is p25 0.221R / p50 0.352R /
p90 1.002R and it lifts **24/130 legs (18.5%)** at base 0.70; making it bind rather than merely
reporting it is worth **+21.86 R vs +18.89 R**. The cap is the plan geometry, because a leg with
a 0.06% stop has a floor of 8.16R and an 8R "target" is not a target.

*Which spread.* The floor's second term is the leg's own **entry-decision** spread, used as a
proxy for the exit crossing and named as one. The review's counter-proposal (use
`side='partial_exit'`, p50 61.72 bps, ⇒ 1.0R) was measured and does not stand:
`spread_bps_at_decision` on an exit row is a literal copy of that leg's entry row — 81/81 exit
and 12/14 partial_exit identical to the bit (78 paired legs: ratio p25 = p50 = p90 = 1.0000).
The 61.72 is a **selection** effect: the 14 legs that got a partial are the wider names (their
own entry-spread p50 is 57.00 vs 40.99 overall). The real gap is that the exit crossing was
**never measured at all** — `intended_price` was NULL on 106/106 exit rows because both
fill-outcome recorders read `le["last_exit_intended_price"]` and nothing wrote it. This PR
writes it (at the single exit-submit seam and at resting scale-limit adoption), so the next
derivation of the floor uses the population that pays it.

**The trigger can never sit below the entry fill.** `target·(1 − tol)` drops below the entry
whenever `rr · stop_pct < tol/(1 − tol) = 0.0050251` — i.e. `stop_pct < 0.7179%` at 0.70R
(0.201% at 2.5R, which no leg reaches). Three of the 88 measured legs are inside it: SKYQ 09-10
($3.37, 0.556%), DPU 09-09 ($2.88, 0.571%), SUNE 09-09 ($3.01, 0.623%). On the no-runner lane
that would sell the **whole** position at a guaranteed loss and book it as
`exit_reason='target'`. `paper_execution.partial_trigger_price()` floors the trigger at the
entry fill — a measured per-leg value, not a constant — and live, paper and replay all use it.

**Two values, not one knob.** `chili_momentum_first_partial_target_r` (0.7) decides only where
the first piece is sold. `chili_momentum_risk_reward_risk_ratio` (2.5) stays the **plan** and
still governs:

* dip-buy **runway affordability** — an ENTRY gate (`entry_gates.py`, `runway_rr_unaffordable`,
  measured zero declines in 14 days; named `runway_reward_risk_floor`)
* trail patience (`cushion_r / rr`)
* the exit ratchets' arm level (`arm_r = max(0.5, arm_frac · rr)`)
* the **meta-label target feature** — deliberately, for training-set parity: every historical
  feature row describes the 2.5R geometry, so feeding the model 0.70R would shift a live sizing
  lever's input distribution rather than correct it. The gap is *reported*
  (`meta_label_derate.target_basis`, `target_basis_rr`, `first_partial_target_r`), not hidden.

The setup-selector R:R ranking *does* follow the first partial — it ranks the geometry it will
actually place, and reports its basis.

**One source for the plan ([37], 2026-09-11).** The plan also caps the per-leg first-partial
fill floor (`max(base, min(fill_floor_r, plan_rr))`), anchors the paper fee basis
(`fee_model_target_price`) and is the counterfactual replay's default R:R. Every consumer reads
it through `paper_execution.plan_reward_risk()` / `class_aware_reward_risk()`, and an unreadable
value falls back to the config field's **declared** default (2.5) — not to a bare `2.0`: eight
such literals existed (seven in `paper_execution`, one in `counterfactual_replay`), and 2.0 is
the arm that *lost* A/B #1271. The field is `gt=0, allow_inf_nan=False`, so `0` / `NaN` / `inf`
fail at load instead of being silently remapped, and `momentum_mfe_target_applied` carries
`plan_rr_source` (`ab_1271_interleaved_10x3` / `env_override:…` / `crypto_class_reward_risk` /
`declared_default_fallback_unreadable_setting`). Caveat on the A/B itself: 95% of the +24.74
advantage came from one window (CELU +23.59) — zero windows got worse, but the evidence of
*benefit* is narrow.

Lowering the single knob would have loosened an entry gate and armed every exit ratchet at
0.5R — neither of which this measurement says anything about. Crypto is untouched: the sweep is
equity tape, so `chili_momentum_crypto_reward_risk_ratio` (3.0) remains the crypto level, and
the receipt says so (`first_partial_base_source = crypto_class_reward_risk`).

**The target cannot learn its own footprint.** A leg that exits *at* the target stops its own
high-water mark there, so its `mfe_r` is right-censored. Left in the pool,
`mfe_percentile_target_r` — the one mechanism that can raise the level — would be pinned at the
base forever, a one-way ratchet *down* that gets worse the lower the level goes.
`exit_calibration.mfe_sample_truncated_by_target` marks those legs and `_recent_mfe_samples`
drops them, using fields `momentum_mfe_realized` has always carried (so history is filtered the
same way as new rows).

**The round-number pull-in below 1R is a readability change, not a bug fix.** Its floor is now
`min(_FIRST_SCALE_MIN_R, plan_target_r)`. Below 1R the old bare 1.0 already made the band
`[entry+1R, rr_target)` empty by construction, so old and new return the same price — verified
over 300,000 randomized (entry, risk, T) triples with **zero** differing inputs. It also only
runs on `partial_capable=True`, i.e. **not** on the Alpaca lane. It is stated honestly as such.

**Reported, not assumed.** `momentum_mfe_target_applied` carries `first_partial_base_r`,
`first_partial_base_source` (derived: default / crypto class / env override — never stamped),
`plan_rr`, `first_partial_leaves_runner`, the per-leg `fill_floor_r` with its inputs
(`fill_floor_stop_pct`, `fill_floor_spread_bps`, `fill_floor_trigger_tolerance_frac`),
`first_partial_floor_binding`, `fill_floor_capped_at_plan_rr` and
`applied_target_below_fill_floor`; it is emitted on the kill-switch and exception paths too.
`live_partial_exit` carries `trigger_price`, `trigger_tolerance_frac`,
`trigger_floored_at_entry` and `entry_price`. There is no enable flag — the value ships live and
on; the rollback lever is `CHILI_MOMENTUM_FIRST_PARTIAL_TARGET_R`.

**Open limit.** The tail (n = 5) wants the opposite and cannot be settled by more tape; it needs
more large winners, which only time supplies. The real fix is a per-leg body/tail classifier
that chooses the level **per trade**; until then the net favours the low level. A base at or
below ~0.35R would score higher still, but there the per-leg floor decides 77.7% of legs and the
realized partial is zero by construction — that is a breakeven-scratch policy, a different
mechanism, and it is an open question in the planner row rather than something shipped here.
When the accel-rollover sell-all (#1385) lands, this level becomes the *second* trigger rather
than the first. Tests: `tests/test_first_partial_target_is_measured.py`.

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
| 0 (after a GREEN leg / profit decay) | prior leg's HIGH PRINT (`entry_gates.prior_leg_high_print`; HWM / exit fallbacks, named) | the tape's **last PRINT** (`last_print` from `signed_tape_accel_features(window_prints=255, feature_contract="count_v1")`; `tick.ask` a named fallback, `price_kind`) | **0 R, `>=`** — the same comparison every other rung makes, so the rung after a GREEN banked round is the LOOSEST one (`reclaim_form=level0_new_high_print`) | `signed_tape_accel > 0` **and** `buy_share_delta > 0` (print-count halves — true of BOTH halves only since [23], §13.3; before it the accel was split at the time midpoint); unreadable ⇒ skipped |
| ≥ 1 (after a RED leg) | same | same | `(level−1)·R` of the failed leg, `>=` (#1376) — **bypassed entirely when there is NO reference at all and `level ≤ 1`** ([7], §13.2) | same (+ structural class / substitute, leader ignition bypass) — **the substitute's tape half is skipped when the tape is wholly unreadable** ([7], §13.2) |

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
  `max(gap_p99_s, chili_momentum_g4_reentry_max_print_age_seconds = 14.69 s)` — **superseded
  by [23] (§13.4): the bound is now the 14.69-s floor alone, held independently of the window
  being tested; under `count_v1` the window's own p99 self-raised it.** The floor is
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
  the deciding values (level / reason / **size_multiplier** — added by [7], §13.2 — / price /
  reference) instead of firing on every tick from both doors.
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

### 13.2 The non-structural substitute fails OPEN on missing data, size-conditioned ([7], 2026-09-11)

The level-≥ 1 substitute (`risk_policy.reentry_escalation_decision` step 1) demanded a
price reclaim **and** a positive tape, both actively satisfied. Two ABSENCES of data were
read as refusals, and both contradicted the rest of the same function. **Measured** (live
`chili`, read-only, 3 days, level 1, `non_structural_trigger`, 4,223 blocks): 2,999 = 71.0 %
carried NO reference at all (a session seeded at level 1 by #1252's cross-day rejection has
no leg TODAY, so the substitute was UNSATISFIABLE for the whole day), 1,180 = 27.9 % were a
genuinely low price, 44 = 1.0 % were the row's original premise.

| absence | what is skipped | what still decides | size | receipt |
|---|---|---|---|---|
| no reference at all (`prior_high_print` + `prior_hwm` + `prior_exit_price` all missing) **and `level ≤ 1`** | the whole PRICE half — including the `(level−1)·R` margin, which is computed inside `_reclaim_required()` and is vacuous exactly here | the tape alone | × **0.81** | `substitute_form=no_reference_tape_only`, reason `non_structural_substitute_no_reference`, `margin_r=None`, `reclaim_form=no_reference_unenforced`, `margin_r_unenforced=<the margin that did NOT run>` |
| the tape is wholly unreadable (accel **and** `buy_share_delta` **and** back buy share all None) | the TAPE half | the price reclaim | × **0.48** | `+unreadable_tape` |

Both multipliers are derived in `app/config.py`
(`chili_momentum_g4_substitute_no_reference_size_mult` / `..._unreadable_tape_size_mult`);
they compose multiplicatively (0.3888) inside `[chili_momentum_frontside_size_floor, 1.0]`
and are **never 0**, so a door can never become a new veto. The size reaches entry sizing
as `_g4_reentry_mult` (in the product, in `le["risk_mults"]["g4_reentry"]`, and re-applied
after `paper_full_size_floor` like the ramp / ToD / shelf / cycle levers) and is cleared
per LEG, not per session.

**Bounds this design deliberately keeps (review fixes, 2026-09-11).**

* **Door 1 is level-bounded.** It makes the ladder's `(level−1)·R` margin vacuous, so without a
  bound the 5th stop-out of the day would enter at the same 0.81 as the 1st. The whole
  derivation and the whole measured population are level 1 (5,627 rows / 7 days); the
  level ≥ 2 no-reference population is **ZERO rows over the full 60-day retention** of
  `trading_automation_events`. The deeper rungs keep refusing (byte-identical to
  origin/main) and are NAMED for the day they appear:
  `substitute_no_reference_level_unmeasured`.
* **Door 2 is NOT parity with step 3 / level 0**, although the first form of this PR said it
  was. Step 3 and level 0 skip on `tape_accel is None` alone; door 2 needs all three tape
  reads absent. The accel-unreadable-but-readable-back-share pocket is therefore still
  refused at step 1. That is deliberate — 0.48 was derived on
  `tape_accel IS NULL AND tape_back_buy_share IS NULL` (n = 28) — and it is named rather
  than papered over.
* **A refusal is byte-identical.** `size_multiplier` / `size_multiplier_binding` /
  `substitute_form` exist in the debug dict ONLY on a pass that actually opened a door, so
  the 1,141–2,061 rows/day `g4_reentry_escalation_blocked` event (emitted as `**dbg` on both
  the trigger and the continuation path) does not grow. This is the [59] byte budget.
* **What keeps refusing, and what it is worth.** Class C — reference present, price clean,
  tape readable but WEAK (9 rows) — earns: AHMA MAE −18.72 %, FTFT −21.81 %, BIAF −2.07 %
  over the next 15 min. Class B — reference present, price genuinely below required
  (1,180 rows = 27.9 %) — is **not** a knife: measured the same way as 0.81 (one sample per
  15-min bucket per symbol, forward 15-min MFE ≥ 2 %, n = 29 buckets / 13 symbols) it hits
  14/29 = 0.483 against the 0.548 we trade at full size, i.e. a ratio of **0.88**, with
  MAE p50 −3.38 % and a tail to −20.29 %. It is left refusing here because the price half is
  the ladder's own contract (the CLRO 07-02 loss-chase this gate exists for); opening it is
  its own design with its own refuter, and the measured 0.88 is written into planner row [7].

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

### 13.3 The remainder of [23]: the cap, the leader bypass receipt, the clock in the bar (2026-09-11)

Verified on origin/main and on the first live day of #1376 / #1386 / #1399 / #1374 / #1398
(lane fast-forwarded 08:29Z): `g4_reentry_escalation_level_update` 9, `g4_same_day_seed` 6,
`g4_reentry_reclaim_proven` 7, `g4_reentry_escalation_blocked` 662, and 0 bailout exits out
of 10. Three holes remained; one PR closes the first and third and puts a receipt on the second.

**A. The cap counts every red exit except a NAMED set.** `risk_policy.reentry_ramp_loss_counts`
was a list of what to count (`stop` token OR `bailout` token), so the #1385 verdict exits that
replaced the opinion bailouts (`tape_accel_rollover`, `tape_sellers_took_it`) were free of the
cap on day one: LBGJ 22135 09:45:43Z `stopout_cap_skipped_non_stop_class {exit_reason:
tape_accel_rollover, return_bps: -358.42, stopout_cycles: 0}` (−$40.00), and the 10:18:21 same-day
seed carried `seed_stopout_cycles 0`. The predicate is now inverted — a red exit is a strike
unless it is in `_CAP_NON_STRIKE_EXIT_REASONS` (the four commanded flattens the exit-order builder
already names as one class — `kill_switch_flatten`, `operator_flatten`,
`overnight_pricebus_dark_flatten`, `eod_flatten` — and the planned exits `max_hold`, `target`,
`scale_out_target`, `scale_out_limit`; name or `<name>_` prefix). An unknown or missing reason is
a strike. `reentry_ramp_strike_class` names the class (`stop` → `bailout` → named non-strike →
`exit_verdict` → `other_red`) and the existing `stopout_cap_counts_bailout` receipt carries it as
`strike_class` (event name kept for ledger continuity); the skip receipt carries
`non_strike_basis` (`named_non_strike` | `revert_stop_class_only`). 14 d census of red live exits
(`momentum_fill_outcomes`): bailout 27, stop 19, operator_flatten 9 (−$531.90), trail_stop 8,
tick_deadman_stop 7, momentum_break_stop 4, deadman_stop 2, tape_accel_rollover 1, burst_window_exit
1 — the inversion changes exactly one leg (LBGJ). `chili_momentum_reentry_ramp_counts_every_loss`
(ON) stays the revert path; the level rule is unchanged (it already counts every loss). The
L4 whipsaw cadence is NOT touched here and is **not** clean: it is a 120-s wall-clock window
keyed on stop-class names — a remaining doctrine item, named in §13.4.

**B. The leader bypass is a ranking that waives the bar — measured, not changed.**
`leader_ignition_bypass` passes BELOW `required` (day leader + structural trigger + tape+) and the
cap exemption (`live_reentry_cap_leader_exempt`) is a ranking too, so past the cap there is no bar.
TNON 09-11 to 11:00Z: 8 bypass fills = −$82.51 against 2 reclaim-proven fills = +$24.26 (the 2
post-cap bypass fills = +$23.98). Forward (the [7] metric, 15-min MFE ≥ 2 %): non-leader same shape
refused 6/11 = 0.545 (8 clusters) vs reference 0.548; leader bypass 4/5 (1 cluster); proven 2/3 —
not decidable, so the pass receipts (`g4_reentry_pass_unproven` / `g4_reentry_reclaim_proven`) now
carry `is_day_leader`, `structural_trigger`, `stopout_cycles`, `past_stopout_cap` and
`max_stopout_reentries`; the dedupe key is unchanged and none of these five keys rides the blocked
receipt (the blocked receipt does grow under `count_v1` — see §13.4). Next step: after 5
days of receipts, rerun the forward with realized P&L per class; if post-cap bypass P&L < proven
P&L, a post-cap re-entry is allowed only with `reclaim_proven`.

**C. No clock inside the bar: the G4 read is `count_v1`.** [29] kept this ramp on
`legacy_time_split` on purpose: 255 prints selected by count, the accel split at the TIME midpoint
and the discontinuity trim at `window_s/2` = 7.5 s — while `buy_share_delta` was already
count-split. The two contracts disagree on tape+ at 465/2,175 = 21.4 % of the comparable G4
instants of the last day (to 2026-09-11 11:15Z; 9 clusters) and neither has an edge over 8 days
(first touch ±2 % in 15 min, ≤ 4 samples per symbol-15-min: TT 40/84 = 0.476, FF 60/133 = 0.451,
count-only tape+ 32/58 = 0.552, legacy-only tape+ 10/16 = 0.625; admitted vs refused 0.507 / 0.470
under `count_v1`, 0.500 / 0.482 under legacy), so the doctrine decides: print-indexed halves, no
seconds. The 7.5-s trim was also BLINDING the bar: over the same day `legacy_time_split` returned
no tape at 67/2,244 instants (3.0 %, the slower names — LBGJ, PCLA, PSIG, SKYQ, SXTC) against 2/2,244
under `count_v1`. The receipts already carry `tape_feature_contract`. The [46] chase gate eats the
same tape and was re-measured under `count_v1` (§15.2, §15.5, §15.6). The [7] substitute: door 2
(×0.48) is defined by a wholly unreadable tape, which the switch makes rarer but does not
redefine; door 1 (×0.81, no reference) DOES read the tape's sign (it passes on tape+ alone), so the
switch moves which no-reference instants pass — 0.81 was derived on the pre-#1376 hold (accel > 0 OR
majority-buy over 15 s, class-A tape+ 36/81 = 0.444 vs tape− 0.420), not on `legacy_time_split`.
Re-measured with the same sampling on the class-A instants since 2026-09-08 (3,266): `count_v1`
tape+ 26/59 = 0.441 ⇒ **0.804** of the 0.548 reference (legacy 24/51 = 0.471 ⇒ 0.859; tape− 0.465 /
0.463 — the tape still does not separate inside class A), so 0.81 stands. `window_prints` 255 is unchanged
(p50 of the 15-s print count at 108 decision instants, #1376 — a 15-s-derived count, named as such).
Tests: `tests/test_cap_counts_exit_verdict_losses.py`,
`tests/test_g4_pass_receipt_carries_leader_and_cap.py`, `tests/test_g4_bar_count_contract.py`.

### 13.4 Review fixes to §13.3 (2026-09-11, same PR)

Thirteen findings were confirmed against the first form of §13.3. Two were major.

**The cross-day seed was still a list of names (major).** `prior_day_rejection_seed` (#1252)
seeded level 1 only when yesterday had a red `live_exit_filled` whose reason matched
`LIKE '%stop%' OR '%bailout%'`. That is the same blindness §13.3 A removed from the cap, and it
sits inside the same `_g4_reentry_escalation_check`. LBGJ's only red exit on 2026-09-11 was
`tape_accel_rollover` −$40.00 (session 22135), so on Monday 09-14 the first LBGJ session would
start at level 0, at full size and with no tape bar. Before #1385 the same failed pop was a
`bailout` and was seeded. The seed now classifies each red reason with the cap's own
`reentry_ramp_strike_class`, reading one `GROUP BY reason` query that is bounded by the reason
vocabulary. Its "prior trading day" is taken relative to the decision instant (`_utcnow()`:
wall UTC when live, the sim clock in replay, the same as the same-day seed), not the wall clock.
The `g4_cross_day_rejection_seed` receipt now carries `prev_trading_day`, `strike_reasons`,
`strike_classes` and `seed_basis`. With `chili_momentum_reentry_ramp_counts_every_loss` OFF the
#1252 substring rule runs verbatim (`seed_basis=revert_stop_or_bailout_substring`).
Census of 30 d of red live exits (40 symbol-days): 35 seeded under the old rule, 36 under the new.
The only change is LBGJ 09-11, and nothing is lost (`scripts/derive_t23_reentry_ramp/seed_census.py`).

**The window raised its own staleness bound (major).** The [59] bound
`max(14.69, window gap_p99)` was always exactly 14.69 under `legacy_time_split`, because the
7.5-s trim caps every gap inside the window. Under `count_v1` the trim is p90 × 7.82 (55–208 s on
slow names), so gaps longer than 14.69 s survive and the window's own p99 sets the bound. This
is the thing [29] forbids: the window being tested must never raise its own freshness
ceiling. Over 1,410 G4 instants in 8 d the bound exceeded 14.69 at 233 (16.5 %), and 7 instants
flipped from a stale WAIT to fresh, 3 of them tape+. Re-read with the shipped helper
(`age_bound_flip_verify.py`):

| instant | age | [59] bound | helper stamp |
|---|---|---|---|
| DPU 09-09 21:36:02 | 30.5 s | 54.65 s (span 1,313 s) | 14.69 s, stale |
| WYHG 09-08 22:12:02 | 26.8 s | 43.41 s | 14.69 s, stale |
| DLTH 09-03 13:24:55 | 15.5 s | 31.87 s | 14.69 s, stale |

The bar now decides on the helper's own stamp (`print_age_s` / `print_age_bound_s` /
`print_stale`, measured against the independent floor at the decision instant), which is the
same source the [26] grind read uses. With no stamp it uses the local age against the floor
alone (`price_age_basis` = `helper_stamp` | `local_fallback_floor`). The [46] chase gate reads
the same `tape_source_stale`, so it inherits the fix.

**Minor findings, fixed:**

* *Whole trade, not final tranche.* The cap judged red-ness on `last_exit_return_bps`, the
  final tranche's return, even though the runner already keeps the whole-trade verdict
  (`g4_prior_trade.was_loss`). Live 09-10, session 21589: a +$22.96 scaled trade whose runner
  trailed out at entry counted as a strike. When the stash belongs to this leg, the whole-trade
  verdict now decides, for the cap, the level and L4 alike. When the two bases disagree, a
  `stopout_cap_loss_basis_whole_trade` row is written.
* *Leg provenance.* Exits that bypass `_complete_confirmed_live_exit` left the prior leg's
  values in place, and the recycle counted them again: an unpriced operator FLATTEN,
  the three `*_broker_zero_reconcile` paths, and the `_whole_trade_pnl is None` skip. The writer
  now stamps `last_exit_leg_key` and `g4_prior_trade.leg_key` as `<session>:<trade_cycles>`. A
  value from another leg holds both the streak and the level
  (`stopout_cap_held_unpriced_exit`). An unstamped value keeps the legacy read, and that is named.
* `alpaca_fractional_remainder_day_close` (the operator-flatten branch's rename) joins the
  non-strike set. The drift pin now reads every literal that can reach
  `_handle_kill_switch_mid_run(flatten_reason=…)`. `exit_retry_cap_emergency` is deliberately a
  strike: it completes a verdict exit that could not fill.
* The binding carries the count_v1 trim that decided: `gap_trim_s`, `gap_trim_window_p90_s`
  and `span_s`. The constant multiplier goes on the deduped pass receipt. The blocked receipt
  is therefore not byte-identical. It grows by these values, plus the longer `gap_trim_basis`
  string that `count_v1` names.
* The stale config text on `chili_momentum_stopout_cap_stop_class_only` ("only stop-class red
  exits advance the cap") now describes the shipped rule.
* The tests that asserted constants defined in the test file were replaced with behaviour
  tests. The same prints now go through the real tape function, the shipped helper, the chase
  gate and door 1, under both contracts. The derivation scripts are committed under
  `scripts/derive_t23_reentry_ramp/`.

**Named rather than fixed. These are open items in the planner row [23]:**

* *L4 whipsaw cadence.* This is a 120-s wall-clock window
  (`chili_momentum_whipsaw_rapid_loss_seconds`, "ONE documented base", with no derivation),
  keyed on stop-class names. Over 30 d there were 4 same-symbol red-exit pairs within 120 s. The
  name list hides 2 of them (WYHG 09-08 stop→bailout, TNON 09-09 bailout→stop), and L4 fired
  once. Next: replace the seconds with the print count between the two exits, measured against
  the leg's own consumption, and key both ends on the strike class. This is a separate
  derivation.
* *`chili_momentum_max_stopout_reentries = 3`.* This is an underived literal. It is a binary
  terminal refusal sitting next to a conditioning level that already counts every red exit.
  Over 30 d it terminalized 1 session, and the leader exemption waived it once. Next: derive N
  from the consecutive-strike streaks against forward outcome, or retire it in favour of the
  level.
* *Leader bypass.* This stays a receipt (§13.3 B). In C the doctrine decided because no
  doctrine rule pulled the other way. In B, "a ranking is not a print" and "mechanism, not
  binary" pull in opposite directions: requiring `reclaim_proven` past the cap adds a refusal,
  and the only post-cap data, 2 fills, were +$23.98. The dated next step is unchanged.

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
| Halt-gap trim | existing window_s/2, 7.5s at current default | Named legacy continuity policy; unchanged for [58] and other consumers ([59]'s G4 read moved to `count_v1`'s scale-free trim in [23], §13.3 C) |
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
whether anyone is buying. It also carried a leader-ignition bypass intended to let a
genuine new leg of the day leader through.

### 15.2 What was measured (live `chili`, read-only, bounded)

Population: 177 `momentum_reentry_chase_blocked` rows, 2026-08-30 → 09-10, forming
**11 episodes** (TNON 77, PCLA 46, AHMA 21, SLE 7, BIAF 7, MIMI 7, BIAF 5, DLTH 4,
LIDR 1, WYHG 1, TPET 1). The CLUSTER is the unit of evidence, not the 77 TNON rows.
Every number below was **re-measured on 2026-09-11 under the tape contract that actually
ships** and on the price basis that actually ships (last print vs prior-leg high print) — see
§15.6. The contract that ships changed the same day: [23] (§13.3 C) moved the G4 read — and so
this gate's tape — from `legacy_time_split` to `count_v1`. Both contracts were re-read side by
side on the same 177 instants; the legacy re-read reproduces every [46] number below, and the
`count_v1` values are given next to them.

1. **The level itself has no edge.** Extension above the anchor in ATR units overlaps
   between episodes that went up and episodes that went down. 1.5 is a symptom, not a
   measurement.
2. **The "ATR" is not an ATR.** `SELECT DISTINCT round(risk_unit_atr/prior_anchor_hwm*100,4)`
   over those 177 rows returns **one** value: `1.5000`. No blocked session's
   `regime_snapshot` carried an `atr_pct`, so `paper_execution.regime_atr_pct()` returned
   its hardcoded `0.015` every time. The "1.5R ceiling" was a fixed **+2.25 %** above the
   anchor — identical for $1.04 MIMI and $11.48 BIAF — and it was silent. It is still the
   band, but it is now named on every receipt (`atr_pct_source`, `risk_unit_source`) and
   it no longer decides on its own: above it, the tape decides.
3. **The tape is readable at every block.** `signed_tape_accel_features` resolved at
   177/177 block instants. Under `legacy_time_split`, tape+ (`signed_tape_accel > 0`
   AND `buy_share_delta > 0`) holds at **47/177**, and at episode level it refuses LIDR /
   DLTH / WYHG / SLE / TPET (zero tape+ instants, all fell) and admits the genuine moves at
   their EARLIEST instant — TNON 13:37:36 @ 4.60 (→ 4.98) and PCLA 14:03:43 @ 9.43
   (→ 10.78). **Under `count_v1` (ships since [23])** tape+ holds at **66/177** (the two
   disagree on 31: legacy−/count+ 25, legacy+/count− 6), readable 177/177 under both, and
   the episode split moves: 8/11 episodes are admitted (SLE 15:39:46 @ 5.06 and TPET
   13:44:41 @ 2.10 join; LIDR / DLTH / WYHG still refused), TNON's first admission is the
   same instant, PCLA's is 14:02:44 @ 9.44 (59 s earlier). By the [46] episode method
   (first touch of +2 ATR vs −1 ATR within 30 min of the first admission) the admitted set
   goes UP first 3/8 (MIMI, TNON, TPET) under `count_v1` against 3/6 (MIMI, TNON, PCLA)
   under legacy — PCLA's earlier instant dips first, SLE's goes down, TPET's goes up. Eleven
   clusters do not separate the contracts; the 8-day G4 forward (§13.3 C) does not either.
4. **The leader-ignition bypass was deleted — but NOT because "0 firings in 187 blocks".**
   That count is worthless: the lane's own `.env` carries
   `CHILI_MOMENTUM_CHASE_CAP_LEADER_BYPASS_ENABLED=0`, so the switch was OFF for every one
   of those 187 blocks and the bypass could not emit an event regardless of trigger class.
   It is deleted because its CASE — the day leader's genuine new leg, with buyers lifting
   — is exactly what the tape admission now lets through, on the same tick, with no board
   read and no second tape query. Operator follow-up: the now-orphaned env key is silently
   dropped by `extra="ignore"` (`app/config.py`) and should be removed from the lane's
   `.env` at the next window boundary.

### 15.3 The new decision (`risk_policy.reentry_chase_decision`, pure)

* **Inside the band ⇒ ADMIT** (`reentry_chase_within_band`) — the replaced guard said
  nothing inside the band either.
* **Above the band and tape+ ⇒ ADMIT**, whatever the extension
  (`reentry_chase_tape_admit`).
* **Above the band, tape readable but not positive ⇒ WAIT** (`reentry_chase_tape_wait`).
* **Above the band, tape unreadable or stale ⇒ WAIT**
  (`reentry_chase_tape_unreadable_wait`).

Both refusals are a WAIT, re-checked every tick, released by the first tape+ print — not
a lockout. They point the same way deliberately: the first shape of this change admitted
an UNREADABLE tape (at a size floor) while refusing a READABLE negative one, so LESS
evidence bought MORE permission — and that branch is exactly the one a thin post-halt
tape, a crypto name (no equity tick tape), a flag-off escalation check and another gate's
fail-open exception handler all land on. The `atr_pct_source`-gated "old ceiling still
decides" branch was removed with it: it was unreachable machinery (0 of 5,680 live
`momentum_symbol_viability` rows in 2 days carry `atr_pct`, so the source is
`fallback_0.015` in 100 % of cases), and the stale sub-case never reaches the gate at all
because `reentry_escalation_decision` already returns False on `tape_stale`.

The gate runs on **both** entry doors: the standard trigger path and the
momentum-continuation fire (which transitions straight to `STATE_LIVE_ENTRY_CANDIDATE`
and, before this change, never saw the chase guard — measured on BIAF 2026-09-03: a
continuation fire at 09:10:50 followed by a standard-path chase block 19 s later).

### 15.4 The band is the UNION of two bases

The deciding price is the tape's LAST PRINT and the anchor is the prior leg's HIGH PRINT
([59]); the ceiling (`cap_r` × 1.5 % of the anchor) was calibrated on the OLD basis (ask
vs quote-mid HWM). Measured on the same 177 rows: **32 fall INSIDE the band on the new
basis** (AHMA 13, SLE 7, MIMI 5, DLTH 4, BIAF/WYHG/TPET 1 each) and **24 of those are
tape−** under `legacy_time_split` (**23** under `count_v1`; the last print is identical
under both contracts, 177/177) — under a print-only band they would enter at FULL size with
no tape check and no ledger row. So `above_band = print-basis OR quote-basis`, and `band_basis` on the receipt
names which one spoke. The guard's reach never shrinks because the measurement basis
improved; the print only adds reach.

### 15.5 There is NO size band (measured, not assumed)

The first shape of this change added a size-down ramp (1.0 at q50 6.19 ATR → floor 0.6845
at q90 8.10) derived from all tape+ instants. Two measurements kill it:

| check | result |
| --- | --- |
| extension at the instants the multiplier would actually condition (first admitted instant per episode, n=6) | −0.62, 1.09, 1.51, 2.00, 2.56, 4.32 — **all below q50**, so the ramp is exactly 1.0 on every entry the change creates |
| the same under `count_v1` ([23], n=8) | −1.85, −0.62, 1.10, 1.51, 2.11, 2.21, 2.56, 4.32 — **all below q50** |
| continuation by extension tercile, 47 tape+ instants, print basis | low 13/15 = 0.8667 · mid 15/17 = 0.8824 · high 15/15 = 1.0000 → ratio **1.1538, RISING** |
| the same under `count_v1` ([23], 66 tape+ instants) | low 19/22 = 0.8636 · mid 20/22 = 0.9091 · high 21/22 = 0.9545 → ratio **1.1053, RISING** |
| continuation by extension tercile, the 21 post-loss re-entries that actually FILLED in 14 d live (`momentum_fill_outcomes`, mode=live) | 5/7 = 0.7143 · 2/7 = 0.2857 · 5/7 = 0.7143 → ratio **1.0000**, non-monotone (contract-independent) |

CONTINUATION = a print strictly above the deciding price within the NEXT 255 prints
(print-indexed — the same window that decides). On the basis that ships, a further
extension is followed by a higher print at least as often, not less; a size-down ramp is
a magic number with a table attached. The extension is therefore REPORTED on every
receipt (`extension_atr`, `extension_atr_quote_basis`) and imposed on nothing. The
mechanism is the admission itself — a WAIT the tape releases every tick.

### 15.6 Binding values

| binding | value | derivation |
| --- | --- | --- |
| admission rule | `signed_tape_accel > 0 AND buy_share_delta > 0` | 66/177 block instants under `count_v1` (47 under `legacy_time_split`); admits 8/11 episodes (6 under legacy), first touch UP 3/8 (3/6) |
| tape feature contract | `count_v1` since [23] (reported as `tape_feature_contract`) | the contract `_g4_reentry_escalation_check` reads; it disagrees with `legacy_time_split` on **31/177 (17.5 %)** instants (TNON 17, PCLA 5, AHMA 3, SLE 2, MIMI 2, BIAF 1, TPET 1); every value in this table was re-read under both |
| band | `anchor + chili_momentum_reentry_chase_cap_r × risk_unit`, UNION of print and quote bases | 32/177 fall inside the band on the print basis alone, 23 of them tape− under `count_v1` (24 under legacy) |
| size multiplier | **none** | §15.5 — inert on the conditioning population under both contracts, and continuation RISES with extension (1.1053 `count_v1`, 1.1538 legacy) |
| `atr_pct_source` | reported only | 0 of 5,680 live regime snapshots (2 days) carry `atr_pct`; the value is the hardcoded `0.015` in 100 % of cases |

Reproduce: `signed_tape_accel_features(sym, db=db, as_of=<block ts>, window_prints=255,
feature_contract="count_v1")` (and `"legacy_time_split"` for the [46] originals) and `prior_leg_high_print(sym, db=db,
entry_at=<prior leg live_entry_filled ts>, exit_at=<prior leg live_exit_filled ts>,
as_of=<block ts>)`. The tape values are NOT stable across weeks — publication-eligibility
filtering moves them — so the fixtures in `tests/test_reentry_chase_is_the_tape.py` carry
the measurement date and the contract in their docstrings.
