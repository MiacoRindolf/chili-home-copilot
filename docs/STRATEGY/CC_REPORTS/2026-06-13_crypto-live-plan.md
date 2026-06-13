# 2026-06-13 Crypto-Live Plan (deep study synthesis)

# THE CRYPTO-LIVE PLAN (synthesis of zero17-forensics, fee-math, b7-counterfactual, crypto-clock, universe-micro)

**The one-paragraph verdict:** The 0/17 (really 0/21, -$218.96) was 81% execution cost on a ~zero-edge signal, traded in the hostile 21:00–05:00 UTC zone, on structurally unexecutable names, with fees the system never even saw (ledger fee=0, paper sim charges 1/7th of reality). Gate relaxation is disproven (-0.57R net take-all). The lane is rebuilt around four levers, in order: **(1) maker-only execution, (2) liquidity-floored universe, (3) fee-aware 3R geometry on ≥2% stops, (4) the crypto clock.** Live arming stays OFF until the weekend paper gate below passes — and the gate itself is unsatisfiable until the measurement bugs are fixed tonight.

---

## (A) THE CRYPTO CONFIG — implement today (Fri night, before the weekend tape)

### A1. Universe: liquidity floor + tiered whitelist (the single biggest selection fix)
Implement as an **adaptive floor** (rule, not list), seeded by the measured snapshot:
- **Floor rule (new gate at selection/viability):** median spread ≤ 50bps AND L2 bid-depth@50bps ≥ $4,000 AND median 1m $-volume ≥ $1,000. Re-probe books at arm time (the `_cx_book_probe.py` logic). This alone would have blocked every name that lost money live.
- **Today's snapshot — A-tier (arm-eligible):** ORCA-USD, MEGA-USD, HYPE-USD, ETH-USD; probation band HOME, ENA, LIGHTER, BILL. **B-tier (majors, arm-eligible, low burst freq):** SOL, BTC, DOGE, ONDO, LINK, SUI, LTC, INJ, JTO. **C-tier (hard-excluded):** INX, ROBO, XPL, PRL, GWEI, OSMO, MOG, KARRAT, EIGEN, FIDA, PYTH, AST, PERP, DOGINME, POLS, RSC, KAIO + the rest failing the floor.
- **Per-name max notional (adaptive, exit-side bounded):** `min(0.25 × bid_depth@50bps, 0.5 × median_1m_$vol)`, capped by `chili_momentum_risk_max_notional_per_trade_usd` (keep 500). This is the liquidity-ceiling cap from SCALING_ENGINE P1, applied to crypto first.
- New knob: `chili_momentum_crypto_liquidity_floor_enabled=True` (+ the two floor thresholds as settings, documented as the ONE irreducible base per the no-magic-numbers convention).

### A2. Fees: make the system see reality (precondition for everything)
- `CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP`: **120 → 153** (measured blended incl. RFQ; config.py:5043).
- `CHILI_COINBASE_MAKER_FEE_BPS_ROUND_TRIP`: **80 → 50** (verified Advanced-1 tier 25bps/side; config.py:5052).
- Enable the live tier fetch (`cost_aware_live_fee_enabled` path in `app/services/trading/fast_path/fees.py`) as primary; statics above are fallback.
- **Wire venue-bps fees into the paper sim**: replace the `fee_to_target_ratio` model in `paper_execution.roundtrip_fee_usd` (currently ~14bps RT vs ~100–153 real) with `fast_path/fees.py`. Without this the weekend gate is meaningless.
- **Book real commissions**: capture `commission` from Coinbase `get_fills` into `trading_economic_ledger.fee` and net it into outcome PnL (`live_runner._record_live_entry_ledger_safe` currently discards it) — otherwise evolution keeps learning from fee-free PnL.

