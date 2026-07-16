# Ross Course Study — Merged Edge-Build Plan for the Momentum Lane

Date: 2026-06-24
Author: momentum-lane head (CC)
Sources: 4 theme studies of Ross Cameron's actual course (`D:/CHILI-Docker/chili-data/ross_course/*.txt`)
Code under study: `project_ws/_worktrees/etfrank/app/services/trading/momentum_neural/`

## Purpose

Merge four independent theme studies (News-Catalyst Conviction; Entry-Technical;
Execution+Microstructure; Risk+Discipline+Strategy) into ONE prioritized,
de-duplicated edge-build plan. Rank by expected impact × feasibility. Distinguish
what is already in flight from what is NEW. Give a sequenced build order where each
step is additive, replay-validatable, and deploy-when-flat.

## Method note + study corrections (verified against the worktree)

I spot-verified the load-bearing "already exists / not called" claims before merging,
because a stale gap would mis-sequence the build. Corrections:

- **Execution study is STALE on the halt-band veto.** It claims `halt_band_trapped()`
  is "DEFINED but NEVER CALLED." It IS called — `entry_gates.py:544-545` vetoes
  dip-buys with `dipbuy_declined=halt_band_trapped` (equity only, non `-USD`). So that
  item is DONE for the dip-buy path; the only residual is extending the same veto to
  the breakout path. Reclassified to a small P3 extension, not a P1 build.
- **Entry study gap #1 (front-side/back-side veto) is REAL.** `front_side_state()`
  exists in `ross_momentum.py` but `grep` finds zero references to
  `front_side_state` / `is_backside` / `backside` in `entry_gates.py`. The backside
  veto is genuinely unwired at entry. Confirmed P1.
- **Weak-catalyst + hot-tape inversion are wired as a SOFT viability tilt only.**
  `pipeline.py:893-918` computes `weak_catalyst_symbols` + `hot_tape` and threads them
  into `ctx.meta`; `viability.py:347-355` applies them via `catalyst_viability_delta`.
  That is a post-selection score nudge, NOT a hard arm/entry gate. The News study's
  framing (suppression "never applied to live arms" as a veto) is correct in spirit:
  it tilts ranking, it does not veto eligibility. Confirmed.
- **`below_explosive_floor()` is a selection filter** (`pipeline.py:760`,
  `replay_v2.py:622`), not a hard entry gate. The Entry study's "soft check, can be
  out-ranked on cold tape" is accurate. Confirmed.
- **`pullback_ordinal_recent()` is computed at the entry decision**
  (`entry_gates.py:1677`) — so the "first-vs-Nth pullback" detection is DONE
  (Execution study correct). The open piece is the THROTTLE (3rd+ de-rate / raised
  vol floor), which the Entry study flags as gap #7. Confirmed as the real residual.

## Already in flight / shipped (do NOT rebuild)

These appear across multiple studies as "already matches" and are confirmed in code or
in MEMORY. They are the baseline the new builds layer on top of:

- News-conviction half-story: weak-catalyst keyword list, `_is_weak_catalyst`,
  `weak_catalyst_symbols`, hot-tape inversion, sympathy/theme clustering — all live as
  a soft viability tilt (`catalyst.py`, `viability.py:347-355`, `pipeline.py:893`).
- Pullback-break entry, first-pullback bias, ordinal detection, vol-aware tolerances,
  deep-reclaim/halt-resume dip-buy (`entry_gates.py`).
- Candlestick conviction + topping-tail dual exit (`candles.py`, `live_runner.py`).
- EMA-9/20/200, VWAP front-side reading, MACD rollover (`ross_momentum.py`, `candles.py`).
- Daily S&R, round-number, 200-SMA distance as soft selection tilt (`daily_levels.py`).
- Explosive-floor screen (rvol≥5 / chg≥10) as a SELECTION filter (`ross_momentum.py`).
- Risk-first ATR sizing, spread-liquidity risk multiplier, daily-loss cap, drawdown
  breaker, post-stopout cooldown, streak de-risk, **cushion risk ladder** (deployed +
  replay-validated, no-cushion floor raised 0.5→1.0, +$1,015/3d) (`risk_policy.py`).
