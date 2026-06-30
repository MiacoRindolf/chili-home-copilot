# Ross Small-Account Baseline + CHILI's Staged Match-and-Surpass Plan (2026-06-27)

Operator north star: baseline CHILI's progress on Ross Cameron's small-account curve and equip it to
MATCH then SURPASS Ross in all aspects, via automation ("makahabol sa experience through automation").

## The verified baseline (CPA-audited + FTC records — NOT marketing)
- $583 -> $100k in 44 days -> $1M (~2yr) -> $10M (Jan 2022) -> ~$18.8M (end-2025). Steady-state ~71-74%
  accuracy, 2:1 R, ~$1,800/day on a 7-figure account (<0.2%/day — geometric growth already decayed to
  near-LINEAR because size is LIQUIDITY-capped, not equity-capped).
- The $583 was a SEEDED RESTART by an already-expert trader (20,000+ prior trades). FTC fined Warrior
  $3M (2022, $2.9M refunded); finding: "the vast majority of customer accounts LOST money"; the
  "not typical" disclaimers were ruled legally ineffective. Base rate: ~97% of persistent day-traders lose.
- "$4k -> $2k/day in a month" = ~50%/day = ~4x Ross's own peak burst (micro-scale only, then decayed);
  by his 10%-of-equity rule, $2k/day = a ~$20k account. It is a STRETCH CEILING, not a baseline.
- USABLE design anchors (and CHILI already implements them): selection (5x RVOL, up>=10%, float<20M,
  $5-10, catalyst, top-5 gapper) + risk (2:1, fixed daily-max-loss, 3-strikes, ~5%/10% scaling, trader-rehab).
- THE TARGET = % of equity within the liquidity ceiling, NEVER a fixed $/day.

## Where CHILI is: AT THE GATE, not on the curve (trade ~0 of 20,000)
Selection solved; fill solved (0-fill streak broke 2026-06-23). Winner-capture = the open gap.
LIVE STAGE-0 READ (scripts/ross_baseline_tracker.py, 57 round-trips, momentum_fill_outcomes, live $ only):
  net -$414 | win 26% (15W/41L) | profit-factor 0.12 | exp/trade -$7.3
  avg WIN +$3.6 (0.32R) | avg LOSS -$11.4 (1.00R) | winners >=1.5R: 1 of 57
DIAGNOSIS: the asymmetry is INVERTED. Losses are controlled (1R, no blow-through), but winners are
SCALPED FOR DUST (0.32R). The momentum edge is small-losers + LET-WINNERS-RUN; CHILI does the opposite.

## The staged path (gates measurable; stages 2-4 locked behind Stage 0)
- Stage 0 EXPECTANCY: profit-factor > 1 over >=30 live round-trips AND >=3 winners >=1.5R, losers <=~0.8R.
- Stage 1 CONSISTENCY: net-positive >=3 of 4 weeks, max-drawdown < the daily-loss cap.
- Stage 2 COMPOUND: verify equity-relative sizing scales (no new build — engine exists, idle).
- Stage 3 MATCH: live %-curve within ~1x the Ross %-of-equity benchmark, win>=60%, R>=1.5.
- Stage 4 SURPASS: beat the benchmark on the 6 mechanizable axes over a rolling quarter.
Hard rule: do NOT push size (lever 3) before expectancy (lever 1) is consistently positive.

## The surpass thesis (CHILI structurally beats Ross — already built)
Breadth (whole field/min vs 1-2 names), all-hours (premarket+24/7), no-tilt/no-revenge (mechanized;
RH101 is a whole course on Ross failing this), perfect-stop-compliance (max_loss_circuit), parallel +
sub-second, liquidity-aware sizing. PLUS 3 of Ross's celebrated behaviors are ANTI-edges NOT to copy
(hot-hand up-sizing, over-holding runners for giveback, blanket reverse-split veto). These compound
the edge ONLY once per-trade expectancy >= 0 — hence staged behind Stage 0.

## #1 NEXT ACTION (data-grounded by the tracker)
NOT more setups (Ross-complete), NOT scaling, NOT loss-tightening (losses already 1R). The lever is the
WIN side: (a) LET WINNERS RUN (exits/trail/breakeven cut winners at 0.32R; target 1.5-2R+), and
(b) raise the 26% win-rate via selectivity (fewer marginal entries). Validate on LIVE fills + replay
run-R (NOT replay $). Instrument with scripts/ross_baseline_tracker.py every session.

## Instrumentation
scripts/ross_baseline_tracker.py — the stage-gate dashboard. Reads momentum_fill_outcomes (live $ only;
replay $ excluded — proven ~5x overstated). Reports expectancy + the Stage-0 gates + an optional Ross
%-of-equity overlay (model, not a $2k/day promise). Run: docker exec ... python scripts/ross_baseline_tracker.py
