# Warrior Courses Re-Audit — CHILI Momentum Lane (2026-06-26)

Method: 21-agent deep research over ALL 7 courses (AS101/HVM101/PSY101/RH101/SCAL101/TOS101/SS101). Each agent read transcript CONTENT + sampled visual key-frames (vision) + audited vs the DEPLOYED code (etfrank/098a6aa) by reading actual logic — NOT filenames/function-names (the prior 3x over-claim root cause). Every gap adversarially verified against the code (really missing vs wrong-name) + scanned for missed techniques.

## Coverage by pillar
Calibrated coverage by pillar. Basis = fraction of the concrete, MECHANIZABLE Ross techniques in each pillar (across AS101, HVM101, SCAL101, TOS101, SS101 a-d, PSY101, RH101) that are genuinely implemented AND live-wired in the deployed momentum_neural lane (project_ws/_worktrees/etfrank), counting flag-default-OFF or inert logic as PARTIAL (half), not COVERED. Percentages are deliberately conservative because prior audits over-claimed.

ENTRIES ~75%. STRONG: micro-pullback/first-pullback-break, flush-dip front-side/bottoming-tail/reclaim, VWAP-reclaim & sub-VWAP-trap, HOD/flat-top break, blue-sky, ABCD coil, inverse-H&S, double-bottom, red-to-green, wick-reclaim, halt-resume DIP, cup-and-handle (implemented but flag default-OFF). GAPS: wedge/converging-trendline break (the one clean MISSING entry, HVM101), soaker/clustered-bottoming-tail absorption (SCAL101), absorption-then-SNAP / eaten-seller-squeeze long trigger (SS101-029 + SCAL101), add-INTO-the-halt (the SAFEST halt method, SS101-048), curling/pulling-away distinct trigger, premarket-pivot-break specifics (MACD-reopen + cold-market avoid). Round-number ENTRY-timing (under/test/hold-over) absent — round numbers feed only targets, not entry.

EXITS ~85% (best-covered actionable pillar). Risk-first sizing, 2:1 R:R floor, structure stops (low-of-1m / round-number), sell-into-strength resting-limit ladder, first-target scale + breakeven-stop-after-partial + runner trail, marketable-limit-at-bid then market fallback, breakout-or-bailout (price-retest-fail + tape-weakness exits), max-loss circuit, profit-giveback & green-to-red halts. GAP: affirmative 'absence-of-confirming-strength within N sec -> bail' side of breakout-or-bailout; instant-bid-above-fill validation/cut read; regime-conditioned HOLD-TIME (hot=hold-through-red-longer / cold=cut-quicker) — only the size half exists.

SELECTION ~70%. STRONG: explosive scorer (RVOL/gap/float/price-band), score_universe + identify_leader, catalyst grading, EDGAR dilution/secondary veto, second-day continuation / day-3 exhaustion, float-rotation re-rank, family/theme regime, leader rank-displacement arming, L2/OFI tilt. GAPS: 200-EMA daily-ROOM gate (distance computed in daily_levels.dist_to_sma_200_atr but DISCARDED, never gates), midday float-OVER-rotation hands-off (deployed signal REWARDS rotation = opposite of Ross), news-PR clock cadence arming, $3-10 sweet-spot tightening, China/foreign liquidation-day & SPAC $10-anchor daily types, faded-HOD 'running-up' selection. Catalyst confirmed-buyout dead-tape upgrade-then-dead.

RISK ~80%. Risk-first qty, max-loss circuit, per-broker daily-loss caps + kill switch, drawdown breaker, streak/cushion/prior-day-PnL dampers, daily-trade-count budget (base 5 = Ross, LIVE+ENABLED — multiple prior audits WRONGLY said removed), adaptive post-loss cooldowns, 2-strike same-day symbol block, no-averaging-down (pyramid fires only on banked cushion + new-HOD + OFI, verified cannot add underwater). GAPS: rule-break -> no-trade-NEXT-day lockout, time/decision-fatigue session-duration derate, pyramid add gated by CONTINUOUS cushion not a fresh DISCRETE sub-pattern (HVM101), consecutive-halt-down liquidation-cascade dip-buy standdown.

PSYCHOLOGY ~55% (lowest — but most is intentionally non-mechanizable). Deterministic substitutes EXIST for the codifiable parts: P&L-keyed size/count ladders, anti-revenge cooldowns, hot/cold & win-cycle size throttles, per-symbol fatigue (but flag default-OFF = inert). GAPS that ARE codable: rule-adherence/process-over-P&L score (all ladders keyed on realized PnL, none on rules-followed), no-trade-next-day operant lockout, decision-fatigue derate, SIM->LIVE green-paper-month graduation gate, rehab 1->2->3 trade ramp. The IFS/parts-work/relationships/meditation layer (PSY101, RH101) is correctly N/A.