- Halt-band trap veto on the dip-buy path (`entry_gates.py:544`) — DONE.
- Pullback-ordinal DETECTION at entry (`entry_gates.py:1677`) — DONE (throttle is the gap).
- NBBO tape persistence for the Ross universe at 1-min cadence (`nbbo_tape.py`).
- OFI + micro-price agreement tilt in viability (shipped #699, kill-switchable).
- L2 candidate pre-subscribe; broker-truth reconciliation (24/7 broker-sync) — DONE.

## De-duplication map (4 studies → unified builds)

Several items recur under different names across the studies. Merged:

- **Catalyst grading** appears as News-P1 (title STRONG/MEDIUM/WEAK), News-P1
  (weak-suppression kill-switch), and Entry-#12 (A/medium/weak tilt). Merged into ONE
  build **E2 (Catalyst grading + weak-suppression hard gate)**.
- **Theme/sympathy** appears as News-P2 (theme quality scorer) and Entry-#4 (sympathy
  auto-detector). Merged into ONE build **E7 (Theme/sympathy detector + quality gate)**.
- **Hot-tape news inversion** (News-P2) folds into the catalyst-grade gate context (E2),
  surfaced as the `hot_tape` arm-context flag.
- **Absolute RVOL/chg floor as a HARD gate** (Entry-#3) is distinct from the existing
  soft selection screen — kept as **E3**.
- **Live spread / tape reading** appears as Execution-P1 (live tape spread),
  Execution-P1 (tape green/red flow), Execution-P2 (spread-trend re-validation).
  Merged into ONE build **E4 (Live tape + spread re-validation at entry)**.
- **Order offset discipline** (Execution-P1 limit offset, Execution-P2 session-aware
  offset) merged into ONE build **E5 (Limit-offset / no-market-order discipline)**.

## Ranked edge builds (impact × feasibility)

Ranking key: Impact = expected $ / risk-reduction from Ross's teaching + our live
evidence. Feasibility = code already present, replay path exists, low integration cost.
Priority bands: P1 = ship first (high impact, code mostly present); P2 = strong, more
integration; P3 = polish / discipline / observational.

| Rank | Build | Status | Impact | Feasibility | Priority |
|------|-------|--------|--------|-------------|----------|
| E1 | Front-side / back-side entry veto | NEW (fn exists, unwired) | High | High | P1 |
| E2 | Catalyst grading + weak-catalyst HARD gate + hot-tape inversion gate | IN-FLIGHT→harden | High | High | P1 |
| E3 | Absolute RVOL+chg FLOOR as hard entry gate | NEW (soft screen exists) | High | High | P1 |
| E4 | Live tape + spread re-validation at entry (green/red flow, real spread) | NEW (tape persisted) | High | Medium | P1 |
| E5 | Limit-offset / no-market-order discipline (session-aware) | NEW | High | Medium | P1 |
| E6 | Measured-move scale targets + round-number scales | NEW | High | Medium | P2 |
| E7 | Theme/sympathy detector + headline-quality gate (1000%-mover lever) | PARTIAL (soft tilt) | High | Low | P2 |
| E8 | Pullback-ordinal THROTTLE (1st/2nd full, 3rd+ de-rate) | NEW (detect done) | Medium | High | P2 |
| E9 | Green-to-red session breaker | NEW | Medium | High | P2 |
| E10 | Faded-name FOMO cooldown (stop-watching discipline) | NEW | Medium | High | P2 |
| E11 | Consecutive-loss hard entry block (psych reset) | NEW (soft shrink exists) | Medium | High | P2 |
| E12 | 2:1 target hard-take gate | NEW | Medium | Medium | P2 |
| E13 | Late-session new-entry cutoff (~11:30 ET, hot-tape relax) | NEW | Medium | High | P3 |
| E14 | Per-symbol session loss-fatigue bench | NEW | Medium | Medium | P3 |
| E15 | L2 stack density (thick=skip / thin=explosive) | NEW (no table) | Medium | Low | P3 |
| E16 | Halt-band veto on the BREAKOUT path (extend dip-buy veto) | NEW (dip-buy done) | Low-Med | High | P3 |
| E17 | Float-rotation (vol/float) pillar | NEW | Low | Medium | P3 |
| E18 | Market-wide leading-gainer rank tilt | NEW | Low-Med | Medium | P3 |
| E19 | 200-EMA proximity-from-below caution (large-cap) | NEW | Low | High | P3 |
| E20 | Partnership/contract body verification | NEW (cost) | Low | Low | P3 |
| E21 | News recency window per-catalyst-type | NEW | Low | High | P3 |
| E22 | Pre-session ritual declaration (data-driven, observational) | NEW (hooks exist) | Low | High | P3 |

---

## Per-theme: teaching → CHILI → gap → recommendation

### Theme 1 — News-Catalyst Conviction

**Ross teaches.** 18 catalyst types; only ~7 are STRONG (FDA/clinical, partnership/contract,
M&A, short-squeeze, theme). WEAK catalysts (offering, reverse split, dilution, compliance,
legal) predict FADE — he will NOT trade them (they signal equity issuance). News
FRESHNESS buys credibility (headline within ~2h). The TITLE tells the story. On a HOT tape
(3+ >30% movers at once) the read INVERTS: foreign no-news low-floats lead, US-news names
reject (2026-06-10 KIDZ rejected).

**CHILI does.** `catalyst.py` has the weak-keyword list, `_is_weak_catalyst`,
`weak_catalyst_symbols`, `hot_tape_regime`, sympathy/theme clustering, close-strength prior.
`pipeline.py:893-918` threads `weak_catalyst_symbols` + `hot_tape` + `theme_symbols` +
`symbol_countries` into `ctx.meta`. `viability.py:347-355` applies them via
`catalyst_viability_delta` (zero boost on weak; no-news boost on hot tape).

**Gap.** It is a SOFT viability tilt applied AFTER the symbol clears the arm queue — there
is no hard arm/entry veto, no kill-switch to enable suppression in live config, no
proactive STRONG-catalyst boost (everything not-weak defaults to neutral), no title
grading at fetch, and the hot-tape inversion never gates `entry_gates.check_eligibility`.

**Recommendation.** Build **E2**: (1) `chili_momentum_weak_catalyst_suppression_enabled`
(default 1); fetch weak set per ignition loop; in `entry_gates` veto weak-set symbols
(`weak_catalyst_distrusted`). (2) Add `STRONG_CATALYST_KEYWORDS` + `strong_catalyst_symbols()`;
return (ticker, title, strong_score, weak_score); STRONG→full tilt, MEDIUM→half, WEAK→0.
(3) Wire the hot-tape inversion as an arm-context flag: hot_tape + has-news + US →
confidence penalty; hot_tape + foreign no-news → no penalty. Deploy with kill-switches;
replay 2026-06-09/10/16 measuring veto rate + PnL delta.

### Theme 2 — Entry-Technical (charts, candles, patterns, S&R, indicators, MTF)

**Ross teaches.** 1m primary / 5m context / 10s micro. Front side (fresh near-VWAP) is
tradeable; back side (extended, 9<20 flip, MACD negative, >50% faded from HOD) is not.
First/second pullback only — 3rd is greed (H&S top). Measured move: 2nd leg ≈ 1st leg;
scale half at 1st-leg-high, half at round-dollar. Hard floor ≥5x RVOL AND ≥10% up.

**CHILI does.** Candle shapes + topping-tail exits, vol-aware pullback tolerances, EMA/VWAP/
MACD, daily S&R + round numbers, `front_side_state()`, `intraday_impulse_freshness()`,
`pullback_ordinal_recent()` (computed at `entry_gates.py:1677`), `below_explosive_floor()`
(selection filter).

**Gap.** `front_side_state()` is NOT called in `entry_gates` (verified) → backside chase-tops
(QXL/NXTS class) still fire. No measured-move scale targets (100% off entry, fixed 2x-R).
The explosive floor is a soft selection screen, out-rankable on cold tape. Ordinal is
detected but not throttled. No green-to-red breaker.

**Recommendation.** E1 (backside veto — wire `front_side_state()` into the entry gate,
veto `is_backside AND score<0.4`), E3 (make `below_explosive_floor` a HARD entry gate),
E6 (measured-move + round-number scale targets into `bracket_intent_writer`), E8 (ordinal
throttle: 3rd+ raises vol floor 1.5→2.5x), E9 (green-to-red breaker). All flag-gated +
replay-segmentable.

### Theme 3 — Execution + Microstructure (L2 / tape / spread / halt)

**Ross teaches.** L1 fails silently; read L2 depth (thick=skip, thin=explosive) and the
Time&Sales tape (GREEN hitting the ask = buyer conviction = fire; RED = distribution).
NEVER market orders — limit with a 5-10c offset above the ask (anti-stop-hunt). LULD
halts are asymmetric tail risk: a stop sitting below the halt band can't exit.

**CHILI does.** NBBO tape persisted (`nbbo_tape.py`), OFI + micro-price agreement tilt,
back-side structural flip detection, vol-aware tolerances, **halt-band veto on the
dip-buy path** (`entry_gates.py:544`, verified live), pullback-ordinal detection.

**Gap.** No LIVE tape spread / green-red flow read at the entry decision (gates read 1-min
close bars; `recent_spread_median_bps` exists but is uncalled in viability/runner).
No limit-offset discipline (sim fills at mid; live offset ≈ 0 → whipsaw / sim-live
divergence). No session-aware offset. No L2 density table. Halt-band veto only on dip-buy,
not breakout.

**Recommendation.** E4 (live tape spread AS-OF entry ±30s + green/red `tape_buy_confirmation`
to upgrade ARM→FIRE / downgrade on RED + spread-trend re-validation while armed). E5
(`order_offset_bps(symbol, regime, session, price)` applied in BOTH paper_execution and
live submit; pre-market +5bps cushion — the single biggest sim/live parity fix). E15 (L2
density, deferred — needs a new snapshot table). E16 (extend halt-band veto to breakout).

### Theme 4 — Risk + Discipline + Strategy (psych / risk / routine)

**Ross teaches.** Risk-first sizing (2:1), max-daily-loss hard stops, ≥3 consecutive losses
→ de-risk / STOP and reset, scale INTO winners never out of holes, survive-until-thrive
(no full risk until a cushion), pre-trade meditation + parts-work, re-entry discipline
(stop watching a faded name).

**CHILI does.** Risk-first ATR sizing, spread-liquidity multiplier, daily-loss cap,
drawdown breaker, post-stopout cooldown, streak de-risk multiplier (0.5 floor),
**cushion risk ladder (deployed + replay-validated)**, session-lifecycle meditation hooks
(architectural).

**Gap.** Streak de-risk is a SOFT shrink — operator still fires sub-threshold (the spiral).
No binary consecutive-loss BLOCK. No hard 2:1 take gate (Rambo extends a winner to 3:1 and
gives it back). No faded-name FOMO cooldown (lane re-arms a name that ticks back up). No
per-symbol session fatigue bench (MEGA double-loss). Meditation hooks exist but nothing
prompts/records the ritual.

**Recommendation.** E11 (consecutive-loss hard gate: ≥3 real-entered losses in last 10 →
`max_concurrent_sessions=0` for 1-2h), E12 (`target_2to1_hard_gate`: auto-close-if-touch at
2R, pre-entry veto allowed), E10 (faded-name cooldown 15min, second-wave entries labeled
separately), E14 (session loss-fatigue bench), E22 (observational pre-session declaration
into `trading_session_telemetry`). Note: the cushion ladder is DONE — verify-only, no rebuild.

---

## BUILD ORDER (sequenced — each additive, replay-validatable, deploy-when-flat)

Each step is independently flag-gated and reversible. Deploy only when the lane is flat
(no open positions). Replay-validate on the cited windows before flipping live ON.

1. **E1 — Front-side/back-side entry veto.** Wire the existing `front_side_state()` into
   `entry_gates`. Pure read of intraday df; one veto branch. Highest impact/effort ratio —
   directly stops the QXL/NXTS backside roundtrips we have live evidence for. Replay
   2026-06-07→2026-06-22 segmented by `is_backside`. Kill-switch
   `chili_momentum_backside_veto_enabled` (default OFF until replay, then ON).

2. **E3 — Absolute RVOL+chg FLOOR as hard entry gate.** Promote `below_explosive_floor()`
   from selection-only to an entry-gate veto. Prevents slow-tape entries on cooling
   markets. `CHILI_MOMENTUM_ABSOLUTE_FLOORS_ENABLED` (default ON). Sequence after E1 so
   backside + floor vetoes are measured independently.

3. **E2 — Catalyst grading + weak-catalyst hard gate + hot-tape inversion gate.** Add
   STRONG keyword set + title grading; turn the existing soft weak tilt into a hard arm
   veto behind `chili_momentum_weak_catalyst_suppression_enabled` (default 1); surface
   `hot_tape` as an arm-context penalty. Builds on E1/E3 (selection is now floor-clean +
   backside-clean, so catalyst quality is the next filter). Replay 2026-06-09/10/16/24.

4. **E5 — Limit-offset / no-market-order discipline.** `order_offset_bps()` in both
   paper_execution and live submit, session-aware (pre-market +5bps). This is the biggest
   sim/live PARITY fix — must land BEFORE the tape build so E4's measurements use realistic
   fills. `CHILI_MOMENTUM_ENTRY_OFFSET_BPS=10`. Parity-test: replay vs live on identical
   names, assert fill-price math matches.

5. **E4 — Live tape + spread re-validation at entry.** `live_tape_spread_at_entry()` +
   `tape_buy_confirmation()` (green/red flow) at the FIRE/ARM decision + spread-trend
   re-check while armed. Depends on E5 (realistic fills) for clean before/after PnL.
   `CHILI_MOMENTUM_LIVE_TAPE_ENABLED=1`.

6. **E8 — Pullback-ordinal throttle.** Use the already-computed ordinal
   (`entry_gates.py:1677`) to raise the vol floor on 3rd+ pullback. Tiny, additive,
   replay-segmentable by ordinal. Flag default ON.

7. **E9 — Green-to-red session breaker.** `is_red_session = last < session_open` → no fire.
   Pairs with E1 (both are session-state vetoes). Flag default ON.

8. **E6 — Measured-move scale targets + round-number scales.** `measured_move_target()` →
   (scale_1, scale_2) into `bracket_intent_writer`. Changes exit shape (partial scales vs
   100%-off). Deploy after the entry-side vetoes (E1/E3/E2) so entry quality is stable
   before changing exit mechanics. Parity-test the snap math against 5.3 examples.

9. **E11 — Consecutive-loss hard entry block.** `chili_momentum_consecutive_loss_hard_gate`:
   ≥3 real-entered losses in last 10 → lock slots 1-2h. Psychological circuit-breaker;
   distinct from the portfolio daily-loss cap. Replay: confirm trades blocked in cooldown.

10. **E12 — 2:1 target hard-take gate.** `chili_momentum_target_2to1_hard_gate`:
    auto-close-if-touch at 2R, pre-entry veto allowed. Sequence after E6 (measured-move
    coexists — 2:1 is the floor target, measured-move is the stretch). Replay: count
    winners held past 2R before/after.

11. **E10 — Faded-name FOMO cooldown.** `chili_momentum_faded_name_cooldown_minutes=15`:
    backside-faded names benched for re-arm; re-breaks labeled `second_wave_entry`.
    Replay: are second-wave entries a negative-R pool?

12. **E7 — Theme/sympathy detector + headline-quality gate.** The only selection-breadth
    swing — surfaces 1000%-sympathy movers invisible to RVOL/gap alone. Higher integration
    (sector tags / leader→peer). Phase it: flag-OFF theme field → replay-only sympathy
    scorer → live `theme_driven` label. `chili_momentum_theme_quality_gate=0.05`. Later
    in the order because it changes WHAT enters, after HOW/WHEN it enters is hardened.

13. **E13/E14/E16/E17/E18/E19/E20/E21/E22 — P3 polish batch.** Late-session cutoff,
    session fatigue bench, breakout-path halt-band veto, vol/float pillar, market-rank
    tilt, 200-EMA caution, partnership body verification, per-type recency windows,
    pre-session ritual declaration. Each independently flag-gated; ship opportunistically
    as observational/low-risk additions once the P1/P2 spine is live and measured.

### Verify-only (no build)

- **Cushion risk ladder** — deployed + replay-validated (+$1,015/3d). Spot-check live
  sessions; log `cushion_usd` + multiplier to `trading_session_telemetry`.
- **Pullback-ordinal detection** — DONE at `entry_gates.py:1677` (E8 only adds the throttle).
- **Halt-band dip-buy veto** — DONE at `entry_gates.py:544` (E16 only extends to breakout).

## Operating principles (per CLAUDE.md + operator memory)

- No dark flags: build → wire live → turn ON → observe → adjust. New flags default ON once
  replay is net-positive/neutral; OFF only during the replay-validation window.
- Adaptive, no magic numbers: derive caps from equity/percentile-within-batch; reference
  points (Ross 5×, 2:1, 11:30 ET) are FLOORS/defaults, not frozen ceilings.
- Evolve, not devolve: parity-test every dual-path change; measure before/after; per-sha
  rollback is the safety net. One logical change at a time; restart server between changes.
