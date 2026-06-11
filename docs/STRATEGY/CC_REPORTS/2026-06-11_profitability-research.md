# CHILI Momentum Lane — Profitability Roadmap
**Date: 2026-06-11 (post first full premarket→open session). Evidence base: 288 terminal equity sessions 06-10/11, 6 closed live trades, EDHL full-tape case study, replay v2 sweeps, L2/selection/style research.**

---

## 1. TODAY'S LESSONS

### Lesson 1 — The EDHL math: we lost $76k of signal to vocabulary, and the residual is sizing
Ross booked **+$76,037** on EDHL. CHILI booked **$0** — zero trades, zero entry candidates, zero submits — despite arming **90 seconds before the first breakout** and watching, risk-clean, through the exact 90-second window of Ross's entry. Five distinct entry opportunities, five distinct blockers:

| Opp | Move | Blocker | $ at $2k clip | Fixed now? |
|---|---|---|---|---|
| A premarket squeeze (11:35Z) | +25–30% | `break_low_volume` ×35 during a 600k-shr/min tape | +$500 | **No** — premarket volume baseline untouched by #608/#609/#611 |
| B first pullback-break | +8–12% | viability stale 787–1662s (refresher outage) | +$200 | **Yes** — #608 pin (proven: session 657 ran 30min clean) |
| C vertical leg 10.9→15.49 | +26–30% | nobody armed + tape at 1/min | +$520 | **Mostly** — #609 feeder, but it needs seconds tape on *unarmed* names (still a hole) |
| D THE Ross trade (reclaim) | +11–16% | `pullback_below_ema9` ×66, no reclaim vocabulary | +$260 | **Yes** — #611, merged 87 min late, 0 live conversions yet |
| E flag scalp 13:15Z | +6% | no session; nothing re-armed after operator cancel | +$120 | **Partial** — re-arm-after-cancel semantics unverified |

**All five captured perfectly ≈ +$1,600 at $2k clips.** Ross's $76k came from ~$700k–1M deployed on opp D alone — **350–500× our clip on the identical entry**. The trigger fixes buy us the $1,600; only the `SCALING_ENGINE` liquidity-cap work buys the rest. Do not confuse the two problems.

### Lesson 2 — Order truth is broken; every real fill these two days was an accident
Ack timeout (~20s) was a **false negative**: every "timed out" order was ACCEPTED at RH as a resting GTC limit. Consequences, all six real fills:
- **BATL +$3,112**: retry loop stacked ~5 live orders → 6,185sh filled vs ~1,250 intended (**5× size, uncontrolled**); managed by the generic broker_sync manager by luck.
- **KMRK −$255**: a *dead session's* resting GTC buy filled at 4.35 into a −21.9% dump.
- **AAOG −$51**: raced fill adopted with a generic stop of 11.81 — **above** the 11.65 entry — dumped in 51 seconds. (Lane structural stop was 10.27.)
- **CPSH −$47 / SNDG −$74**: sessions died on watch-expiry while their fills lived; SNDG's adopted stop was 13.58 vs lane intent 17.36 — **21% intent divergence**.

Net **+$2,932 realized — sign was luck, size was uncontrolled**. Intent divergence scales with size; this is the wound that turns the scaling engine into a disaster. The known open wound (raced fills → generic brackets, lane re-adoption TODO) is the **single highest-EV fix in the system**, and the broker order `ref_id` needed for re-adoption is *already logged* in `live_entry_submitted` payloads.

One agent disagreement to resolve empirically: the loss-reversal agent says the 45s ack patience "is NOT in effect" (observed 12.95s/11.26s timeouts); the EDHL case study notes those submits (14:03/14:06Z) **predate** the #611 merge (14:19Z). Unverified, not disproven — **the next real submit is the test**.

