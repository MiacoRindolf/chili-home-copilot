# 2026-06-12 Quant Pass v2 — Ranked Enhancement List

# RANKED ENHANCEMENT LIST v2 — second deep pass synthesis
(3-day replay basis 2026-06-10..12; deployed baseline = board +$5,979 / wide −$708 / combined +$5,264; all knob locations verified in the night-ops worktree)

---

## A. SHIP THIS WEEKEND (high confidence, replay-validated)

**A1. Broker-truth reconciliation into the outcome finalizer (ORDER_LEDGER state-inversion fix)** — RANK 1
- What: wire `momentum_automation_outcomes` finalization to broker-truth fills (`trading_trades` / broker executions) instead of session event self-reports; reclassify sessions with entry submits + broker fills that are currently `cancelled_pre_entry`/`error_exit` with NULL PnL; set `contributes_to_evolution` from broker truth.
- Measured corruption: 30d ledger says −$234/26 fills/1 win while broker truth says +$2,568/35 trades. BATL +$3,111, HIHO +$346, SDOT +$248 all censored. ARVN 06-12: `streak_mult=0.5` on phantom `win_rate=0.10` the day after +$3k — the sizing engine halves risk at exactly the wrong time. 06-12 fills STILL misclassified (ALOY, RZLV, SMU, ASTN, VSME).
- This is a **prerequisite** for A2/A4: their risk-concentration effects are silently cancelled live if streak/cushion inputs stay corrupted.

