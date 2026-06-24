# Ross-live vs CHILI — running insights log (2026-06-24)

Accumulated LIVE during Ross Cameron's Warrior premarket/RTH stream + cross-referenced against CHILI's
momentum lane (chili-app:main-clean-4e57382). Goal: synthesize into prioritized CHILI upgrades AFTER market.
Each entry: what Ross did, what CHILI did, the gap, the proposed lever.

---

## ⭐ #1 CASE STUDY — PLSM (Pulsenmore): Ross +$10,330 / CHILI −$8.08 — SAME NAME, opposite outcome

**The setup:** PLSM +458% on the day ($3 → $19+) on a NEWS catalyst — "Pulsenmore Strategic Partnership
with Ouma Health to Expand Remote Prenatal Care" (07:00 ET). Parabolic low-float runner.

**Ross (winning):** entered the $16–18 range, **1,438 sh (~$25k+ notional)**, scaled in (bought $16.32–18.89)
+ took profits ($18.87–19.20), **Realized +$10,330.85 + Open +$169** on PLSM. Knew the catalyst → conviction
→ held through volatility + pyramided/re-entered.

**CHILI (losing):** ONE entry at **$10.21** (breakout_level was $7.63 → entered ~34% extended above the level),
tiny **$163 notional (~16 sh, capped by notional_ceiling)**, tight **$0.55 stop (5.4%)**, **structural_pullback**
stop model. Got shaken out → **−$8.08 in 4.5min** (session 8613, live_cooldown). entry_features at the bad
entry: trade_flow=-0.001 (weak/neutral tape — NOT confirming), book_imbalance=0.046 (neutral), ofi=1.0,
spread_bps=122.6 (WIDE 1.23%), above_vwap=1.0, premarket=1.0. Then PLSM continued to $19 — CHILI missed it.

**The gaps / LEVERS (prioritized):**
1. **NEWS-CATALYST CONVICTION (the #1 lever, seen twice today: CALC + PLSM).** CHILI is blind to the small-cap
   catalyst (Ouma partnership) that drove +458%. No conviction → tight stop → shaken out → missed the runner.
   Ross's News Desk gave him the catalyst → he held + scaled. FIX: small-cap-catalyst news feed/filter (Benzinga
   small-cap PR/partnership/FDA), wire as a CONVICTION input (size up + wider stop tolerance + hold-through when a
   FRESH catalyst is present). CHILI's catalyst set currently has NONE of Ross's movers (large-cap-skewed).
2. **SIZE / notional ceiling too small.** $163 notional on a ~$8.6k account (1.6%) vs Ross ~$25k. Even a win is
   pennies. FIX: revisit the notional ceiling for high-conviction catalyst names (equity-relative, not a tiny cap).
3. **TIGHT STOP shaken out on a parabolic.** $0.55 stop (5.4%) on a +458% mover = stopped by normal noise, then
   it ran. FIX: volatility/ATR-aware + catalyst-aware stop (wider for a high-conviction news runner), OR a
   structural stop below the pullback low, not a fixed 5.4%.
4. **CHASED — entered 34% ABOVE the breakout level ($10.21 vs $7.63).** Late/extended entry → bought near a local
   top → stopped on the pullback. FIX: an extension/chase guard (skip or wait-for-pullback when price is already
   X% extended above the trigger level); enter near the break or the first pullback, not vertical.