### Lesson 3 — The reaper kills setups that are still alive, but blind re-entry is −EV
27 of 164 killed-with-tape sessions (16%) ripped ≥5% within 30min; 12 ripped ≥10% (avg 30-min high +26%). PPCB died on `waiting_for_break` and broke **+130.7%** 74 minutes later; the 30-min rotation missed PPCB, QH, RKDA entirely. Class ceiling ~$5.4k/2 days. **BUT** the loss-reversal sims are unambiguous: a blind price-reclaim re-entry across all kills = **−0.59R/trade as spec'd, still −0.16R best-tuned**. The two agents converge on one mechanism: a **killed-symbol re-WATCH list** that re-arms only on the lane's own fresh `pullback_break` trigger + spread <150bps + fresh viability. Re-watch, never a +1% buy-stop.

### Lesson 4 — Infra starvation is a silent P&L line, and the WS flip didn't fix what we thought
- The 06-11 11:00–12:04Z **viability-refresher outage** starved 14+ concurrent sessions (snapshot ages to 1,928s vs 600s max), costing EDHL (+68.5% post-kill) and QH (+42.4%, peak **16 minutes** after the kill). ~$2.2k ceiling from one outage window. #608 pins armed sessions; nothing detects or heals the refresher itself.
- **stale_bbo did not drop after the 12:44Z WS flip** (1,268 blocks pre vs 1,242 post) — per-symbol WS subscription coverage is incomplete and unverified. stale_bbo is the #1 block reason (5,657 in 2 days). It's a data-rail bug, not a market signal; never loosen the 15s gate.
- The **spread-tape sampler dropped SDOT mid-rip** (tape died 14:14Z while it ran 22.6→30.13), leaving a filled position managed blind; 126/290 killed sessions had zero tape at kill time. Our own tape **never saw EDHL's true highs** (12:17–12:31Z gap covers the HOD leg).

### Lesson 5 — Exits cap winners: 44–67% capture, 55% peak give-back, zero sells into strength
BATL captured **0.44** of its in-trade move ($3,112 realized of $6,853 peak open PnL — gave back 55%); SDOT 0.67; EDHL replay **0.024**. Both real winners rode past their peaks. Trail widths sit at **both bad extremes simultaneously**: AAOG effectively negative (inverted stop), BATL ~1,860bps effective. A 500bps trail from HWM lifts BATL capture to 0.71 (**+$1,856 on one trade**). And the EDHL replay proves trails alone can't capture spike-and-fades (best static = 0.13–0.17 on a full round-trip); only partial scale-out into the parabolic does.

### Lesson 6 — The stops are right; the geometry and fills are wrong
3/4 stops were good (KMRK fell another 44% post-exit — the stop **saved ~$366**). The losses were mechanical: KMRK filled **1,143bps below its recorded stop** (market order into a collapsing book, −$107 of the −$255); BATL filled 95bps below its trail; AAOG's atr_swing stop was computed **above entry** (100% avoidable, ~$120 swing). This is the entry-side spread-cross root cause appearing on the exit side.

### Lesson 7 — The backside is real and structurally unavailable; encode it long-only
Bounce-failure shorts on AAOG and KMRK each returned +2R on tape (~$400–500). **Robinhood does not support equity shorting at all** — and these are low-float HTB names anyway. The V-recovery long re-entry went **0/3, −1R each time**; staying flat after every stop was optimal both days. The only RH-legal expressions of "the backside is coming": (i) hard stand-down lockout after a stop (already effectively optimal), (ii) sell-into-strength partials, (iii) halt-resume-style flush-dip buys at capitulation (KMRK 1.85), (iv) VWAP-reject and first-red-day as *exclusion* signals.

### Lesson 8 — Premarket volume truth is wrong and nothing shipped today touches it
`break_low_volume` fired 35 times on EDHL while it ran 8.7→12.62 on ~600k shares/min — the heaviest premarket tape in the market failed our volume-confirm gate. The bar/volume baseline is calibrated to RTH; before 13:30Z it misclassifies everything. This blocked the single largest %-move entry of the day (opp A, +25–30%).

---

## 2. MID-MOMENTUM DEATH RECOVERY MATRIX