**A2. Time-of-day schedule policy** — RANK 2, largest direct measured $: **+$4,942/3d** (combined +$5,264 → +$10,206; min day −$362 → +$1,906)
- Components, in confidence order:
  1. Wide lane OFF 10:30–14:30 ET: **+$1,903/3d** (the wide lane's entire −$708 is midday: −$1,956 in-band). This IS the regime-clustering "midday lull" finding — ship once, not twice.
  2. No new entries ≥14:30 (board too): **+$180/3d**; freed-slot signals there lose money (−$169/−$322 buckets).
  3. Board midday = risk ×0.5–1.0, NOT off: an outright board midday cut **costs $521** (06-10 midday was +$1,132).
  4. Premarket+open (04:00–10:30) risk ×1.5: **+$3,038/3d** — a risk-budget shift (max 3× base $55 = $165/trade when ladder at 2×); must stay bounded by the existing aggregate at-risk $ cap. Medium-confidence component; AM edge is tail-driven but Welch p=0.024, premarket positive all 3 days.
- Knob direction: lane-active windows + per-window risk multiplier in the runner schedule; hot window 04:00–10:30, board-only ×0.5 midday, hard entry cutoff 14:30.

**A3. Re-entry policy: 2-strike → 4-strike + 5min → 15min cooldown** — **+$1,159/3d (~+$386/day)**
- Exact knobs (env-settable, no code change): `CHILI_MOMENTUM_SYMBOL_MAX_DAILY_STOPOUTS=4`, `CHILI_MOMENTUM_SYMBOL_LOSS_COOLDOWN_MIN=15` (`app/config.py:2288-2295`).
- Post-loss re-entry EV +0.08R (not toxic); blocked ≥3rd-attempt pool nets +4.2R; only toxic zone is <15min after the stop; imposed-wait EV peaks at 15–30min. Still kills the FIDA machine-gun mode (3 re-entries in 5min).

**A4. Cushion-ladder floor at 1.0 (kill the 0.5× start)** — **+$1,015/3d alone; +$1,208/3d combined with A3**; chop day turns positive (+$5 vs −$293)
- Exact edit: `app/services/trading/momentum_neural/risk_policy.py:311` — `max(0.5, min(2.0, 0.5 + 0.5*(cushion/base)))` → `max(1.0, ...)` (+ docstring line 291).
- First triggers are the highest-EV pool (+1.45R); the half-size start was a stealth de-risk of the day's best trades. Cost: ~27% more total $ at risk ($10,556 vs $8,337/3d) and gives up green-by-construction — daily-loss cap + drawdown breaker remain the bound. **Ship with A1**, not before it.

**A5. Partial fraction 0.50 → 0.33** — **+19.0R/3d** (~+$1,045 at constant $55 risk; +$318 ladder-$), better R on all 3 days
- Exact knob (already exists, env-settable): `CHILI_MOMENTUM_SCALE_OUT_FRACTION=0.33` (`scale_out_fraction()` in `app/services/trading/momentum_neural/paper_execution.py:200`, consumed at `live_runner.py:1049/3639`). Most robust geometry result: monotone across all 9 RR×frac cells and all 3 days. Keep RR=2.0.

**A6. Venue correctness bundle** (not replay-$; recovers live throughput + measurement truth)
- `app/services/trading/venue/alpaca_spot.py:379`: RTH limit TIF `GTC` → `DAY` for fractional qty (verified: line 379 is GTC, only the extended_hours branch at 375 is DAY) — recovers 25% of twin entries.
- Size all exit sells from broker-reported position qty (fixes 8× "cannot be sold short" / `live_error` stuck sessions on Alpaca; same family as RH "Not enough shares to sell" storms — 37 of 40 RH rejects).
- RH fallback: on "untradable for 24 hour trading" reject, resubmit on the regular-hours order form; retry once on empty response — ~1 recovered live entry/day (material at ~7 entries/day).
- Instrument `alpaca_spot` to write `trading_order_state_log` (currently 0 rows); persist broker execution timestamps (Alpaca `filled_at`, RH `executions[].timestamp`); iqfeed frozen-book watchdog.

---

## B. PAPER-VALIDATE NEXT WEEK

- **B1. Demand-driven slot expansion 04:00–10:30** (up to ~6, conviction-ranked, bounded by aggregate at-risk $ not count): +$1k–3.5k on saturated days, ~$0 thin days. Held back because the harness allocated slots alphabetically — live A0 re-rank may already capture part of this, and it competes with A2's ×1.5 for the same AM risk budget. Uncapped demand hit 17 concurrent (~$950+ simultaneous open risk).
- **B2. Partial fraction 0.25/0.20** (108.7R/114.3R — trend continues): pending live BE-ratchet/trail fill quality; sim post-partial downside ≈ 0 is optimistic.
- **B3. Entry peg = ask + max(1 tick, 1.0–1.5× spread)**: marketable pegs filled 7/8, realized price lands at the touch regardless of cap; n=22, curve coarse.
- **B4. Pre-submit pipeline latency cut** (place→submit median 15–33s → <5s): bigger price lever than venue choice; profile preflight/BP checks first.
- **B5. Paper bench repair**: force paper arms onto live-armed symbols (n=2 overlap ever, 0% sign agreement); reconcile paper gates (9,255 candidates → 99.9% blocked); per-execution-family fee model (Coinbase 60bps shadow = 12.6% phantom cost on Alpaca equities); find why the Alpaca crypto twin arm spawned 0 sessions.
- **B6. Demote (not kill) `momentum_ok_abs_vol`**: negative in both sources but n=3.

---

## C. REJECTED — measured flat/negative, do not relitigate

1. **Loss-streak day stand-down** — every (N,M): 2L/30m −$2,008, 2L/60m −$3,822, 3L/30m −$2,178, 3L/60m −$3,606, 4L/60m −$1,379. Keep daily-loss cap + giveback halt only.
2. **Per-symbol anti-martingale sizing** — −$3,265/3d (halves every first attempt, the +1.45R pool).
3. **Spread-aware stop floor** (exit-study B2) — negative at every k on matched pairs; −9 to −42R portfolio.
4. **Ultra-velocity wider-stop carve-out** — class nearly empty (1/108 entries); widening cut UBXG +21.5R → +6.4R; current tight-stop+trail geometry is what caught both +21R monsters.
5. **RR 1.5 or 3.0** — 1.5 dominated (−$3,300 at f33 basis); 3.0 coin-flip on day type (−$2,700 swing on 06-11). Keep 2.0.
6. **Flat all-day slot bump to 4–6** — ~$0 net (±$300 noise); actively negative after 14:30.
7. **Killing `momentum_ok_rel_vol` on ledger evidence** — broker truth says 4/12 +$109; blocked until A1 + the crypto exit-price double-bookkeeping (FIDA −$54 vs +$123) is resolved.
8. **Tape-velocity/breadth regime dial** — correlation ~0; adaptive fade band net-negative every config. The midday bleed is clock, not tape.
9. **Board midday full stand-down** — costs $521/3d; half-risk only.
10. **Venue migration verdict now** — Alpaca fills are paper-touch fills; the only same-second twin (SMU) favored RH; family/variant kill-promote from the variant table (≤4 fills/family) statistically unsupported — rank by trigger + replay instead.

---

## D. SINGLE HIGHEST-LEVERAGE CHANGE + UPDATED P&L RANGE

**D = A1, broker-truth reconciliation into the outcome finalizer.** The schedule policy has the largest direct measured $ (+$4.9k/3d), but A1 is the multiplier: every sizing decision (streak mult, cushion ladder, evolution credit, every future per-variant study) currently reads a losers-only censored ledger that made the system halve risk the day after +$3k. Without A1, A2's risk concentration and A4's floor are randomized live by phantom 0.5× streak halving; with A1, the same fix also unblocks the rel_vol verdict and repairs the venue soak's evidence trail.

**Interaction cross-checks applied (do not naively sum):**
- A2 already contains the regime-clustering midday finding (count once).
- A3's +$1,159 was measured without A2's 14:30 cutoff — late re-entries lose money, so expect ~$100–300 overlap.
- A4 × A2-component-4 are multiplicative on risk (up to 1.5 × 2.0 × $55 = $165/trade); the aggregate at-risk cap and daily-loss cap are the binding guards — verify the daily-loss cap doesn't strangle the AM window after 2–3 full-size premarket stops.
- A5 banks less cushion per partial → slower ladder ramp; A4's floor reduces cushion dependence, so they're compatible, but **the full combined config (A2+A3+A4+A5) was never jointly simulated** — run one combined pass through the cached-signal harnesses (`_q2_schedule_policy.py` + `_q2_geom_sweep.py`, ~minutes) before deploy and flag if combined < ~70% of the parts' sum.
- B1 slot expansion overlaps A2's ×1.5 (same AM budget) — that's why it's in B.

**Updated expected daily P&L at current sizing ($55 base, cushion ladder) if the A-list ships:**
- Replay arithmetic: +$5,264 → ~$10.2k (A2) plus partially-overlapping +$1.2k (A3+A4) and +19R (A5) ≈ **+$11.5k–13.5k/3d ≈ $3.3k–4.5k/day** on tape like 06-10..12; replayed min day ≈ +$1.9k–2.4k.
- After honest discounts (board-quality selection assumed — wide standalone was −$708; sim-optimistic partial/trail fills; 3-day tail-driven sample; ~20–25% of live entries still lost at placement until A6 beds in): **expected operating range ≈ +$1.5k–3.0k/day on momentum-supply days, +$500–1,900 on the weakest sampled day-type**. The $1k/day floor clears on all three replayed day-types; it is NOT guaranteed on selection-failure/no-supply days, and a single 20R tail trade swings any day ±$1–2k. The residual unsolved problem is unchanged from pass 1: making BATL/DSY-class selection repeat — every $ figure above is conditional on board-quality selection.

---

## Study summaries

### Study 1
OPPORTUNITY CLOCK + CAPITAL CONCENTRATION (3 replay days 2026-06-10..12, bar-scan of 2,315 symbol-days, MC sweep with exactly-reproduced baselines). (1) +3R windows cluster brutally by clock: the 09:30-10:30 hour is the densest (39 wins/100 covered symbols at 09:30, 2.4x the next bucket), a genuine premarket band runs 07:00-09:30 plus an 04:00 mini-burst, and the last 90 minutes are dead (entry win rate 35% at the open vs 4-8% after 15:00). (2) The deployed board replay made 91% of its +$5,979 before 10:30 (AM meanR 1.51 vs PM 0.08, Welch p=0.024); the wide lane's entire -$708 loss is midday (-$1,956 in 10:30-14:30). (3) Slots saturate exactly when it matters: all 3 slots busy 80% of hot-window minutes on 06-12, where 60% of board +3R wins opened into a full book; uncapped demand peaked at 17 concurrent in the open hour. (4) Flat slot bumps to 4-6 are ~$0 whole-day (path noise ±$300), but time-sliced they're +$950 before 10:00 and negative after; only uncapped concurrency captured DSY 07:02 (+$1,808, 19.7R) and UBXG 08:09 (+$1,722), worth +$3,130 on 06-12. DELIVERABLE POLICY (ladder-faithful re-sim, P0 reproduces published $ to within $6): 04:00-10:30 = wide+board active, risk x1.5, slots demand-driven ~6+ bounded by aggregate at-risk $ (not count) with conviction-ranked allocation; 10:30-14:30 = board only at x0.5-1.0 risk, 2-3 slots, wide lane OFF; >=14:30 = no new entries (freed-slot signals there lose money). Combined effect +$5.0k/3 days (+$5,264 -> +$10,206) and min day -$362 -> +$1,906, clearing the $1k/day floor on all three days; hot-window slot expansion adds ~+$1k-3.5k more on saturated days. Artifacts: scripts/_q2_{opportunity_clock,replay_bucket_pnl,slot_utilization,board_missed_while_full,mc_sweep,schedule_policy}.py + CSVs (_q2_opportunity_heatmap, _q2_replay_bucket_pnl, _q2_slot_utilization, _q2_board_missed_while_full, _q2_mc_sweep_{trades,summary}, _q2_missed_signals_by_bucket, _q2_schedule_policy_results) in D:/dev/chili-home-copilot/project_ws/_worktrees/night-ops/scripts/.

### Study 2
RE-ENTRY + SEQUENCING POLICY — 3 concrete changes with measured $ deltas (3-day board replay harness, reproduces night anchors within 2% on 06-10/11; all artifacts in night-ops scripts/_q2_*).

(1) POST-STOP RE-ENTRY: re-entries are NOT toxic — signal-level chains (n=181 standalone attempts, 63 symbol-days) show post-loss next-trigger EV +0.08R (n=62) vs +1.45R for first attempts; the pool the 2-strike blocks (n=22 attempts after 2+ consecutive losses) nets +4.2R because 3rd+ attempts on still-triggering names run +1.28R (n=8). The only toxic zone is re-entry <15 min after the stop (0-5min: -1.04R n=2; 5-15min: -0.11R n=4; live FIDA machine-gun = 3 re-entries in 5 min, -$53). Imposed-wait sweep over 97 stop events: EV peaks at 15-30 min wait (+0.16/+0.17R vs +0.08R immediate). Alpha matrix (n=1,497): 12.5% of first-trigger-fail days still print a 3R later. Portfolio counterfactual: deployed guard (2-strike+5min) costs -$1,126/3d vs no guard under ladder sizing (-$320 under flat — the ladder amplifies blocked early winners); cd15+4-strike is +$33 vs no guard. POLICY: chili_momentum_symbol_max_daily_stopouts 2->4, chili_momentum_symbol_loss_cooldown_min 5->15 = +$1,159/3d (~+$386/day).

(2) CUSHION-LADDER SEQUENCING: same 108 trades under all sizing modes (R-sum invariant +80.8R). Ladder $5,433 > flat $4,446 > per-symbol anti-martingale $2,168 — reject symbol-anti (-$3,265; it halves every first attempt, the best pool). The ladder's real defect is the 0.5x START, not the ramp: floor the multiplier at 1.0 (risk_policy.py:311, clamp 0.5->1.0) = $6,448 = +$1,015/3d, better on all 3 days including the chop day. Best combined config (floor1 + cd15 + 4-strike) = $6,642 = +$1,208/3d and turns the 06-12 chop day positive. Flag: flat is still the most risk-efficient (0.750 vs 0.652 $/risk$) and floor1 risks 27% more total — acceptable under the existing daily-loss cap/breaker.

(3) LOSS-STREAK STAND-DOWN: rejected. Every (N,M) variant tested loses: 2L/30m -$2,008, 2L/60m -$3,822, 3L/30m -$2,178, 3L/60m -$3,606, 4L/60m -$1,379 per 3 days. At 46% win rate 2-loss streaks fire constantly and the system's edge after streaks is unchanged; only the one chop day benefited (+$689) — not identifiable ex-ante. Keep daily-loss cap + giveback halt as the day brakes. Caveat throughout: 3 days, board selection, thin per-bucket n (2-32), live n=7; direction consistent across 4 independent sources (chains, portfolio grid, live outcomes, alpha matrix).

### Study 3
TARGET/STOP GEOMETRY SWEEP on the NEW exit engine, 3 days (2026-06-10/11/12), board variant, 21 geometry combos through a unified parameterized harness (D:\dev\chili-home-copilot\project_ws\_worktrees\night-ops\scripts\_q2_geom_sweep.py) built on the _replay_new architecture: real momentum_pullback_trigger signals generated once per day (cached _q2_sig_<day>.pkl), then cheap portfolio re-runs per geometry. Baseline validation vs published board replays: 0611 exact (40 trades, $2,599 vs $2,606), 0610 close (23 vs 21 trades, $3,127 vs $3,184), 0612 diverges ($-293 vs +$189 — position-independent signal stream adds 4 entries that reshuffle 3-slot concurrency on a near-flat day); all sweep comparisons are within-harness so internally consistent. VERDICT: ONE GLOBAL setting wins — keep RR=2.0 for the first partial, shrink the partial fraction 0.50→0.33, NO spread-aware stop floor, NO ultra-velocity wider-stop carve-out. rr2.0_f33 = 3-day +$5,751 / +99.7R vs deployed rr2.0_f50 +$5,433 / +80.8R (+$318, +19.0R ≈ +$1,045 at constant $55 risk; R better on all 3 days: 48.2/36.1/15.5 vs 40.6/29.8/10.4). The partial-fraction effect is the single most robust result (monotone in R across all 9 RR×frac cells and all 3 days) and the trend continues below 0.33 (f25: 108.7R, f20: 114.3R) — the +2R banked half is mostly drag; the 5m-EMA trail runner is where the money is. Both spread-floor (B2) and ultra-widening REDUCE matched-pair PnL on the exact entries they touch. Key structural insight: the pullback trigger's structure stop means the "1m range > stop+target" ultra class is nearly EMPTY at our entries (1 of 108 baseline entries by ATR defn, 3 by max-5-bar-range defn) — and the current tight-stop+trail geometry is exactly what captured the two +21R monsters (DSY 06-10, UBXG 06-12); widening their stops cut them to +6.4R. Caveats: n=3 days, board (known-mover) selection only, $ totals amplified by cushion-ladder sequencing (R-sum is the geometry-pure metric — conclusions hold in both), trail/partial fills are sim-optimistic (partial = limit no penalty, trail pays half-spread). Artifacts: scripts/_q2_geom_{20260610,20260611,20260612}_{summary,trades}.csv, _q2_geom_3day_summary.csv, _q2_geom_report.py, probes _q2_probe_rr.py/_q2_probe_ultra.py, day outputs _q2_geom_<day>.out.

### Study 4
VENUE MICRO-ALPHA twin report (sessions since 06-11; scripts/_q2_venue_micro_alpha.py + _q2_report.txt + 9 CSV artifacts in night-ops scripts/). Sample is thin: 23 entry submits, 14 real entry fills, 7 exits across both venues. VERDICT: Alpaca is NOT yet measurably better — entry slippage medians vs NBBO mid favor Alpaca (+25bps, n=5) over RH (+43bps clean, n=8), but the only same-second twin pair (SMU) favored RH (+5.5 vs +27.5bps), Alpaca fills are paper-simulator touch-fills (optimistic ceiling), and 2 of Alpaca's 5 'good' fills were adverse-selection resting fills below the bid. The soak's real value so far is the reject taxonomy: Alpaca lost 2/8 entries to a fixable adapter bug (fractional+GTC -> reject 42210000; alpaca_spot.py:379 uses GTC for RTH limits) and has a broken twin exit path (sell-qty > position -> 'cannot be sold short' x8, session stuck live_error); RH lost 3/14 entries at the API ('untradable for 24 hour trading' form rejects on OTLK/CUPR + one empty response). Order ledger: 40/73 RH submits since 06-11 rejected (37 = 'Not enough shares to sell' retry storms = the known state inversion); time-in-SUBMITTING healthy (med 2.1s, p90 3.9s); Alpaca writes NOTHING to trading_order_state_log (venue gap). Optimal entry peg (n=22 submits): cap at ask + ~1.0-1.5 spreads — conditional on broker acceptance, marketable pegs filled 7/8 and realized price lands at-or-inside the ask regardless of cap (wider cap = free fill certainty at current size); at/below-ask pegs miss or adverse-select. Biggest latency lever is internal: place->submit median 15-33s vs broker ack ~2s. Measurement fixes shipped into the harness: NBBO-preferred tape with iqfeed frozen-book guard (iqfeed froze on VSME producing a phantom +1790bps), and detection-lag-aware 'clean' slippage cuts (INDP +1183bps was a 16-min-late detection artifact).

### Study 5
FAMILY/VARIANT EDGE + PAPER TRANSFER VALIDITY (scripts: _q2_family_edge.py, _q2_broker_truth_edge.py, _q2_paper_transfer.py, _q2_alpaca_rail.py in night-ops scripts/; 12 CSV artifacts). HEADLINE: the brain's evolution ledger (momentum_automation_outcomes) is a censored, losers-only sample — 30d ledger shows 26 filled outcomes, 1 win, -$234, while broker truth (trading_trades) shows +$2,568 over 35 closed trades. Every big winner (BATL +$3,111, HIHO +$346, SDOT +$248) was misclassified cancelled_pre_entry/error_exit with NULL PnL and contributes_to_evolution=false, so evolution credit is ANTI-aligned with realized PnL, and the streak/cushion sizing engine is actively halving risk off the false loss record (ARVN 06-12: streak_mult 0.5 on win_rate=0.10 the day after +$3k). Trigger-level truth: pullback_break_ok is the only paying trigger (+$2,644/16 trades, 25% win) but 100% tail-dependent (ex-BATL -$468); momentum_ok_rel_vol's 0/15 kill verdict is DISPUTED by broker truth (4/12, +$109) due to a crypto exit-price double-bookkeeping conflict (FIDA 06-07: -$54 ledger vs +$123 envelopes); momentum_ok_abs_vol is negative in both sources (kill candidate, n=3); family-level kill/promote from the variant table is statistically unsupported (<=4 fills/family). PAPER: transfer validity is unmeasurable — n=2 symbol-day overlaps ever (0% sign agreement); the 10x mass yields 9,255 candidates -> 99.9% gate-blocked -> 10 trades/day; lifetime paper is 14% win, negative every week. Paper is NOT a valid weekend bench yet; use replay + broker truth instead, and force paper arms onto live-armed symbols to manufacture overlap. ALPACA: the 'crypto' paper rail has 0 crypto sessions (all 29 are equities), 2 fills total (RZLV twins, redundant duplicates) booked with a Coinbase 60bps fee shadow on commission-free equities (12.6% of the loss is phantom fees); the live alpaca soak has 4 fills (-$8.35), 8 sell-without-position exit failures, no alpaca rows in the order state log, and zero RH-vs-alpaca head-to-head fills — soak verdict impossible yet. Single highest-leverage weekend fix: broker-truth reconciliation into the outcome finalizer (the ORDER_LEDGER inversion), which un-censors evolution, fixes sizing inputs, and repairs every downstream study.

### Study 6
midday_lunch_lull_is_the_bleed