5. **NO scaling / re-entry (task #8 pending).** CHILI does one shot + one stop. Ross pyramids + re-enters to ride
   the runner. FIX: pyramid_max_adds + re-entry-after-stop when the trend resumes.
6. **CHILI's own signals SAW it but didn't act:** trade_flow=-0.001 (weak tape) + spread 122bps at entry were
   warning signs; they're meta-label features (inert) + the tilt only adds on agreement. Once the meta-label has
   data, these should down-rate such chasey/wide-spread entries — watch that it learns this.

---

## ⭐ #2 CASE STUDY — FRTT + PLSM: Ross scalps EVERY pullback (concurrent); CHILI watches but doesn't convert/re-enter

**Operator live obs:** Ross traded a LOT — scalped EVERY pullback on FRTT; said he MISSED FRTT's FIRST
explosion + only scalped a little after; he's running PLSM AND FRTT concurrently. "Bakit walang ginagawa
si CHILI?"

**CHILI snapshot at that moment:** NOT idle — PLSM `live_scaling_out` (has a position) + FRTT, ABSI, WEN,
CALC, EHGO all `watching_live`. FRTT IS seen (viability 0.688, live_eligible) + watched concurrently with
PLSM. So selection + concurrency WORK. Realized today: PLSM −8.08, CALC −2.50.

**The gaps (why CHILI under-trades the runners Ross scalps):**
1. **PULLBACK-BOUNCE ENTRY missing.** CHILI sits in `watching_live` on FRTT — its entry trigger waits for
   ITS break pattern and doesn't fire on the pullback-and-go bounces Ross buys. This is the long-standing
   conversion gap (watch → no entry). FIX: a pullback/dip-bounce entry trigger (buy the higher-low bounce
   off a rising EMA/VWAP after the first push), not only the breakout. (Ross's bread-and-butter: first
   pullback after the break.) Maps to pending task #7 (dip-rip/VWAP-reclaim playbook).
2. **NO RE-ENTRY / SCALP-EVERY-PULLBACK / PYRAMID.** Ross takes MANY entries per name (each pullback) +
   pyramids the winner; CHILI does ONE entry then watches. FIX = pending task #8 (pyramid_max_adds +
   re-entry-after-exit when the trend resumes). Without it CHILI can't compound a runner like Ross's
   +$10,330 PLSM.
3. **DON'T MISS THE FIRST EXPLOSION** (operator's explicit ask). Ross himself missed FRTT's first pop —
   so even he values catching it. CHILI should catch the FIRST break (fast tick-entry) AND then scalp the
   pullbacks. Combine #1 (pullback entry) + a fast first-break entry + #2 (re-entry) so CHILI rides the
   WHOLE move (PLSM + FRTT) instead of one small scratch.

**Net:** CHILI's selection/concurrency is fine (sees + watches FRTT & PLSM together). The lever is EXECUTION
on the runner: pullback-bounce entry + re-entry/pyramid-every-pullback + a fast first-break catch. This is
the #2 upgrade theme after news-conviction (#1). Both compound: news-conviction tells CHILI WHICH runner to
commit to; pullback-scalp/re-entry tells it HOW to extract the move like Ross.

## ⭐ #3 CASE STUDY — PLSM RE-ENTRY into NEGATIVE FLOW (OFI=-1.0, trade_flow=-0.51): "pumasok kung kelan walang momentum"

**Operator live obs:** "pumasok naman si CHILI sa PLSM kung kelan walang momentum... ano yan?" — CONFIRMED by the entry_features. This is the sharpest, most-actionable finding of the day.

**CHILI session 8616:** re-entered PLSM @ **$11.56** (11:46:27), now `live_trailing`. Entry microstructure:
**OFI = −1.0** (MAXIMUM sell-side order-flow — entire book offering), **trade_flow = −0.51** (sellers
aggressing the tape), book_imbalance = +0.211 (mildly bid-heavy — STALE / lagging the flow), spread 90bps,
admission_viability 0.67. = CHILI bought a LONG straight INTO maximal selling flow (a fade / falling knife).
Exactly "walang momentum" — it's NEGATIVE momentum. (The earlier 8613 entry $10.21 lost −$8.08; this re-entry
chased the FADE, not a thrust.)

**WHY it entered anyway — 3 compounding causes:**
1. **The L2 seller-veto keys on the WRONG signal.** `chili_momentum_entry_l2_veto_enabled` gates on
   `book_imbalance` (static top-of-book = +0.211 → PASSED) — NOT on the FLOW signals (OFI/trade_flow) that
   were screaming SELL. It watches the lagging static book, not the leading flow.
2. **The extreme-mover guard SKIPS the bearish-OFI discount.** viability.py: ross_scores≥0.8 (PLSM +458%)
   skips the bearish-OFI penalty ("never penalize the explosive tail") → viability stayed 0.67 despite
   OFI=-1.0. CORRECT for SELECTION (keep watching the runner) but WRONG for ENTRY-TIMING (don't BUY this
   exact tick into -1.0 OFI). The guard conflates "keep on the watchlist" with "buy now".