TOOLS ~60%. STRONG/OVERTURNED: equity L2 IS real — iqfeed_depth_bridge.py (5-level depth + imbalance, sticky-resubscribe self-heal) + iqfeed_trade_bridge.py (per-trade tape) feeding _l2_entry_veto + signed_tape_accel; crypto L2 via Coinbase WS ring. So 'finish L2' is NOT the gap prior audits implied. GENUINE GAPS: equity has only AGGREGATE 5-level depth, not the per-TIER stack Ross reads ('19-18-17 thinning then snap') — the big-seller veto is functionally DEGRADED by this; bright-green ask-eaten/price-lifting print not a discrete signal; hidden-BUYER (accumulation) confirmer absent; 8am top-of-hour order-burst candle-distortion guard absent; dark-pool DMA routing out of reach by broker constraint (RH/Alpaca/Coinbase). Host-daemon live UPTIME unverifiable from code (soak item).

## Honest verdict (incl. FALSE gaps from stale memory)
Honest, calibrated verdict. The deployed momentum_neural lane is substantially MORE Ross-complete than prior audits' raw gap counts implied — and several headline 'gaps' were FALSE, caught only by reading deployed code against stale memory. The three biggest corrections: (1) equity entry is a MARKETABLE-LIMIT with re-peg chase, NOT spread-crossing MARKET orders (the '0-fills' memory note is stale); (2) equity + crypto L2/tape are REAL, hardened, consumed daemons (iqfeed_depth_bridge.py / iqfeed_trade_bridge.py / Coinbase WS ring), NOT 'incomplete/None'; (3) the ~5-trades/day budget is LIVE and ENABLED, not 'removed' (this error recurred across FOUR audits — a memory-contamination pattern worth fixing). Two prior-audit reclassifications also held against me: cup-and-handle is fully implemented (flag-off), and the curl entry is covered by composition and is Ross-faithful as a fall-through.

That said, the gaps that survived adversarial verification are REAL and concentrated. The single cleanest MISSING entry is the WEDGE/converging-trendline break (HVM101, ~10k transcript chars + the f024 apex frame, Ross calls it 'the most advanced' entry) — no geometry exists anywhere, high priority. The most-repeated MISSING tell across SCAL101/SS101-029 is the absorption family — the SOAKER (clustered bottoming-tails = support strength) and the absorption-then-SNAP / eaten-seller LONG trigger — currently coded ONLY as a veto, never as a reason to fire (Mishka & Ross both say 'I take that trade all the time'). The most STRUCTURAL tools gap is that equity L2 is only AGGREGATE 5-level depth, so the big-seller veto is functionally DEGRADED — it cannot see the per-tier '19-18-17 thinning then snap' stack Ross actually trades; per-level equity ladder is the highest-leverage tools build.

Calibration caveats: percentages are intentionally conservative; flag-default-OFF or inert logic (cup-and-handle, per-symbol fatigue) is counted as PARTIAL, which is the operator's 'no dark flags' standard — flipping those on is cheaper than any new build and should precede new work. Host-daemon live UPTIME cannot be confirmed from code (the real residual of L2 task #6 is a SOAK, not a build). And the PSY/RH 'low' coverage % is by design — most of it is correctly non-mechanizable, so the headline number understates the actionable completeness. Net: entries/exits/risk are in good shape; the durable alpha is in (a) absorption/soaker + eaten-seller entries, (b) wedge geometry, (c) per-tier equity L2, (d) turning the 200-EMA room-distance that is ALREADY COMPUTED into an actual selection gate, and (e) flipping the two inert flags.

## Solidly covered — DO NOT REBUILD
DO NOT REBUILD these — verified in deployed code (not name-only), to avoid the prior duplicate-near-miss:

ENTRIES: cup_and_handle_confirmation (entry_gates.py ~3980, FULLY implemented per SS101-016: rim/handle/9-EMA/first-new-high/volume — just flag default-OFF, flip don't rebuild); curl entry (COVERED BY COMPOSITION — Ross says the curl trigger IS the 1m-pullback-new-high, which is pullback_break_confirmation, resolving from inverse_head_shoulders_confirmation:3796 / ross_double_bottom_confirmation:3653, with curl_score + _curl_rank_bonus selection tilt — the 'fall-through' is Ross-faithful); flush_dip front-side/bottoming-tail/reclaim (2906-3045); hod_break (4828/4853), blue_sky (5158), ross_abcd (3469), red_to_green (5648), wick_reclaim, vwap_reclaim, halt_resume_dip_trigger (6514), first_pullback_break/continuation_high_conviction (Mishka 2nd/3rd-leg-up).

EXITS: risk-first sizing + 2:1 floor, sell-into-strength resting GTC limit at first target (paper_execution.py:1488/1823), breakeven_stop_after_partial (live_runner.py 1630-1671/1738-1763), runner trail, marketable-limit-at-bid->market fallback (769-904), round_number_first_scale_target (paper_execution 207), breakout-bailout price-retest-fail (6760) + tape-weakness exits, max-loss circuit, profit-giveback halt, green-to-red halt.

EXECUTION/TOOLS (heavily overturned — prior audits trusted stale memory): equity MARKETABLE-LIMIT entry capped at guarded_ask with bounded re-peg chase (live_runner.py 6088-6217 — NOT market orders; the '0-fills MARKET crosses spread' memory note is STALE); crypto post_only maker-at-bid (6237); equity L2 depth feed iqfeed_depth_bridge.py + tape iqfeed_trade_bridge.py (REAL, hardened, consumed by _l2_entry_veto + signed_tape_accel_features); crypto Coinbase WS L2 ring (live, NOT None as memory claimed); _l2_entry_veto big-seller-wall + hidden-seller-absorption legs; OFI/micro-price tilt.

SELECTION/RISK: explosive scorer + score_universe + identify_leader, EDGAR dilution/secondary-offering veto (edgar.py — Mishka's offering-tanks-the-pump warning, COVERED), daily-trade-count budget (risk_policy.py:605, base 5 = Ross's '5 trades/day', default ENABLED, LIVE-WIRED at live_runner.py 6292-6303 — prior audits REPEATEDLY mis-stated this as removed; it is present and active), per-broker daily-loss caps + kill switch (check_daily_loss_breach governance.py:771 — note prior audit cited a non-existent symbol _check_global_daily_loss_cap; substance is correct), streak/cushion/prior-day dampers, no-averaging-down (verified), hot/cold + win-cycle + per-symbol-fatigue size throttles, second-day/day-3-run daily context, time-of-day clock policy (hot/midday/late hard-blocks + midday-lull bar-raise + crypto-session gate — a deliberate CLOCK choice matching Ross's fixed 8-10:30, NOT a missing learned model; the learned regime dial 'measured WORSE', rejected).

## Discretionary — DO NOT mechanize
Mostly/entirely DISCRETIONARY — do NOT attempt to mechanize (would burn build effort on non-codable color):

1. PSY101 (Trader's Mindset, 13 modules) and RH101 (Trader Rehab) are ~90% pure operator psychology: IFS/parts-work, radical acceptance, resource-states, relationships, guided meditation, emotional self-awareness, rapid bias-shift detection. Frames are confirmed slide-decks + Zoom talking-heads + a workbook PDF with ZERO charts/scanners/L2/executions. CHILI's deterministic gates (size dampers, cooldowns, kill switch) ARE the substitute for an autonomous system; this is the correct architecture, not a gap. Only the FEW concrete codable rules inside these courses (no-trade-next-day lockout, decision-fatigue derate, rule-adherence score, graduation gate) are listed as gaps — the rest is N/A.

2. The pure trader-PSYCHOLOGY framing of otherwise-codable rules: e.g. AS101 'a dip is never my first trade of the day' has a codable kernel (prior-trades-on-symbol gate, listed low-priority) but its mindset wrapper is not.

3. Dark-pool DMA order routing (AS101 module-004: hit dark-pool 1->2 do-not-reroute->3->lit, quarter-position sweep at the ask) is NOT discretionary but is OUT OF REACH by broker constraint — RH/Alpaca/Coinbase lack Lightspeed-style DMA. Only the maker-only/post-only/sell-at-ask PRINCIPLE is portable (and is ported for crypto). Do not build; it is unbuildable on current brokers.

4. SS101 modules 102/103/104 (taxes, broker choice, Roth) and AS101 '10-15c/day goal mindset' = out of engine scope entirely.

5. Fine execution-mechanism parity that achieves the same RISK outcome by a different path is NOT a gap: offset-LIMIT-via-Ctrl-on-the-ladder (TOS101) == CHILI's marketable-capped-limit; PFOF microscalp (SS101-071) intentionally out of scope.

## Verified REAL gaps (per course)


### Adversarial re-verification of the 4 PARTIAL/near-MISSING items from t

- **[MEDIUM] Dip-buy / algo-flush 'works between 9:30 and 4 because that's when stop orders fire (no stops pre-market)' — RTH-only ti** (entry)
  - CONFIRMED absent. entry_gates.py:flush_dip_buy_confirmation (line 2906) carries an unused `now` param and has ZERO session/clock check anywhere in its body (lines 2940-3047); the call site live_runner.py:4193 even omits `now`. The only related time-gated dip-buy, `_evaluate_deep_reclaim` (entry_gates.py ~640-668), gates the WRONG direction: it imposes a morning UPPER cutoff (~10:30 ET) and its own

- **[LOW] News-release clock cadence (top/bottom of the hour pre-market: 7:00/7:30/8:00/8:30) as a WATCH/ARM trigger** (selection)
  - CONFIRMED absent. ross_momentum.py:news_catalyst_signal (line 1446) maps a symbol's catalyst grade/presence to a [0,1] sub-score only — no time-of-release dimension. Grep of momentum_neural for minute==0/==30/top_of_hour/half_hour/news_window found no clock-phase logic. auto_arm.py has no news-driven arming (only the passive score tilt). Transcript 005 verbatim: 'News PRs are typically released at

- **[LOW] Dip-buy discretion: 'a dip trade is never my first trade of the day on a name — only after I've already taken trades on ** (psychology)
  - CONFIRMED absent. Grep of momentum_neural for first_trade/prior_trade/prior_win/trades_on_symbol/prior_pnl_on/been_winning returned NO matches — no dip-family gate (flush_dip, deep_reclaim, micro_pullback_primary) conditions on prior trades/PnL on the same symbol. Transcript 005 verbatim: 'a dip trade has probably never been my first trade of the day... I'll only start doing dip trades if I've alr

- **[LOW] Aggressive dark-pool order routing ladder (hit dark-pool 1 → 2, do-not-reroute-to-lit, then 3, then lit; quarter-positio** (tools)
  - CONFIRMED absent and correctly scoped as out-of-reach. Grep of services/trading/venue/ for darkpool/crossfinder/midpoint-peg/do_not_reroute/ping-dark returned NO matches; the 'route' hits in robinhood/alpaca adapters are the brokers' own smart-route fields, not DMA dark-pool pings. CHILI's brokers (RH/Alpaca/Coinbase) lack the Lightspeed-style DMA dark-pool access module-004 teaches, so only the m

  - _missed-by-audit:_ ONE genuine miss: 'large BUYER stacked on the bid near a half/whole dollar' as a dip-buy STARTER confirmer — the L2 bid-side mirror of the seller-veto the audit did capture. Transcript 005 verbatim: 'I'll sometimes take a starter on a dip if I see a large buyer on the bid... at 651 there's a 20,000-


### HVM101 gap-verification: adversarially re-checked the 4 non-COVERED au

- **[HIGH] WEDGE / converging-trendline break (3+ taps on BOTH highs and lows, coiling, body/wick FILLS the wedge at the apex = bre** (entry)
  - Confirmed GENUINELY ABSENT. Grep of momentum_neural for wedge|trendline|trend_line|apex returns ZERO geometry in any entry gate AND zero in ross_momentum.py (not even a selection signal). The only adjacent thing is hod_break_confirmation(flat_top=True) at entry_gates.py:4853-4974, which is a FLAT (horizontal) resistance with 2-3 topping-tail taps + round-number context — that is the opposite of a 

- **[MEDIUM] Pyramid ADD anchored to a discrete NEW sub-pattern (Ross: 'I do NOT scale in — one entry and scale out... I would buy th** (risk)
  - Confirmed PARTIAL/real. paper_execution.py:549 pyramid_add_decision correctly FORBIDS averaging-down (requires banked cushion >= min_cushion_r*R0, stop>=breakeven, new-HOD, OFI thrust, non-decreasing trail, no-iceberg) and DOES carry a max_adds cap param — but the fire condition is a CONTINUOUS cushion+confirmation threshold, NOT 'a new discrete sub-pattern (cup-and-handle/wick-reclaim) has formed

  - _missed-by-audit:_ Reading the transcripts surfaced detail the original audit under-specified but did not outright miss as techniques: (1) RISING-vs-FALLING wedge directional bias (Ross: 'a rising wedge has higher odds of failure; a descending/lower wedge is stronger') — if/when a wedge gate is built it must encode th


### PSY101 "Developing the Trader's Mindset" (13 modules) — adversarial re

- **[MEDIUM] Rule-break consequence / no-trade-next-day lockout (operant conditioning — Mod 10's central teaching)** (risk)
  - CONFIRMED ABSENT. Greps for no.?trade.?next|rule.?break.?consequence|lockout|trade_ban|tomorrow across the whole trading tree returned only 3 hits, ALL unrelated comments (ignition_loop.py:235 'tomorrow's replay', replay_regression.py:8, tape_ws_recorder.py:6 — replay data-prep prose, NOT a consequence mechanism). The drawdown breaker blocks-until-manual-reset on a P&L threshold, but there is NO a

- **[MEDIUM] Time/decision-fatigue session-duration derate (decision quality degrades over continuous trading time / trade count — si** (risk)
  - CONFIRMED ABSENT. No elapsed|since_first_entry|session_minutes|fatigue-derate|late_session matches in risk_policy.py; no decision_fatigue|session_duration|hours_traded|intraday_fatigue matches anywhere in the trading tree. daily_trade_count_budget_decision bounds the COUNT of entries and adaptive-clock bounds the WINDOW open/close, but nothing models decision-quality decay as a function of elapsed

- **[LOW] Per-symbol attempt fatigue is INERT in prod (flag default-off) — violates the operator's 'no dark flags' principle** (selection)
  - CONFIRMED PARTIAL. The logic is genuine and fully wired: _per_symbol_fatigue_level (auto_arm.py:1659) does 3rd-attempt RED veto + 2nd-attempt YELLOW size-down; per_symbol_fatigue_size_multiplier is composed into entry sizing at live_runner.py:6001-6011 and per_symbol_fatigue_blocks_entry gates the arm path at auto_arm.py:2959-2964. BUT chili_momentum_per_symbol_fatigue_enabled defaults False (conf

- **[LOW] Process-over-profits scoring (grade a session on rules-followed independent of P&L; a 'win' = followed rules)** (psychology)
  - CONFIRMED PARTIAL. outcome_labels.py classifies terminal outcomes (success/stop_loss/bailout/governance_exit/cancelled/no_fill...) and is_real_entry_outcome distinguishes entered-vs-never-entered, and feedback_emit logs gate decisions — but there is NO first-class rule-adherence/discipline score. Every size ladder (streak_risk_multiplier, cushion_risk_multiplier, daily_trade_count expectancy_mult,

  - _missed-by-audit:_ One technique the audit under-extracted (it split the evidence awkwardly across two findings): Mod 010's SYMMETRIC rule-following operant ladder — 'If I'm following my rules and having success, I get to take MORE trades, increase share size, increase targets. If I break a rule / not following my rul


### RH101 "Trader Rehab" (Warrior's psychology/discipline + guided-meditat

- **[LOW] SIM->LIVE graduation discipline: trade in the simulator / negligible size until a defined strategy posts a GREEN PAPER M** (psychology/discipline)
  - CONFIRMED ABSENT. operator_readiness.py build_momentum_operator_readiness gates live ONLY on flags + broker connectivity/trade-scope (coinbase_can_trade, robinhood_connected, scheduler role) — there is NO performance-metrics graduation gate (no green-paper-month requirement, no accuracy/PLR threshold) blocking live arming. Grep for accuracy|win_rate|profit_loss_ratio|require.*green|metrics_gate in

- **[LOW] 'Rule-break / red day -> take the NEXT day off' forced forward cooldown (chronic-discipline reset). Confirmed in 022 ('i** (psychology/discipline)
  - CONFIRMED ABSENT. All cooldowns in auto_arm.py are SHORT INTRADAY ones — _reap_cooldown (300s, oscillation-scaled), _entry_reject_cooldown (900s), _adaptive_loss_cooldown_minutes (post-loss minutes scaled by loss bps), and the 2-strike SAME-DAY symbol block (_symbol_loss_guards, max_stops=2). governance daily-loss-breach + kill-switch halt the REST OF THE SAME day and auto-clear. NONE impose a for

- **[LOW] Number-of-trades REHAB MODE: literal 'one trade a day' to start, then 1->2->3 only after a clean/green stretch (intermit** (risk/discipline)
  - CONFIRMED PARTIAL. risk_policy.daily_trade_count_budget_decision (lines 605-695) is an ADAPTIVE CEILING: base 5 * heat_mult(cushion) * expectancy_mult(recent win_rate), clamped [base, base*2]. It tightens when cold and is wired live, but the FLOOR-reference base is 5 (not the rehab '1'), the ceiling never goes BELOW base, and there is no state machine implementing the literal 1->2->3 ramp gated on

- **[LOW] Know-your-METRICS surface + journaling/recap cadence: per-strategy accuracy (first) then profit-loss ratio, recorded/rev** (psychology/tools)
  - CONFIRMED PARTIAL. The lane CONSUMES its own realized outcomes for self-relative dials (streak_risk_multiplier and daily_trade_count expectancy_mult both compute recent win_rate over MomentumAutomationOutcome via is_real_entry_outcome) — so accuracy/expectancy IS measured and fed back into sizing/count. GAP unchanged: no operator-facing per-strategy accuracy/PLR dashboard or journaling surface, an

  - _missed-by-audit:_ Frame 004/f010_01779s.jpg shows a PDF page the prior audit did not transcribe in full: the Trader-Rehab workbook lists 'What is PROHIBITED: Adding to initial entry' — i.e. an explicit NO-AVERAGING-DOWN / no-adding-to-a-loser rule, and 'What is my focus: A quality setup, One Entry One Exit, Taking pr


### SCAL101 (Warrior Scalping, Max Mishka) — adversarial re-verification o

- **[HIGH] SOAKER — multi-bar clustered-bottoming-tail support-strength detector (3-6 stacked bottoming tails / red-green hammers a** (entry)
  - VERIFIED ABSENT. flush_dip_buy_confirmation (entry_gates.py:2906) keys on a SINGLE flush bar (flush_idx = cur - 1) plus one curl bar; _bottoming_tail (entry_gates.py:2884) is per-bar shape only. grep for soak/clustered/stacked/consecutive across momentum_neural returns ZERO matches in entry_gates.py and ross_momentum.py — the only 'soak' hits are unrelated (alpaca validation-soak, and the SELL-sid

- **[MEDIUM] Eaten-seller-squeeze ENTRY (big resting ASK getting EATEN by green tape → level breaks → trapped-short squeeze = a posit** (entry)
  - VERIFIED ABSENT as an ENTRY. The deployed L2 logic (_l2_entry_veto, entry_gates.py:1047) is VETO-ONLY: it has a big-SELLER-wall leg (depth-imbal percentile floor) and a hidden-seller absorption leg (micro-edge<0 vs OFI>0). It never reads 'a big ask being consumed then broken' as a reason to FIRE. grep for eaten/squeeze.*entry/ask.*eaten/big_bid.*support/short_trap in entry_gates.py = zero matches.

- **[LOW] Market-condition (hot/cold) HOLD-TIME modulation — hold through red LONGER in a hot market, cut QUICKER in a cold market** (risk)
  - VERIFIED ABSENT. grep for hot.*market/cold.*market/hold.*longer/hold_time/cut.*quick in risk_policy.py = zero matches. risk_policy has cushion-scaled SIZE multipliers and daily_trade_count regime sizing, but exits are purely structure/trail driven with no regime-conditioned hold-time / stop-tolerance knob. Mishka 002 opens the entire risk chapter with this: 'in a hot market I will definitely hold 

  - _missed-by-audit:_ The audit MISSED four concrete Mishka techniques: (1) SECONDARY-OFFERING / dilution selection veto (Mishka 003 warns at length that reverse-split gappers get a 'secondary offering by the company... that just tanks the stock') — this IS covered by edgar.py (dilution_risk_symbols), so it's a missed-bu


### TOS101 (Warrior course taught by Manoli; complete Warrior/Ross momentu

- **[MEDIUM] Halt resumption-IMBALANCE / flat-vs-gap resumption-PRICE read + modeled FALSE-HALT reversal entry** (entry/halts)
  - Confirmed by reading the code. Halts module (009) teaches the core tell: read the exchange resumption PRICE (halted-down + FLAT resumption = bullish 'selling done'; halted-up + flat = bearish) and trade FALSE HALTs (ask wall stacks to 140k then collapses to 18k -> violent reversal; he buys it with a LIMIT on the ladder). Deployed lane has NONE of this: live_runner.py:_register_stale_quote_tick / _

- **[MEDIUM] Midday float-OVER-rotation hands-off veto/derate (a name that has ALREADY turned its float multiple times by midday = 't** (selection/timing)
  - Confirmed by reading ross_momentum.py:float_rotation_signal (lines 1261-1326). TA module (005) is explicit: 'I don't want to be trading something midday that has already rotated its float multiple times... the moves have already been made, easy to get chopped up... I go completely hands off.' The deployed signal does the OPPOSITE shape: it rewards PROJECTED-rotation-to-EOD toward the SS101 5x satu

- **[LOW] Volume-magnitude-conditioned overhead-supply selection veto: a prior huge-volume doji day that 'gave it ALL back' makes ** (selection)
  - MISSED by the audit (not in its 16-technique list). Trading Edge module (004) teaches it explicitly as a daily-history filter. daily_levels.py models adjacent ideas — _red_rejection_history (repeated upper-wick seller-defense at a LEVEL), _day_number_in_run (day-3+ exhaustion derate), overhead_supply_atr — but NONE condition the overhead-supply strength on the VOLUME that was traded into the prior

  - _missed-by-audit:_ (1) The volume-magnitude-conditioned overhead-supply veto above (Trading Edge: prior huge-volume doji round-trip -> near-untouchable level). (2) The 'bid pops up 3-20c ABOVE my fill the instant I'm in = instant trade-validation, otherwise cut immediately' confirmation read (Breakouts/Dips modules, s


### Adversarial re-verification of the SS101-a audit's PARTIAL/MISSING cla

- **[MEDIUM] Bullish absorption-then-SNAP entry (hidden-seller-bought-up 'dam breaks' trade)** (entry)
  - SS101 module 029 (which the audit never read — it stopped at 026) teaches Ross's SIGNATURE L2 entry explicitly: a hidden seller / resting wall absorbs buying ('green prints but price won't advance'), then 'finally it breaks and we go from 1029 immediately to $11, it's just instant. I love those trades, I take that trade all the time.' He ADDS into the break. In the deployed code the hidden-seller 

- **[LOW] Confirmed/definitive buyout dead-tape skip (name pinned at the deal price trades sideways — do not trade)** (selection)
  - Ross (004/007, restated 029): a CONFIRMED acquisition pins price at the deal price and 'just trades sideways like this... it's not gonna work.' In code, 'buyout','acquisition','to acquire','to be acquired','takeover','merger' are STRONG-catalyst keywords (catalyst.py:416) earning a POSITIVE boost. Only the RUMOR/unsolicited variant gets a soft de-weight (catalyst.py:_is_fake_catalyst:470 + viabili

- **[MEDIUM] Adaptive volume confirmation on the break (kill the hardcoded 1.5x multiple)** (entry)
  - CONFIRMED reachable on the LIVE default path. momentum_volume_confirmation (entry_gates.py:115-152) hardcodes 'cur_v < 1.5 * avg_v' / 'vr>=1.5' in three branches. It is the FALLBACK trigger in the default hybrid mode (config.py:3552 default='hybrid'): live_runner.py:4546 calls it when the pullback + continuation triggers do not fire, and the arm probe auto_arm.py:2002 calls it for the 15m leg. The

- **[LOW] 8 a.m. (top-of-hour) order-burst candle-distortion guard** (tools)
  - Module 029: 'something that occurs at eight a.m. ... at the top of the hour at 8am we often see this burst of orders and the candlestick charts can get really kind of screwed up.' Ross warns the 8am burst distorts the chart/trigger geometry. No grep hit for an 8am / top-of-hour burst guard in entry_gates.py. The lane has premarket/early-window handling (per memory) but no specific top-of-hour orde

  - _missed-by-audit:_ The audit MISSED an entire real teaching module by scoping to 001-026 and wrongly assuming nothing teachable exists after 019. SS101 module 029 'Part 1: Level 2 and Time and Sales' is a full, dedicated L2/tape lesson (read in full; key-frames f017 OGEN L2 'Big Bids or Big Sellers', f022 LightSpeed m


### SS101-b adversarial verification of audit PARTIAL/COVERED claims (modu

- **[MEDIUM] Add-INTO-the-halt anticipation (Ross's SAFEST halt method: as a strong name pins the LULD UP-band, add via micro-pullbac** (entry)
  - Confirmed absent. entry_gates.py has only luld_down_band (353) + halt_band_trapped (369) = the PROTECTIVE down-band veto, and halt_resume_dip_trigger (6514) = the resume DIP (Ross's SECOND-safest method). A repo-wide grep for luld_up / up_band / limit_up / add_into_halt / pre_halt across momentum_neural returns ZERO matches (only an unrelated micro_bars.py docstring). 048 transcript verbatim: 'I w

- **[HIGH] Full per-level equity Level-2 ladder (read the stack tier-by-tier: sellers thinning 19-18-17 then snap; true big-seller ** (tools)
  - Confirmed. pipeline.py:_ladder_equity (541) reads ONLY aggregate 5-level fields (bid5_size/ask5_size/imbalance5/top-of-book) from iqfeed_depth_snapshots; its own docstring (545-548) states 'No per-level arrays, but the aggregate carries the distribution signal.' Crypto (_ladder_crypto, 447) reads per-level JSONB bid_levels/ask_levels from fast_orderbook, so the per-level capability exists for cryp

- **[LOW] Adaptive volume confirmation (Ross: high volume on the push, light on the pullback; RVOL>=5x) — replace the hardcoded mu** (selection)
  - Confirmed. entry_gates.py:momentum_volume_confirmation (115) uses a HARDCODED 1.5x recent-average threshold at three sites (lines 133, 143, 150-151: 'cur_v < 1.5*avg_v', 'vr>=1.5'). Docstring line 116 literally says 'volume above 1.5x recent average.' The adaptive per-instrument _sustained_rvol (155) exists but is a SEPARATE function not used by this gate. The dip-buy push/pullback volume-contrast

- **[MEDIUM] Curling / pulling-away as a distinct SELECTION + ENTRY path (faded former-HOD re-rallying on a 'running-up' scanner, not** (selection)
  - Partially confirmed (audit nuance holds; one sub-claim softened). A curl detector DOES exist as a SELECTION RANKING TILT only: ross_momentum.curl_score (1107, rounding-bottom/cup-handle [0,1]) stamped in auto_arm._candidate_freshness (2049-2054) and read as a small additive _curl_rank_bonus (2089). candles.is_bounce_curl_candle (68) is a per-bar exit/scale confirm. BUT: (1) no distinct curling/pul

  - _missed-by-audit:_ No material technique was wholly missed by the audit. Verification confirmed two scaling/exit details the audit only asserted by name and that DO hold: (1) Ross's 'sell half at first target, then move the BALANCE stop to breakeven, hold the runner' (037) is fully implemented — live_runner.py:1630-16


### SS101-c modules 053-078 (daily stock types 053-064, gap-and-go 065-070

- **[LOW] Foreign/Chinese small-cap DAILY TYPE: explosive 10,000%-up / 99%-liquidation-in-a-day names; trade long-only as hot-pota** (selection/risk)
  - catalyst.py:705-746 catalyst_viability_delta has an hq_country foreign branch, but it ONLY grants a no-news 'room-to-speculate' boost in a HOT tape (foreign no-news gets full half-tilt). It is NOT a China/foreign daily-type detector and carries NO liquidation-risk derate. The pump-and-dump keywords (catalyst.py:465) live in the FAKE-catalyst headline credibility guard, not a foreign-name guard. Lo

- **[LOW] SPAC daily type: trades at $10 pre-merger; blue-sky above $10 on merger rumor/news; HARD resistance back at $10 (pre-mer** (selection)
  - grep for spac|special.?(purpose|acquisition)|reverse.?merger|shell.?company|qsip across momentum_neural = 0 detector hits (only SpaceX-theme + 'spacing' false matches). daily_levels.py has no $10-anchor / merger-resistance logic. Module 064 teaches the $10 floor/resistance and the post-merger history-wipe as a recent-IPO. Genuinely absent. Ross himself de-prioritizes SPACs ('lost popularity'), so 

- **[MEDIUM] Break of the PRE-MARKET PIVOT: anchor on the prior stair-step-down swing-high (the pivot), enter on the FIRST step UP th** (entry)
  - ross_abcd_confirmation (entry_gates.py:3469-3588) is a generic ATR-filtered swing-pivot ABCD coil that fires on a D-break above the B->C swing high with stop at the C low. It approximates the geometry but does NOT specifically anchor the premarket stair-step-down high as the pivot with the premarket-high as the explicit next target, has no MACD-reopen requirement, and no cold-market avoidance togg

- **[LOW] Chase-the-rising-halt-band scalp: as the LULD halt-UP level steps higher, jump in on resumption for a quick 10-15c pop t** (entry)
  - halt_resume_dip_trigger (entry_gates.py:6514-6642) buys the FIRST stabilizing DIP off a halt resumption (post-resume reference high -> dip -> reclaim). That is the opposite shape from Ross's halt-CHASE in module 072 ('right as the halt level moves up, I jump in for a quick 10-15 cent pop up to the next halt level'), which rides the upward halt-band ladder rather than buying a pullback. No discrete

  - _missed-by-audit:_ Two concrete techniques in-scope the audit under-weighted: (1) Module 062 ZJYL/HKD HALT-LADDER LIQUIDATION TRAP — Ross's $40k loss buying the dip into a series of halt-DOWNS on a Chinese liquidation day ('don't try to buy the dips on Chinese stocks ever... when halted down you're stuck in the halt, 


### Adversarial re-verification of the SS101-d audit (modules 079-104, the

- **[MEDIUM] Round-number (half/whole-dollar) ENTRY-timing gate** (entry)
  - CONFIRMED. Ross (083) teaches three concrete round-number entry tactics: (a) buy UNDER the level (e.g. 4.75-4.85 so you can exit under 5 still green), (b) wait for it to TEST/hold 5 then enter ~4.96-4.97, (c) wait for the break-and-hold-over then enter ~5.08 stop at 5 — and explicitly warns 'an entry at 4.90 is risky, 10c from resistance, could be a hidden seller.' In code, round numbers feed ONLY

- **[MEDIUM] 200-day-EMA/SMA daily-ROOM selection gate** (selection)
  - CONFIRMED + MISSED by the prior audit. Ross (083) makes this a near-hard selection criterion: 'Daily 200 EMA position is critical — the stock should be ABOVE it OR have room to go up 100% today before it runs into it; if the 200 EMA is only 10-15% away it's too close, it won't work.' daily_levels.py:500-516 computes sma_200, above_sma_200, and dist_to_sma_200_atr (signed daily-ATR units = the 'roo

- **[LOW] $3-10 small-account price sweet-spot tightening + lower-price-for-share-count nuance** (selection)
  - CONFIRMED (audit PARTIAL upheld). universe.py:74-99 EQUITY_ROSS_SMALLCAP hard-codes price_min=1.0/price_max=20.0 uniformly with no tightening to Ross's stated $3-10 (083) / $1.50-$6 (089 f008) sweet spot, and no share-count-maximization preference for the lower band. Ross's rationale (083): 3-10 = tight-enough spreads for risk mgmt AND enough range for a 50c move; sub-3 'don't even give you 10c ea

- **[HIGH] Real tick-level L2 book/flow as the execution lens (esp. crypto)** (tools)
  - CONFIRMED (audit PARTIAL upheld). L2 IS consumed (entry_gates.py:1047 _l2_entry_veto + OFI/micro-price tilt), but per the lane's own open task #6 'Make IQFeed L2 real (tick book/flow)' and MEMORY, the crypto Coinbase WS level2 ring buffer returns None in the scheduler process so crypto decides without L2, and the equity depth is snapshot- not full tick-flow. Ross (083) executes the micro-pullback 

- **[LOW] Stop-trading-after-a-red-day (full halt, not just size-down)** (psychology)
  - CONFIRMED as a design divergence / partial gap. 089 frame f008 (Day 1-10) states verbatim 'If I have a red day, I have to STOP RIGHT AWAY and not overstay my welcome' and 083 hammers 'one trade a day → one loss, that's it, walk away.' Code response: prior_day_pnl_damper_multiplier (risk_policy.py:760) only SIZES DOWN after an OUTLIER prior red day (and is symmetric on big greens); it never benches

  - _missed-by-audit:_ Two concrete Ross techniques the audit failed to extract: (1) The 200-EMA daily-ROOM selection gate (083: 'above the 200 EMA, OR room to run ~100% before hitting it; if only 10-15% away it won't work') — the distance is computed (daily_levels.dist_to_sma_200_atr) but never gates selection (see confi