### A3. Entry: maker post-only, cancel-don't-chase
- `CHILI_COINBASE_MAKER_ONLY_ENABLED=1` for the momentum crypto lane; post-only limit at the decision quote/bid.
- TTL **300s** (`CHILI_COINBASE_MAKER_FIRST_FALLBACK_AFTER_SECONDS=300`) but **disable the taker fallback** (`CHILI_COINBASE_MAKER_FIRST_FALLBACK_ENABLED=0`): at TTL, cancel — never cross. Sim: 72–80% of the lane's own signals fill within 3–5m; missed signals had 10–29% taker win rates anyway. Worth +0.35 to +1.0%/trade — the biggest single lever.
- **Kill the RFQ path** (0.881%/side realized): force CLOB limit orders only.
- **Anti-chase:** `CHILI_MOMENTUM_ENTRY_ALLOW_RUNAWAY_BREAK=0` for crypto (burst-chase bought blow-off tops after a median +6.2% 90m pre-run); keep `chili_momentum_pullback_require_retest=True`; `CHILI_MOMENTUM_SYMBOL_LOSS_COOLDOWN_MIN` **5 → 30** for crypto and verify `CHILI_MOMENTUM_SYMBOL_MAX_DAILY_STOPOUTS=2` actually enforces on crypto (FIDA was entered 5× in 30 minutes).