3. **OFI/trade_flow only TILT selection + are INERT meta-label features** — neither GATES the entry trigger.
   Nothing blocked a long into active selling.

**THE LEVER (#3 — immediate + sharp): an ENTRY-TIME FLOW VETO.** At the trigger tick, if OFI ≤ strong-negative
AND trade_flow ≤ negative (tape actively selling), DEFER/skip the BUY even on an extreme mover (keep it armed +
watching — just don't buy into the flush; wait for the flow to flip back positive = Ross's "wait for the tape
to turn"). Distinct from the static book_imbalance veto. KEY PRINCIPLE: separate **selection** (keep on the
watchlist, never-penalize-the-tail) from **entry-timing** (buy this tick → MUST respect live flow). This is the
most direct fix for "entered when there's no momentum" AND it complements the pullback-entry design (#2): the
pullback-bounce confirmation IS "wait for OFI/trade_flow to flip back up off the dip" — same flow gate.

## ⭐ #4 CASE STUDY — THE BIGGEST LESSON: Ross BANKS +$9,487 then SITS FLAT; CHILI OVERTRADES the cooldown

**Snapshot ~08:15 ET:** Ross FLAT (PLSM pos 0, FRTT pos 0), **Realized +$8,921.72 PLSM + $565.37 FRTT = +$9,487
today**. His 5-Pillars Scan = **"No qualified trading opportunities."** PLSM round-tripped $19 → $10.06. Ross
caught the move EARLY (cost basis $16.95, scaled in/out, sold the top), banked, and is now WAITING in cash.

**CHILI in the SAME window:** −$44 realized, and DURING this cooldown it FORCED entries — RUN chase (+20%
above break), PLSM re-entry $11.56 into OFI=−1.0 — exactly when the prime move was done and nothing qualified.

**THE LEVER (#4 — regime/cooldown gate):** Ross's edge today was as much knowing WHEN TO STOP as the entries.
When the prime movers are FADING (PLSM −50% off highs) AND no fresh A+ setup qualifies (CHILI's viability
equivalent of Ross's "no qualified opps"), CHILI must STOP INITIATING new entries (manage existing only) and
sit in cash. CHILI has no "the move is done, sit" discipline → it overtrades the chop, bleeding small. Maps to
task #4 (event/structure abandonment, kill the magic clock) + #7 (setup-selector). PAIRS with news-conviction
(#1): commit BIG early when the catalyst is fresh (Ross +$8,921), then STOP when it's spent — the inverse of
CHILI dribbling small + late + into the fade.

**SELECTION confirm (RUN divergence):** RUN (float 230M = large-cap, Sunrun) was on the HOD-momentum scanner
(+20%) but Ross did NOT trade it — he only traded low-float catalyst names (PLSM 50M float, FRTT 2.52M float).
CHILI traded RUN = a large-float divergence (same class as the WEN quirk). Tighten the LIVE-ENTRY universe to
low-float catalyst movers; large-float names can watch but should not consume live entries.

## ⭐ #5 — LIVE VWAP-RECLAIM (Ross says PLSM is the only bull, waiting for the reclaim) — validates the #2 design + a stuck-session gap

**Ross live (operator relay + stream audio):** "PLSM lang ang mukhang optimistic/bullish ngayon, di babagsak" — he is WAITING for PLSM to RECLAIM VWAP before buying. PLSM ~$10.36 (bid 10.36/ask 10.39, +204% day) is BELOW its VWAP ~$11.73 (his 5m chart). His trigger = a cross back ABOVE VWAP (momentum resuming up).

**CHILI cross-ref — two gaps:**
1. **No VWAP-RECLAIM entry trigger.** CHILI uses `above_vwap` as a binary gate/feature, NOT a reclaim-CROSS trigger. So even if PLSM climbs back through VWAP, CHILI has no entry that fires on the reclaim. This is EXACTLY what the #2 pullback-entry design (`wswja5d4k`, vwap_reclaim approach) builds — Ross doing it live RIGHT NOW validates the direction.
2. **Stuck-session recycling gap.** CHILI's PLSM session is in `live_error` (~36 min, from the −$33 loss) while PLSM is STILL eligible (viability 0.671, live-eligible). The errored session is not recycled back to `watching_live`, so CHILI is not even tracking PLSM for the reclaim. Even with a VWAP-reclaim trigger, a stuck terminal session could block the re-arm. FIX (add to the build list): recycle a live_error/live_cooldown session back to watching when the name is still fresh+eligible (with the reap-cooldown so it doesn't instantly re-chase).

This pairs with #1 (news-conviction: PLSM's Ouma catalyst is why Ross has conviction it "won't crash") and #2 (the VWAP-reclaim entry). The lane needs BOTH the reclaim trigger AND session-recycling to catch this kind of second-leg play.

## ⭐ NEWS DISCREPANCY — PROVEN a DATA gap (not a wiring gap)

Live check 2026-06-24: CHILI's `momentum_symbol_viability.execution_readiness_json.extra.catalyst_symbols` =
[AMZN, BWMN, CRWD, DCCPY, DVN, GILT, META, PODC, ROK, SCHD, SNIRY, SPCX, TSLA, YUM] — **all large/mid-cap;
ZERO of Ross's small-cap movers** (PLSM, CALC, FRTT, RUN, CCXIW, VTAK, QNRX, CUPR — all `no`). `top_market_
gainers` = ['RUN'] only (caught the large-float name, missed the low-floats). No dedicated small-cap news
table exists (trading_news / news_headlines = UndefinedTable). So CHILI is genuinely BLIND to the small-cap
PR/partnership/FDA/offering catalysts (PLSM Ouma, CALC $49M) that drive the +200-458% movers.

CONSEQUENCE for the #1 lever (news-conviction): it has TWO parts — (1) DATA: ADD a small-cap-catalyst news
SOURCE (Benzinga small-cap PR / Polygon news covering PLSM/CALC-type names); the current catalyst feed is
large-cap-only. (2) WIRING: the conviction (size/stop/hold) per the wsfemnhzb design. Part (1) is the bigger
lift (a new news-data integration) — this is why #1 is NOT a same-day flag-flip; it needs the data source first.

## Other cross-references (2026-06-24 premarket)

- **CALC (CalciMedica, +60%, $49M financing news):** CHILI traded it → −$2.50 controlled scratch (book turned
  seller-heavy: book_imbalance -0.277, trade_flow -0.047 → bailed clean). Ross was watching/waiting (choppy). CHILI
  caught it via MOMENTUM, blind to the $49M news (same news-catalyst gap). The microstructure signals (trade_flow/
  book_imbalance) correctly flagged the selling → good controlled exit. ✅ first real fill, full pipeline validated
  (entry_features + trade_flow + meta_label_emit all captured).
- **SLOT MISALLOCATION:** CHILI's highest live-eligible viability names (CUPR 0.742, QNRX 0.680, VTAK 0.639) were
  NOT armed, while the 3 watch slots held WEN (0.600, large-cap NOT on Ross's gapper scanners — divergence) +
  EHGO (0.532, live_eligible=FALSE → can't even trade live). FIX: arm the top-scored LIVE-ELIGIBLE names; drop
  divergent large-caps + paper-only names from live slots (rank/slot logic).
- **CCXIW (warrant, +153%):** Ross's 5-Pillars Scan flagged it (huge RVOL) but it's a thin warrant ("Check
  Filing", 150K vol). CHILI correctly EXCLUDES warrants (not in universe) — arguably smarter here. ✅ keep.
- **WEN divergence:** CHILI watching WEN (Wendy's, large-cap) — not a Ross-style low-float gapper. A universe quirk
  taking a slot. Investigate why WEN qualified.

---

## Recurring theme

CHILI's SELECTION is good (it sees the same movers, excludes warrants smartly). The losses come from EXECUTION +
CONVICTION on the names it does trade: chasing extended entries, tiny size, tight stops shaken out, no scaling,
and — the through-line — **no news-catalyst conviction** to justify holding/sizing a real runner. The #1 after-
market upgrade is the small-cap-catalyst news → conviction (size + stop-tolerance + hold-through), then the
extension/chase guard + scaling/re-entry.
