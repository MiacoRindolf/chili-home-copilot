# Ross Challenge Parity + Mistakes Catalog + L2-Recording Plan (2026-06-28)

Operator: study Ross's Small Account Challenge, mock his trades so CHILI can be tested for the RIGHT
decision, study his MISTAKES; + research how to RECORD L2 properly for study/replay.

## Ross's documented trades (Warrior recaps — self-reported/survivorship-biased; decisions+ratios are
## high-confidence, dollars are marketing — NEVER a CHILI expectancy target; FTC-sanctioned 2022)
- KAVL (clean winner): low-float runner; entered on the break + ADDED on each pullback INTO strength
  ($2.38->2.60->2.75->2.80); partials at $3.25; rode the runner through a halt (halt=continuation) to ~$7.93.
- LRHC (low-float IPO, ~5M float): $2.40->$4.40, "100% in 30 seconds"; was up >$10k, gave back 25% off the
  top = the lesson: didn't sell ENOUGH into the vertical spike.
- DCFC (the -$15k blow-up): did RIGHT = sold into the halt-spike (+$4,500); then the MISTAKES = FOMO long,
  added the runner TOO HIGH ($11.50), then AVERAGED DOWN ($10.30/$10.40) hoping for a bounce -> -$15k.
- LGVN (disciplined -$344): micro-pullback failed, choppy tape, cut small, REFUSED to re-enter ("garbage").
- Mechanical geometry: first 1-min candle to break the prior high + GREEN tape confirm; STOP = low of the
  pullback (<=50% of the up-leg or invalid); sell into strength in tranches; pyramid UP, NEVER average down.

## Mistakes -> CHILI guard (the proof CHILI doesn't repeat Ross's errors)
- M1 revenge/double-down after a loss -> COVERED (sizing is f(equity,ATR,stop,liquidity) ONLY, never streak;
  reap-cooldown; per-broker daily-loss #727).
- M2 FOMO chase into extension -> COVERED (the 4 chase-guards: pullback_too_deep + extension + backside+VWAP).
- M3 OVERTRADING after 10am (Ross: ALL his losses were after 10am) -> PARTIAL: #770 midday-deweight raises the
  bar, but NOT a full session-phase edge-weighting derived from the lane's own per-phase realized expectancy
  (adaptive, no hardcoded clock). THE cleanest new-guard candidate.
- M4 no hard daily max-loss -> COVERED (governance daily-loss + per-broker; blocks entries, never exits).
- M5 averaging DOWN -> COVERED (pyramids UP only).
- M6 low-volume parabolic -> AVOID by design (extension/backside guard, not a buy).

## L2-recording: the REAL bottleneck (verified in code, NOT the IQFeed 500-cap)
- iqfeed_depth_snapshots (iqfeed_depth_bridge.py:58-71) persists ONLY aggregate 5-level sums + top-of-book at
  2s -> the per-price-level LADDER (built in Book, line 74) is THROWN AWAY before disk, so spoof / hidden-
  seller / absorption CANNOT be reconstructed for replay/study.
- _live_symbols() (line 124) subscribes ONLY to names already in a LIVE session -> the ~88-name ceiling; L2
  arrives AFTER arming, never during candidate eval when the veto would inform entry.
- THIS is why the OFI-exhaustion exit + hidden-seller veto data-starve (~88/684 names).

### The plan (highest leverage first)
- P0 (~1 day, ~0 cost): decouple subscription from live sessions -> _subscribe_set() = live sessions UNION
  top-N momentum candidates (by score_universe explosiveness), pre-warm L2 when a name enters WATCH (selection
  leads the window). Moves L2 from ~13% -> ~top-400 of the universe. DIRECTLY fixes the exit-model's OFI
  data-starvation.
- P0b (~0.5 day): collapse the triple-subscribe (WOR+WPL+w) to the one dialect that fills the book -> ~3x the
  shared 500-symbol budget for free.
- P1: lossless snapshot + incremental-delta schema -> revive the per-level ladder for replay/study (the
  operator's "marecord nang maayos para magamit sa pagaaral later").

## Parity-test instruments (building now): tests/test_ross_parity_scenarios.py (KAVL/DCFC/LGVN decision-parity)
## + tests/test_ross_mistakes_guarded.py (one test per mistake -> the guard blocks it).