### A4. Geometry: fee-aware floor via the existing fee gate, 3R targets
- `CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO`: **2.0 → 3.0** (config.py:2603). The surface shows 1.2%-stop/2R can NEVER clear taker fees at ≤50% win; the clearing geometry is maker entry + stop ≥2% + 3R + ≥45% first-target hit (+0.17%/trade).
- Stop floor ≥2%: don't hardcode — with honest fees wired (A2), tighten `CHILI_MOMENTUM_RISK_MAX_FEE_TO_TARGET_RATIO` **0.35 → 0.25**; the ATR%≥~3% candidate floor (expected move ~6%) then emerges from the gate itself. Keep `chili_momentum_risk_stop_vol_floor_mult=0.5`.
- Partial: `CHILI_MOMENTUM_SCALE_OUT_FRACTION` **0.33 → 0.5** for crypto (f=0.33 raises break-even floors ~10%; bank more where the fee is already paid). Keep BE ratchet + 500bps/5m-EMA9 trail (the counterfactual's only big winner, KAIO +$40, came from exactly this ladder).

### A5. Clock: crypto gets its own UTC schedule (new — `schedule_window_now` is ET-equity-only, market_profile.py:88)
- **ON:** 05:00–10:00 and 12:00–21:00 UTC. **OFF/observe-only:** 21:00–05:00 and 10:00–12:00 UTC (follow-through collapses to 8–29%; 14 of the 17 booked live losses were Sun 01:00–10:00 UTC).
- Weekend: full windows (richest tape of the week — 2.2× burst rate, 33% vs 26% follow-through; Sat 17:00 UTC is the best single bucket). Weekday: windows minus the existing `chili_momentum_crypto_pause_during_us_session` overlap (13:30–20:00 UTC) — flag: that pause eats most of the weekday prime window; acceptable while equity owns the slots, revisit after the soak.
- Fix the runner's **21:00–24:00 UTC daily dead zone** (only 3–9 active days vs 33–41 other hours) — it must be deliberate (window) not accidental (bug).

### A6. Dead things to remove
- 24/7 always-on crypto arming (replaced by A5).
- RFQ taker orders (A3).
- Runaway-break chase trigger for crypto (A3).
- C-tier universe (A1).
- Taker fallback after maker TTL (A3).
- The fee_to_target paper fee model (A2).

### A7. Plumbing bugs (block the gate if unfixed)
1. Ghost exit-retry storms: check broker position truth before resubmitting exits (CTSI: 1,550 fails over 8.6h AFTER flat; FIDA: 952).
2. Unbooked round trips: broker-zero reconcile must book the exit (POLS s7, FIDA s20 exist at the broker, not in the DB).
3. Paper spawn starvation: 10 sessions/36h ⇒ single-digit weekend trades ⇒ gate unsatisfiable. Raise crypto paper spawn rate/scan cadence on the whitelist tonight.
4. Alpaca crypto twin 0-sessions: `execution_family_registry.py:213–217` returns coinbase for crypto BEFORE the paper→alpaca branch; also `operator_actions.py:268` hardcodes venue='coinbase'. Fix to route Alpaca-listed majors (BTC/ETH/SOL etc.) in paper; majors-only is correct anyway.

---

## (B) THE VALIDATION GATE — before `CHILI_MOMENTUM_CRYPTO_LIVE_ARM_ENABLED=1`

**Weekend paper (Sat 00:00 → Mon 12:00 UTC), new config, honest venue-bps fees wired, whitelist+windows enforced. ALL must hold:**

| # | Criterion | Threshold |
|---|---|---|
| 1 | Filled round trips (whitelist, in-window) | **n ≥ 25** |
| 2 | Maker fill rate on triggered entries within TTL | **≥ 60%** (sim predicts 72–80%) |
| 3 | Net expectancy after real fees | **≥ +0.10R/trade** AND total net PnL > $0 |
| 4 | First-target (2R-partial) hit rate | **≥ 40%** (surface requires 40–45%; lane's historical 8–19% is the bar to beat) |
| 5 | Max drawdown over the run | **≤ 3R cumulative**; no single trade < −1.5R (stop integrity on dense books) |
| 6 | Measurement integrity | fees booked on 100% of fills; 0 unbooked round trips vs broker truth; 0 exit-retry storms (>10 fails); 0 fabricated-quote voids |

Honesty note: at n=25 a +0.1R mean is weak evidence (≈0.5σ) — the gate is a sign-plus-mechanics check, so the live box must be sized as a continued experiment, not a scale-up. If n<25 because spawn throughput wasn't fixed, the gate FAILS by default — do not arm on a 5-trade sample.

**Live load-test risk box (only after the gate passes):**
- Whitelist A-tier + B-tier majors only; maker-only entries (no taker fallback); UTC windows enforced; CLOB only.
- Per-trade max loss **$25** (`CHILI_MOMENTUM_RISK_MAX_LOSS_PER_TRADE_USD=25`, half default), notional per trade ≤ min($500, depth-derived cap from A1), **max 2 concurrent live**, daily loss cap **$75** (`CHILI_MOMENTUM_RISK_MAX_DAILY_LOSS_USD=75`), weekly kill at **−$150**, ≤ 10 live entries/day.
- Auto-disarm on: 6 consecutive losses; any unbooked round trip; any exit-retry storm; realized fee/side > 60bps (tier regression); any single fill slippage > 50bps past stop.
- Pre-declared purpose of week 1 live: **execution-quality measurement** (maker fill rate live, realized RT cost ≤ ~80bps, live-vs-paper slippage parity within 30bps) — n<30 PnL is not a verdict either way.

---

## (C) REJECTED — ideas the data kills

1. **"The lane is over-gated; relax gates"** — take-all blocked counterfactual +0.03R gross / **−0.57R net**; wide_bbo_spread is PAYING (−0.80R net blocked cohort). Keep every microstructure and trigger-discipline gate.
2. **"Hold longer / be patient on exits"** — hold-to-max −$358, hold-4h −$384 vs actual −$219. Names dump after entry; only selling INTO strength works.
3. **Trading the explosive-alt (C-tier) universe at reduced size** — depth@50bps is ~$0; they're untradeable at ANY size. Exclude, don't size down.
4. **Overnight/equity-clock weekend arming** — 21:00–05:00 UTC follow-through 8–29%; this is where 0/21 was earned. The weekend tape itself is fine.
5. **Verticality gate as the chase fix** — retro-catches 2/21. The fix is pullback/retest triggers + re-entry cooldowns.
6. **Waiting for volume-tier fee relief** — even at 25–35bps taker, 1.2%-stop taker geometry stays −0.40 to −0.96%/trade. Tailwind, not a plan.
7. **Mechanics parity as the live justification** — tonight's full ladder on the real cohort is still **−$91**; zero-fee still −$19.
8. **Sub-2%-stop taker scalps on Coinbase** — structurally unprofitable at any plausible win rate (break-even stop at 2R/50% win = 4.02%).
9. **`price_below_ema9` relaxation** — +0.61R net but n=6; not actionable.
10. **Weekend paper proof under the current fee model** — flattered ~0.85–1.0%/round trip; invalid until A2 lands.

---

## (D) Honest viability probability + fallback

**Is Coinbase-fee crypto momentum viable at our size at all?** The stack of evidence is harsh: 0/21 even gross; empirical first-target hit rates 8–19% vs the 40–45% the fee surface demands; and a near-total structural anti-correlation — names that burst can't be executed, names that can be executed don't burst (only ORCA-class breaks it, measured over a single ~25h window). Maker-only + whitelist + windows + 3R fixes the COST side completely (fees drop from ~1.3R to ~0.25–0.4R of geometry), but the SIGNAL side has never once measured positive, even fee-free, on any cohort examined.

- P(weekend gate passes as specified): **~20%**.
- P(some Coinbase crypto momentum config is net-positive at our size within a quarter, given all fixes + tier progression): **~30–35%**.
- P(the measurement fixes in A2/A7 are worth doing regardless): 100% — without them the lane cannot even know if it's profitable, and evolution rewards are corrupted ~35–55%.

**Fallback ladder if the gate fails (likely):**
1. **Alpaca crypto majors rail** — fix the twin spawn (A7.4) NOW so it accrues comparison data this weekend; Alpaca crypto taker ~15–25bps relaxes the geometry math 2–3×; majors-only matches the B-tier whitelist anyway. This is the most direct path to a tradeable crypto lane.
2. **Majors-only swing variant** — 4h–1d holds, 5–8% targets on B-tier depth; fees amortize to <0.1R; uses the same clock windows for entries. New variant, not a momentum-lane tweak.
3. **Park live crypto indefinitely; keep paper as a research rail.** The equity lane (premarket + Alpaca DMA soak) has proven edge mechanics; crypto stays a measurement-honest paper lab until (1) or (2) shows a positive month.

**Tonight's execution order:** A2 fee wiring + A7.3 spawn throughput first (they gate everything), then A1 floor + A3 maker-only + A5 windows, then A4 geometry, then start the weekend soak. Key files: `app/config.py` (knobs at lines cited), `app/services/trading/fast_path/fees.py`, `app/services/trading/momentum_neural/market_profile.py:88`, `paper_execution.py` (roundtrip_fee_usd), `live_runner.py` (fee booking, exit-retry), `app/services/trading/execution_family_registry.py:213-226`, `operator_actions.py:268`. Evidence caches: `D:/dev/chili-home-copilot/scripts/_cx_cache/`.

---

## Study summaries

### Study 1
THE 0/17 FORENSICS — it was actually 0/21, and the real loss was -$218.96, not the -$143.73 the DB believes.

DATA: All 21 live coinbase round trips (2026-06-06 20:42 UTC -> 2026-06-08 07:58 UTC) reconstructed from momentum_automation_outcomes + trading_economic_ledger + trading_automation_events, then verified against BROKER TRUTH: 120 real fills pulled read-only from the Coinbase fills API (21 BUY orders match the 21 sessions 1:1; account went flat; 2 round trips never booked in the DB: POLS s7, FIDA s20). 1m public candles fetched+cached for all 21 trades (scripts/_cx_cache/, full coverage, no Advanced-Trade-only gaps among traded names; thin-minute gaps noted). Scripts: scripts/_cx_0of17_{extract,candles,fills,attrib,report,peek}.py.

PER-TRADE ATTRIBUTION (entry UTC | sym | hold | notional | gross | real fees | REAL net | exitfric bps | MFE_R | recovered-BE-2h | sim tonight net):
06-06 20:42 POLS $306 36m -3.76 4.87 -8.63 +86 0.4R noBE sim-6.08 (35min STUCK EXIT, 18 fails) [UNBOOKED]
06-06 23:20 RSC $252 3.6m -2.39 4.02 -6.41 +15 1.6R BE sim-7.43
06-07 01:20 KAIO $248 14m -4.13 3.93 -8.06 +111 1.5R BE sim-7.69
06-07 02:09 KAIO $251 30m -9.00 3.95 -12.95 +275 0.4R BE sim-7.88
06-07 02:57 GWEI $251 4m -0.59 2.51 -3.11 +83 3.5R BE+2R sim+0.32 WIN
06-07 03:31 GWEI $251 7m -6.92 2.48 -9.40 -29 0.8R noBE sim-5.58
06-07 05:43 FIDA $257 6m -0.84 2.57 -3.41 +98 1.6R BE sim-6.66
06-07 05:47 FIDA $1059 3m -30.68 10.44 -41.11 +69 -0.2R noBE sim-32.84 (scale-in chase, biggest single loss)
06-07 05:53 FIDA $253 6m -6.54 2.50 -9.04 +66 0.0R noBE sim-8.04
06-07 06:02 FIDA $253 10m -7.67 2.49 -10.16 -70 0.0R noBE sim-8.30
06-07 06:12 FIDA $250 430m -0.86 2.50 -3.36 +140 3.0R BE+2R sim-8.66 [UNBOOKED, 952 ghost exit fails]
06-07 06:22 DRIFT $236 31m -2.19 2.35 -4.53 +4 0.4R noBE sim-3.26
06-07 07:01 DRIFT $235 133m -0.21 2.34 -2.56 +11 1.4R BE sim-3.21
06-07 07:10 KAIO $1509 172m -48.46 14.85 -63.31 +348 2.6R BE sim+40.17 WIN (worst trade -> tonight's biggest winner: 2R partial + BE trail)
06-07 10:05 OSMO $251 9m -2.95 2.49 -5.44 +48 3.4R BE+2R sim-1.57
06-07 10:31 CTSI $261 1.3m -4.76 2.58 -7.34 -1 0.8R BE sim-7.63 (1550 ghost exit fails over 8.6h AFTER flat)
06-07 10:33 BILL $234 3m -0.41 2.34 -2.75 +62 2.8R BE+2R sim-3.80 [verticality would block]
06-07 10:50 BOBBOB $236 5m -0.98 2.35 -3.33 +11 2.4R BE sim-3.30
06-08 03:38 EIGEN $214 2m -1.13 2.13 -3.27 0 2.0R BE+2R sim-1.39
06-08 04:13 EIGEN $213 2m -1.13 2.13 -3.25 0 0.0R BE sim-3.26
06-08 07:58 META $218 54m -5.39 2.15 -7.54 n/a 0.2R BE sim-5.23 [verticality would block]

TOTALS n=21, $7,238 entry notional: gross fill-to-fill -$140.99, REAL fees $77.97 (DB recorded $0), real net -$218.96. 0/21 won even GROSS. Median hold 7.1 min. Median pre-entry 90m run +6.2%.

THE DOMINANT KILLER, with %: ROUND-TRIP EXECUTION COST = 81% of the real loss ($178.19 of $218.96). Components: (1) taker fees 36% — every one of 120 fills was TAKER, 80bps/side on the first 4 trades, 50bps/side after (1.0-1.6% round trip, never booked to the ledger); (2) exit-side cost 39% ($85.49) — market sells sweeping thin books into dumps (KAIO $1.5k position paid 348bps to get out; KAIO s10 275bps; FIDA s20 140bps); (3) entry-side cross 7% ($14.73). The remaining 19% (-$40.77) is adverse mid-to-mid path = the signal itself, i.e. the entries had roughly ZERO edge: a 2.5%-of-notional round-trip cost machine was run on a flat-edge signal.

(a) FEES ALONE: flipped no trade (all 21 lost gross) but deepened the loss 55% and are invisible to the system — trading_economic_ledger.fee=0 on all live rows, so evolution credits learn from fee-free PnL (live_runner books avg fill price only and discards commissions).
(b) SPREAD: entry crossing modest (7%); the real spread damage is on the EXIT side (39%) because exits are market sweeps into momentum collapses.
(c) TRIGGER: entries chase blow-off tops — median +6.2% already run in the prior 90m; only 3/21 paths reached the 2R partial before hitting even tonight's WIDE vol-floored stop (~1.9-2.5%); 10/21 stopped within 10 minutes. Tonight's verticality gate retro-catches only 2/21 — it does NOT fix this cohort.
(d) EXIT QUALITY: old exits sold bottoms (15/21 recovered to entry within 2h, 5/21 to +2R after the sale) BUT patience alone is worse, not better: hold-to-max-hold = -$358, hold-4h = -$384, oracle top-tick exit only +$102 (11/21). The names dump after entry on average — the only winning shape is selling INTO strength (partials), which is what the new ladder does. Plus an infra pathology: ghost exit-retry storms (s52: 1,550 failed exit submissions over 8.6h AFTER the broker was flat; s20: 952 over 7h) and one real stuck exit (POLS: 35 min from intent to fill, 18 fails).
(e) REGIME: the tape was NOT dead — 06-07 (16 of 21 trades, -$190) had constant pumps; most symbols kept moving after CHILI was out. This was not a no-opportunity weekend; it was cost + timing.

COUNTERFACTUAL (tonight's mechanics: 0.33 partial @2R, BE ratchet, 500bps + 5m-EMA9 trail, vol-floored stop, marketable-limit exits, honest 50bps taker fees): 2/21 become winners (KAIO s25 +$40.17, GWEI s11 +$0.32), cohort total -$91.32 — a 58% loss reduction vs the real -$218.96, but STILL NEGATIVE. With the verticality gate: -$82.29 on 19 taken. At ZERO fees: 4/21, -$19. Conclusion for the weekend go-live decision: mechanics parity alone does NOT make this lane profitable on this evidence; live re-arm needs (1) maker-side/post-only execution or a fee-tier path (taker 50bps x 2 vs ~2% stops eats ~0.5R/trade), (2) earlier, non-chase triggers (the 90m +6.2% pre-run is the tell), and (3) the fee-booking + broker-zero-unbooked-exit + ghost-exit-retry bugs fixed so paper/live PnL is even measurable correctly.

Coverage notes: spread-at-entry uses fill-vs-minute-open proxy (no historical L2 on public API); 4/21 entry-friction values missing (thin-tape minute gaps); 15m-ATR vol floor reconstructed from public 1m bars (live used its own feed); sim assumes marketable-limit stop fills at the stop level (gap-through handled via min(stop, open)).

### Study 2
FEE-AWARE GEOMETRY VERDICT — recovered CHILI's actual Coinbase fees from the broker API (DB recorded $0 fees on every live fill): the account sits at the verified "Advanced 1" tier (maker 25bps / taker 50bps), paid 76.7bps/side blended over 60d ($286.31 on $37.3K, 431 fills; 120bps taker mid-May at intro tier, exactly 50bps median since Jun 7). The break-even surface shows the lane's current geometry (1.2% stop, 2R target, ladder partial 0.5 + BE ratchet) can NEVER clear taker/taker fees at <=50% win rate — the ladder caps avg win at ~1.5R and round-trip taker costs ~1.0% ~= 0.83R of a 1.2% stop. The geometry that clears fees at the actual tier: MAKER entry + stop >=2% + 3R target + >=45% first-target hit rate (+0.17%/trade), or stop 3% + 3R at >=40% (+0.15%). Implied candidate floor: ATR% >= ~2.7-3.4% (engine stop = 0.6 x ATR >= 2%), i.e. names that plausibly move >=6% in the hold window. Maker post-only entries at the decision quote fill 72%/80% of the lane's own historical signals within 3m/5m (conservative through-the-price rule, 106 episodes on real 1m paths) and beat taker entries by +0.35 to +1.0%/trade in every geometry x fee config — the single biggest lever; recommendation: post-only limit, 3-5m TTL, cancel-don't-chase. Volume tiers: at the Jun 6-8 live cadence ($345 avg notional, ~$9.9K/day RT volume -> ~$295K/30d) the account would climb to ~25-35bps taker within a month — helps but does not flip the verdict. CRITICAL honesty check: empirical first-target hit rates on the lane's own signal stream were only 8-19% (gross expectancy negative BEFORE fees), far below the 40-50% the surface requires — selection quality, not just fee geometry, is binding. Also: the paper sim charges ~14bps round-trip (fee_to_target_ratio model) vs ~100bps reality — weekend paper cannot prove live-readiness until venue-bps fees are wired in (fast_path/fees.py already fetches the live tier) and ledger fee capture is fixed. Scripts: scripts/_cx_fees_recover.py, _cx_fees_june.py, _cx_breakeven_surface.py, _cx_maker_fill_sim.py; caches in scripts/_cx_cache/ (cb_fills.json, cb_tx_summary.json, candles/, maker_sim_results.json). Coverage: 106/142 entry episodes simulated (26 FET drops were entry-minute candle gaps = thinnest tape, so maker fill% is mildly optimistic); INX/ROBO/XPL covered fine via the public Advanced-Trade market candles endpoint; 104/106 timestamp-price sanity matches.

### Study 3
B7 complete — full crypto gate counterfactual on last 10d of trading_automation_events. 22,879 raw crypto (-USD) blocked/waiting events collapsed to 1,137 episodes (per session+normalized-reason, 5-min gap rule; 430 live / 707 paper across 58 symbols). Coinbase Exchange public 1m candles fetched with chunk-grid caching + exponential backoff: only 158 HTTP requests total, ALL 58 products covered by the Exchange API (the prior pass's 'XPL/INX/ROBO have no public candles' belief was a 429 artifact — XPL/INX/ROBO alone were 525 episodes and are now 90%+ covered). Episode coverage 882/1,137 (77.6%); the 255 uncovered episodes had no 1m prints in the 60-min window (dead tape), concentrated in wide_bbo_spread (55) and quote_unavailable (53) — a selection bias that FAVORS those gates. Counterfactual per episode: entry = payload mid when present else last close at t0; forward returns +15/+30/+60m; stop race = -2% (-1R) vs +4% (+2R, lane's 2:1) over 60m, same-candle tie counted as stop. HEADLINE: taking every blocked episode = +0.03R gross, -0.57R net of retail taker fees (0.6%/side = 0.6R round trip) — the crypto lane is NOT over-gated, and no gate's blocked cohort recovers realistic taker fees. Verdicts (n=covered): PAYING on crypto — wide_bbo_spread n=98 (meanR -0.20 gross/-0.80 net, stop-first 56% vs target-first 18%, r60 median -0.97%; robust ex-top-symbol -0.11), stale_bbo n=7 (-0.40), faded_volume_no_sustain n=3 (-1.00, 100% stop-first), pullback_below_ema9 n=35 (-0.20; live-only -0.41 n=27), retest_failed_hold live n=14 (-0.41), pullback_too_deep live n=72 (-0.27, stop-first 49%). COSTLY GROSS ONLY (negative after taker fees) — reclaim_forming n=89 (+0.20 gross/+0.25 ex-XPL/-0.40 net), waiting_for_break n=63 (+0.24 gross but XPL-concentrated, +0.07 ex-XPL, -0.36 net), waiting_for_reclaim_high n=102 (+0.13/-0.47 net). NEUTRAL — break_low_volume n=73 (+0.09), quote_unavailable n=76 (+0.04), ohlcv_insufficient n=77 (0.00 R but violent tails: mean r60 -2.92% vs median +0.06% — keeps you out of outlier dumps), risk_viability_stale n=27 (+0.01, infra hygiene not alpha), volume_below_1p5x_avg n=11, no_entry_data n=40 (-0.06), risk_daily_loss_cap n=8. Only net-positive blocked cohort: price_below_ema9 n=6 (+0.61 net) — too small to act. Live-vs-paper divergence: trigger-discipline gates (pullback_too_deep, retest_failed_hold, pullback_below_ema9) blocked LOSERS in live (stop-first 49-57%) but mild winners in paper — equity replay verdicts (e.g. the open SDOT pullback_too_deep divergence) do NOT transfer to live crypto. Fee reality from own data: NO live Coinbase fee rows recoverable anywhere (trading_economic_ledger live/crypto rows have no fees); paper lanes assume 0.200%/side (shadow ledger, n=2,056 exits) to 0.26-0.28%/side (momentum paper runner, n=9) — roughly HALF the realistic retail taker 0.6%/side, so weekend paper PnL is flattered ~0.6-0.8% per round trip unless fills are maker. Bonus confirmation: alpaca crypto paper rail spawned 0 sessions in 10d (all 230 crypto sessions are venue=coinbase: 220 live + 10 paper) — the suspected bug is real. Implication for weekend live-arming: gate relaxation is NOT the lever (gates are collectively free-to-paying); the levers are fee mechanics (maker entries/fee tier) and the quality of accepted entries. Artifacts: scripts/_cx_b7_gate_counterfactual.py (fetch+analyze, rerun-free via cache), scripts/_cx_b7_robustness.py, scripts/_cx_b7_symbols.py; data in scripts/_cx_cache/ (b7_episodes.csv, b7_results.json, b7_product_source.json, candles/<SYM>/<chunk>.json) under D:/dev/chili-home-copilot.

### Study 4
THE CRYPTO CLOCK, from 15 days of Coinbase 1m candles (28 lane products, 501k alt symbol-minutes, 1,040 up-bursts, cached to scripts/_cx_cache/), the lane's own paper/live fills, and real Coinbase commission data. Headline: crypto momentum's signal clock is roughly the INVERSE of a naive overnight assumption — bursts and follow-through concentrate 12:00–21:00 UTC daily with a secondary 05:00–10:00 band, and 21:00–04:00 UTC is hostile tape (follow-through collapses to 8–29%). The weekend is structurally FAVORABLE on the signal side: 2.2x the burst rate (3.45 vs 1.58 per 1k min), better follow-through (33.3% n=454 vs 25.7% n=580), and per-name alt dollar flow that does not thin out (≈$494 vs $449/min) — consistent across all 4 weekend days sampled, not one event day. The live 0/17 record was earned almost entirely in the weekend 01:00–10:00 UTC overnight zone on wide-spread names. But the decisive constraint is fees, measured from CHILI's own 431 real fills: blended 0.767%/side (median 0.8%; taker-RFQ 0.881%, taker-CLOB 0.733%, maker 0.412%) — worse than both the 0.6% assumption and the config's 120bps round-trip gate; realized all-in round-trip cost median 1.67% (n=69). At real taker costs the +2%-before-−1% chase needs 84% follow-through to break even; the best hour bucket on earth here prints 60.9%. Verdict: weekend live load-testing is favorable as an EXECUTION test (best tape of the week, 12:00–21:00 UTC Sat) but hostile as a PnL test unless entries are maker-only — fees are 1.3–1.7R of the lane's geometry and losses there would measure the fee tier, not the signal. Coverage caveats: momentum_nbbo_spread_tape contains ZERO crypto rows (it is a Massive equity feed) so historical crypto spread-by-hour is unrecoverable from the DB — spread evidence is 2 live L1 snapshots + realized cost logs; paper trades under the NEW mechanics number only 9 (the 24/7 paper flow is currently starved: 10 sessions in the last 36h); contrary to the prior pass, INX/XPL/ROBO DO have public exchange candles (all 28 products fetched clean, 2,105 requests, zero 429 failures).

### Study 5
THE TRADEABLE CRYPTO UNIVERSE AT OUR SIZE — quant pass on Coinbase Exchange public API (824 products, 475 USD pairs; 99-pair eval set = top-50 by 24h $-vol UNION 61 lane-touched pairs) + DB tape + realized broker fills. Scripts: scripts/_cx_universe_scan.py (meta/stats/books/candles/report), _cx_fee_truth.py, _cx_book_probe.py, _cx_burst_probe.py; cache scripts/_cx_cache/{products,stats_all,books,candles_1m,candles_5m,report}.json.

COVERAGE (honest): metadata + 24h stats for all 475 USD pairs; 4-pass L2 book (spread+depth, n=4/pair) for all 99 eval pairs; 1m/5m candles for 83/99 (57/62 touched — 5 tail names XPL/ZRX/YB/VELO/VOXEL missing candles but have book/spread). Book n=4 is small (spreads stable across passes); candle window ~25h Fri/Sat. 429 contention with a parallel session capped candle fetch — book/spread/stats are complete.

VERDICT — there is a structural spread-vs-burst anti-correlation: the pairs that actually produce Ross-style bursts are exactly the wide/thin ones, and the deep-tight pairs do not burst on a weekend. CORRECTION to the brief: INX/ROBO/XPL/PRL/KARRAT etc. DO have public Exchange candles and are online/unrestricted (post_only=False, limit_only=False, min_market_funds=$1) — the 'no public candles' was a wrong assumption (likely Advanced-Trade-only quote path). They are C-tier on LIQUIDITY (spread+depth), not on data availability.

FEE TRUTH from 431 real broker fills (cb_fills.json): current tier Advanced-1 = taker 0.50% / maker 0.25%. Realized blend: TAKER 0.81% (incl RFQ markups), MAKER 0.41%; recent clean days 0.50-0.53%. Round-trip taker = ~1.0%, vs the lane's ~0.83-1.97% stop => fees eat 50-120% of one R before any spread. This is THE wall, independent of pair selection. Maker-only entry (0.25%/side) roughly halves it.

LIVE RECORD: 220 live coinbase sessions since 2026-06-06, only 17 reached graded terminal PnL, ALL losers, -$143.73; 203 ended pre-fill. Confirms no-fill + fee-wall thesis.