| Death class | n (2d) | EXISTS NOW | MISSING | BUILD | "Reverse into a win" verdict |
|---|---|---|---|---|---|
| **(a) Reaped stale-watcher** (>1800s generic wait) | 38 | 30-min re-arm rotation (missed PPCB/QH/RKDA); 1800s reaper | Post-death tape monitoring; adaptive watch TTL (extend while viability fresh + structure unbroken) | **Killed-symbol re-watch list (60–90min)**: re-arm ONLY on fresh `pullback_break` + spread<150bps + viability fresh. Mass re-arm cycles must not cancel queued sessions (WCT 411 killed at 16s old) | Rips are real ($5.4k ceiling, PPCB +130.7%) but blind +1% reclaim = **−0.59R/trade**. Fresh-setup re-arm only; realizable subset ~⅓–½ of the 12 big rips ≈ $2.3–3.4k/2d ceiling, honestly less |
| **(b) Viability starvation** (snapshot age >600s) | 22 | **#608 armed-session pin (live, proven)**; gate itself correct | Refresher health alert (median age >300s); on-demand refresh from runner before blocking; auto re-arm on freshness recovery | Refresher watchdog + runner-side pull-refresh + freshness-recovery re-arm hook | Pure infra. EDHL +68.5% / QH +42.4% = ~$2.2k from one outage. No reversal play needed — fix the feed |
| **(c) Trigger family** (`pullback_too_deep` / `below_ema9`) | 54 | **#611 reclaim path (live 14:18Z, 146 reclaim_forming events, 3 sessions engaged, 0 conversions yet)** | Pullback-anchor reset/decay on re-arm (CPOP killed TWICE on a stale anchor before +41.5%/+25.5%); watch long enough for reclaim to convert | Anchor-reset-on-re-arm; replay-validate #611 against CPOP/RKDA/EDHL-opp-D tape | Highest ≥10% hit-rate of any class (17%, $4.2k ceiling). The reclaim path IS the reversal play — it converts the death into the entry. Validate before trusting |
| **(d) Spread / stale-bbo** | 160 | Spread + 15s BBO-age gates (correct); 12:44Z WS rail | **Per-symbol WS subscription verification** (flip didn't reduce stale_bbo: 1268→1242); marketable-limit-at-mid + spread-aware sizing for DAIC-type repeats (blocked-and-ran 3×) | WS coverage audit; mid-pegged limit entry for the repeat-blocker cohort | Mostly **correct kills** — capture at 300–2,100bps spreads is illusory; $3.8k ceiling evaporates after crossing the very spreads that triggered the gate. No re-arm: dead names also lose tape |
| **(e) Ack timeout / order truth** | 8 (+20 timeouts) | Unresolved-orders guard (stops stacking, but session idles to death); #611 45s patience (merged, **unverified live**) | Poll order state by `order_id` before declaring timeout; **adopt fill if cumulative_quantity>0**; cancel-before-retry; **day/IOC TIF not GTC**; cancel all child orders on session death; **lane re-adoption keyed on the ref_id already in `live_entry_submitted`** | The full order-truth bundle (this is the lane re-adoption TODO) | **Not a re-arm problem.** −$172 direct bad-bracket losses; the +$3.1k BATL win was made at accidental 5× size outside the lane. Fix truth, not recovery |
| **(f) live_error** | 7 | Quick 5-min re-arm (**proven**: BATL 430→458 recovered the +$3.1k trade) | Formalized error→auto-re-arm with cooldown; symbol-class quarantine (SPKLW warrant crash-loop, 3 re-arms in 60s on no_bbo); hardened #597 halt-resume error path (DGXX errored at halt_resumed) | W/U-suffix quarantine at arm time; error-re-arm policy; halt-resume error handling | Median post-death move **+9.8% — highest of all classes**. Re-arm works here; formalize it |
| **Stop-out (trade-level)** | 4 | 2-strike symbol guard; stops themselves (3/4 good, saved ~$465) | Stop<entry validation gate (AAOG inverted); stop-limit/marketable-limit exits (KMRK 1,143bps slippage) | Submission-time geometry gate + limit-based stop execution | **V-recovery long: 0/3, −1R all three. Backside short: +2R×2 but RH-impossible.** Verdict: stand down after stops (was optimal both days); the only legitimate post-stop long is the capitulation flush-dip (KMRK 1.85), not the +1% reclaim |
| **Wrong-manager adoption** (CPSH/SNDG) | 2 | broker_sync adoption (keeps positions from being orphaned — but with generic brackets) | Session must adopt-or-cancel before watch-expiry kills it | Same fix as (e): re-adoption by ref_id, ordered before expiry | −$121 realized; tape post-kill chopped <2% — losses were purely mechanical, no reversal play exists |

---

## 3. EXITS — where replay says the most money is left

**Findings (n=6 live + EDHL replay; 06-10 tape ~1-min cadence, sweep approximate; SDOT post-exit unverifiable — tape gap):**
- Winners capture 0.44–0.67; every loser that saw green captured none of it (CPSH peaked +2.5%, realized −2.0%).
- Trail sweep: **≤400bps whipsaws all 6 samples negative** (spreads run 40–80bps on these names); the optimal band is **500–1,000bps from HWM, after a structure-stop phase**. Live effective widths sit at both bad extremes (inverted / ~1,860bps).
- Post-exit tape **vindicates exit direction every time** (BATL peak never re-tested; re-entry sims lost). The problem is width + slippage + the missing partial — *not* trigger-happy stops. Where the exit-quality and loss-reversal agents seem to disagree ("widen to 500–1,000bps" vs "do not loosen"), they don't: BATL's effective width was 1,860bps — moving to 500–1,000 is a **tightening**, and the EDHL replay's 1,000bps floor only applies *before* the move (no sub-1,000bps trail until structure breaks).

**The three highest-EV changes, in order:**

1. **Tiered trail + limit-based exit execution.** Structure stop until +5% open; then 500–1,000bps off HWM; tighten toward ~500bps after +20%. Execute trail/stop breaches as marketable limits, not market orders (BATL filled 95bps below its trail; KMRK 1,143bps below its stop = −$107 of pure slippage). **Evidence: BATL capture 0.44→0.71 = +$1,856 on one trade.** Fully replay-validatable tonight.
2. **Partial scale-out into parabolic extension.** Sell 1/2 at +2R or on a parabolic signature (>3× ATR above 9-EMA, tape-speed/size climax with aggressor flip — L2 signals 7/8/12), trail the remainder at ~1,000bps. This is the **only** mechanism that captures the spike-and-fade class: EDHL replay capture 0.024 actual, 0.13–0.17 best-static, **~0.4–0.5 with scale-out (5–20× class improvement)**. It is also the sole RH-legal expression of backside alpha (Lesson 7), and it's what SDOT's 30.13 HWM wanted (~$120+ left).
3. **Breakeven ratchet + stop-geometry gate.** Once open profit >+1.5–2%, stop to entry+spread (CPSH −$47→~breakeven; AAOG/CPSH ~$226 combined swing). At submission, **reject/recompute any long whose stop ≥ entry** (AAOG's atr_swing produced 11.81 over an 11.65 entry — 51-second, 100%-avoidable death).

---

## 4. ENTRIES & SELECTION — adopt from research, mapped to our data

Priority order (impact × data-readiness). Per the adaptive convention: every threshold below is a **documented prior, fit per-symbol-session** from stored snapshots + backfilled future returns (Gould–Bonart logistic), never a hardcoded constant.

1. **Premarket/time-of-day volume normalization** (fixes Lesson 8 / EDHL opp A). Volume-confirm baseline = mean volume *for that same time-of-day slot over 10 days* (Trade-Ideas' "% of normal" semantics), not an RTH-calibrated bar average. Data: our 1m bars + per-slot history. This is the difference between calling 600k shr/min "low volume" and firing.
2. **RVOL_5m burst trigger decoupled from day-change** (the SKYQ/flat-day-burst fix). `RVOL_5m = vol_5m / mean(vol_5m same slot, 10d)`; fire at ≥4–20× with **no |day-change| requirement** — TI deliberately omits it from momentum alerts. Pair with a **dynamic $-volume floor**: cumulative floor collapses to ~40–200k shares when RVOL_5m ≥20× (TI Strategy 6), or use projected $-volume. Feeds #609.
3. **Running-Up quality score** for #609 ranking: `q = Δmid_1m / σ(Δmid_1m, per-stock baseline)`; rank by q (q≈4 = top third, q≈10 = top 1%), not raw %. **Dependency: seconds-cadence tape on unarmed watchlist names** — today the WS tape only ran dense while a runner was armed, and the day's most vertical leg (EDHL opp C) printed into a 1-row/min gap. Universe-wide WS + sampler pinning is load-bearing for this entire item.
4. **L2/tape entry confirmation and veto stack** (from the L2 research, by evidence tier): **OFI over 2–15s windows** (strongest academic signal; our 2s IQFeed cadence is exactly right for CKS-style OFI, too slow for literal next-tick QI); **micro-price − mid** as the marketable-limit offset price; **aggressor imbalance + at-ask streak** from Massive ticks (weight by $-volume, keep odd lots); **ask-decrementing-while-eaten** as the Ross-style fire trigger (tick dispatch, 1–10s); **hidden-seller detector** (cum executed ≥ k× displayed, refill signature) as a long **veto** and post-exhaustion entry; **spoof filter** (persistence-weighted depth — depth that never trades is intent, not liquidity; SEC-documented habitat is exactly our names); QI only when spread ≤ k ticks. Ship **log-only first**, calibrate from our own forward returns, then wire.
5. **Float rotation counter**: `cum_vol / float`; ≥1.0× upgrades to squeeze-regime priority, 2–3× flags exhaustion. Predicts intensity not direction — combine with catalyst. Data: we have float (ross_momentum scorer) + cumulative volume.
6. **Offering-risk score** (the bad-candidate killer; strongest single discriminator per DilutionTracker — 59% of mega-squeezes had **zero** dilution capacity). Standing state: effective S-3/F-3, active ATM, warrants/converts, recent reverse split or S-1; computable **baby-shelf capacity** = ⅓ × (float × 60-day-max-close) − trailing-12m raises. Live: **EDGAR 424B/8-K poll on in-play tickers** = immediate disqualify/exit. New (free) data source; medium effort.
7. **Premarket tradability gate**: ≥100k cumulative by 9:00 ET (300k preferred), premarket RVOL ≥3×, holding-near-highs vs fading-off-highs check, weight 8am+ data (4:00–7:00 ET books are thin/wide).
8. **Day-2 continuation carve-out**: exempt from the ≥10% day-change rule (Ross's own explicit exception) when day-1 closed in top ⅓ of range, day-2 gap ≥4%, volume not decaying >50% vs day-1 pace.
9. **Exhaustion/chase demotion**: ≥4 consecutive up 5-min candles + 2-min RSI ≥75 + ≥2% above VWAP = demote (TI top-reversal formula inverted); plus VWAP-slope filter (flat slope = coin-flip, no trade).

---

## 5. NEW LANES

### A. L2-scalp **merge** (not a lane — a signal layer on the existing lane)
Don't build a separate scalping lane; the momentum lane already owns the right symbols at the right moments. Phase 1 (this week): compute and **persist log-only** — OFI(2–15s), QI, micro-price gap, TFI/at-ask streak, tape-speed z, large-print percentile flags, ask-eaten events, hidden-seller flags, spoof-filtered depth — joined to forward returns (we already backfill future returns on viability snapshots; reuse that machinery). Phase 2: wire as (i) entry-timing tiebreaker + marketable-limit pricing off micro-price, (ii) hidden-seller **veto**, (iii) exit cues (climax = take the partial; speed-stall after extension = tighten trail). Signals 1/3/4/5/7/8/12 ride the Massive tick stream + tick dispatch; 2/9/10 ride the 2s IQFeed snapshots; 6/11 need both. Frequency impact: better timing on existing entries, not more entries.

### B. VWAP pullback/reclaim lane (best-fit new lane)
- **Entry**: (i) *first* VWAP touch after the gap — name +4–8% over OR, **rising** VWAP, declining pullback volume, first green candle with wick rejection (highest-win-rate variant, ~64% practitioner-reported when aligned — practitioner-grade evidence, not academic); (ii) VWAP reclaim — reclaim candle closes above VWAP on ≥2× consolidation volume, slope curling up.
- **Stop**: cents under VWAP + 1×ATR buffer (or reclaim-candle low, wider of the two). **Target**: HoD, then measured move; same 2:1 management.
- **Window**: 9:45–11:00 ET (the dead zone after our open drive) + break-and-hold 14:00–15:00. **Data**: 1m bars + cumulative volume (have), 9EMA, ATR (have). **Frequency**: 1–3/day on the same gappers we already rank — near-zero new scanning cost.
- **Long-only translation of the fail-fade**: touch-and-reject at falling VWAP = exclusion/standdown input to every other lane.
- **Crypto port**: anchored VWAP (swing-low/event/UTC-day anchors); plain daily VWAP is weak without session resets.

### C. ORB stocks-in-play, long-only (diversifier lane, different symbol class)
- **Universe**: liquid names >$5, ATR >1% of price, ranked by first-5-min slot-normalized relative volume, top N (start N=3–5). **By construction zero symbol collision** with the low-float Ross lane.
- **Entry**: 9:35 marketable limit on OR-high break iff candle 1 green (long half only). **Stop**: OR low or 0.05–0.10× 14d ATR. **Target**: 10R or **EoD flat — the EoD exit is load-bearing** (P&L is trend-day capture).
- **Sizing**: fixed-fractional 1% risk; **expect ~17% win rate and long streaks** — sizing must assume it.
- **Evidence honesty**: SSRN 4729284 / QuantConnect replication (Sharpe 2.4-2.8, 36% ann. alpha) but **zero slippage modeled, 2016–2023 sample only, leverage-dependent, optimized variants smell curve-fit**. Run small as a diversifier; replay-validate on our own data first.

### D. Halt/resume extension (extend what #597 built)
Add: (i) continuation-add when first prints > halt price with bid depth holding (L2 signal 9); (ii) **halt-counter** — 3+ consecutive up-halts flips the symbol from continuation to standdown/flush-watch; (iii) post-resume grace window on the spread gate (spreads >2×, volatility ~9× normal for 1–2 min — our gate will be binding; widen limit allowance, never market orders). Frequency: 0–10+/day clustered on exactly our names. Halts **own the symbol** — overrides every other lane.

### E. Day-2 / first-green-day (later; new risk class)
Daily-bar scanner (consecutive-green count, close-location, gap%, float, RVOL) + power-hour close-strength entry for the overnight gap. Few/week. **Overnight holds on RH have no stop protection across the gap** — separate, smaller budget. FRD (price breaking prior close after 3+ green days) = unshortable, encode as a standdown input.

### F. Parabolic flush dip-buy (gated last)
Long-only compatible, but hardest to mechanize safely (halt-down risk, knife-catching). Trigger: ≥X×ATR flush into pre-marked support + 1-min stabilization (green candle + L2 bid-hold — we have the inputs) + not near lower LULD band; stop = flush low; target 9EMA/VWAP underside (~4R geometry); hard time-stop. ~31% of extreme 1-min down-moves revert next minute — the edge is real but fast. Wait until the L2 stabilization detector (signal 9) proves out in log-only mode.

### Collision rules (time-of-day ownership)
| Window (ET) | Owner |
|---|---|
| 7:00–9:30 | Ross premarket rail (just shipped) |
| 9:30–10:00 | Ross gap-and-go (low-float) + ORB at 9:35 (liquid) — segmented by symbol class, no overlap |
| 10:00–11:00 | VWAP first-touch/reclaim; flush-dip watch opens |
| 11:30–14:00 | **Standdown** except halt/resume events |
| 14:00–15:00 | VWAP break-and-hold; FGD confirmation builds |
| 15:00–16:00 | Day-2/FGD entries; ORB EoD exits; no new dip-buys |

One lane per symbol at a time; an active halt sequence owns the symbol over the clock; mean-reversion and momentum lanes never hold opposing theses on one name; shared risk budget with per-lane sizing (low-win-rate ORB at 1% fixed-fractional; VWAP/halt lanes normal size; overnight tiny). Cross-lane **signals** beat cross-lane trades: FRD/halt-count/VWAP-reject are inputs everywhere.

---

## 6. PRIORITIZED ROADMAP

### TONIGHT (replay-lab validatable / small + surgical)

| # | What | Why (evidence) | Risk |
|---|---|---|---|
| 1 | **Order-truth bundle**: poll order state by `order_id` before declaring ack timeout; adopt fill as `live_entered` if cumulative_quantity>0; cancel-before-retry; **day TIF (never GTC)**; cancel all child orders on session death; **lane re-adoption keyed on the ref_id already in `live_entry_submitted`**; verify the 45s patience actually deployed | Every one of 6 real fills was raced to generic brackets; BATL 5× size; KMRK GTC knife −$255; SNDG 21% stop-intent divergence; −$172 direct + uncontrolled size on the only +$3.1k win. Highest EV in the system, and the linkage data already exists | Broker-API edge cases (partial fills, replace semantics) can't be fully replay-tested — stage against RH paper-shape responses; ship behind nothing (operator: live+on), watch first submit |
| 2 | **Stop-geometry gate**: reject/recompute any long whose stop ≥ entry at submission | AAOG: 11.81 stop over 11.65 entry, dead in 51s, −$51, 100% avoidable | ~None; trivial validation |
| 3 | **Replay-validate #611 reclaim** against CPOP (killed twice), RKDA, EDHL-opp-D tape; add **pullback-anchor reset/decay on re-arm** | Class (c) = highest ≥10% hit-rate (17%, $4.2k ceiling); CPOP died twice on a stale anchor before +41.5%/+25.5%; 0 live conversions yet — unproven until replayed | Reclaim may convert into chop entries; replay measures that before live does |
| 4 | **Tiered trail + breakeven ratchet + limit-based stop execution**, swept in replay v2 | BATL +$1,856 (capture 0.44→0.71); KMRK $107 slippage; CPSH/AAOG ~$226 ratchet swing; ≤400bps proven whipsaw-negative — band is 500–1,000bps post-structure | n=6 + 1-min 06-10 tape = approximate sweep; treat band edges as priors to refit weekly |
| 5 | **Viability refresher watchdog**: alert median snapshot age >300s; on-demand refresh from runner before blocking; auto re-arm on freshness recovery | One 64-min outage hit 14+ sessions and cost EDHL +68.5% / QH +42.4% (~$2.2k); #608 pins armed sessions but nothing heals the feed | Low; pure infra hardening |
| 6 | **Sampler pinning**: any symbol with an open position or active session stays in the spread-tape universe unconditionally | SDOT's tape died mid-rip → position managed blind through HWM 30.13 (~$120 left); 126/290 kills had zero tape | Sampler load — cap the pinned set size |

### THIS WEEK

| # | What | Why | Risk |
|---|---|---|---|
| 7 | **Sell-into-strength partials**: 1/2 off at +2R or parabolic signature (>3×ATR over 9EMA, tape-speed climax + aggressor flip), trail remainder ~1,000bps | EDHL-class capture 0.024→~0.4–0.5 (5–20×); BATL ~$1–1.5k more kept; the only RH-legal backside expression; SDOT's 30.13 wanted it | Partials cap the rare monster runner — but the tape says our names round-trip; data favors the partial |
| 8 | **Killed-symbol re-watch list** (60–90min): re-arm ONLY on fresh `pullback_break` + spread<150bps + fresh viability; mass re-arm must not cancel queued sessions; quarantine W/U-suffix symbol classes; formalize error→re-arm with cooldown | 16% of kills ripped ≥5% (PPCB +130.7%); blind reclaim is −0.59R so the trigger must be the lane's own; WCT 411 killed at 16s old by a re-arm batch; SPKLW crash-loop; BATL error-re-arm already proven (+$3.1k) | Re-watch list growth — bound it; honest ceiling is well under the $5.4k headline because most rips aren't capturable at our spreads |
| 9 | **Premarket volume baseline**: per-slot 10d time-of-day normalization for volume-confirm | `break_low_volume` ×35 on a 600k shr/min tape; opp A (+25–30%, +$500 at $2k) lost; untouched by all of today's fixes | Premarket history is thin for fresh gappers — fall back to RVOL-vs-universe percentile when no history |
| 10 | **Burst feeder upgrade**: RVOL_5m decoupled from day-change + dynamic $-vol floor + running-up q-score ranking; **verify per-symbol WS subscription coverage** (stale_bbo 1268→1242 across the flip = it didn't take) and extend seconds tape to unarmed watchlist names | EDHL opp C printed into a 1-row/min gap; #609 is only as good as its tape; stale_bbo is the #1 block (5,657/2d) and is a data bug, not a signal | WS fan-out cost; the pre/post stale_bbo comparison is a raw count (not rate-normalized) — measure properly during the audit |
| 11 | **L2 signal layer, log-only**: OFI/QI/micro-price/TFI/tape-speed/ask-eaten/hidden-seller/spoof-filter persisted with forward returns (reuse viability backfill machinery) | OFI = best-evidenced signal in the literature and fits our 2s cadence exactly; calibration must come from our own tape per the no-magic-numbers rule | None live (log-only); deferred wiring is the point — this is measurement, not a dark flag |

### LATER (ordered)

| # | What | Why | Risk |
|---|---|---|---|
| 12 | **SCALING_ENGINE liquidity cap** (size by name's $-volume) | The only thing separating +$1,600 from +$76k on EDHL (44M shares traded; could absorb 100×+ our clip). Do **not** scale until item 1 (order truth) is proven — intent divergence at 5× size is how this kills us | Sizing into thin names; the cap design IS the mitigation |
| 13 | **VWAP pullback/reclaim lane** (design §5B) | Fills 9:45–11:00 dead zone on symbols we already scan; highest-win-rate complement | Practitioner-grade evidence only; start small, replay first |
| 14 | **Wire L2 signals live**: micro-price limit offsets, hidden-seller veto, ask-eaten trigger, climax exit cue | Whatever the log-only forward returns validate | Wire only what calibrates positive |
| 15 | **Halt lane extension**: continuation-add, halt-counter standdown, post-resume spread grace | Detection + resume-dip already shipped (#597); clustered on our exact names | Resume gaps; re-halt loops |
| 16 | **Offering-risk score + EDGAR 424B poll**; float-rotation counter; catalyst tiering | Strongest bad-candidate discriminator (59% of mega-squeezes had zero dilution capacity); cheap data | EDGAR polling latency; tiering needs a news source decision |
| 17 | **ORB stocks-in-play lane, long-only, N=3–5** | Mechanical, peer-replicated, zero symbol collision | Papers assume no slippage + leverage; 17% win rate needs streak-proof sizing; validate on own replay first |
| 18 | **Add-backs while holding** (multi-leg session model) + day-2/FGD lane | Ross traded EDHL as a 5-leg sequence; our model is one-entry-one-bracket — even perfect #611 catches one leg | Biggest session-model refactor on the list; day-2 = overnight gap risk, new budget class |

### Where the agents disagree / evidence is weak (carry these forward honestly)
1. **Re-arm value**: taxonomy calls reaped-watchers the largest miss ($5.4k ceiling); loss-reversal proves blind capture is negative. Resolved as fresh-setup re-watch (item 8) — but the realizable number is unproven until replayed.
2. **45s ack patience**: "not behaving" (loss-reversal) vs "merged after those submits" (case study). Unverified either way — instrument the next submit (item 1).
3. **Trail width**: 500–1,000bps band rests on n=6 with 1-min 06-10 tape; refit weekly from live capture ratios.
4. **stale_bbo pre/post WS counts** are raw, not rate-normalized — the audit (item 10) should measure blocks per armed-session-minute.
5. **ORB and VWAP lanes**: published evidence has zero slippage modeling (ORB) or is practitioner-anecdotal (VWAP win rates). Both must pass our own replay before earning real size.
6. **SDOT post-exit**: unverifiable (tape gap) — its 0.67 capture number is a floor estimate.