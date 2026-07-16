# Ross Cameron 39-video playlist → CHILI momentum-lane gap roadmap

**Date:** 2026-06-15
**Source:** PL1xI23WKVWidDl8RJ4zJ6LJq5SQnBQ6Mr (39/39 transcripts with accessible captions; 36 of the 75 had transcripts disabled/region-locked — yt-dlp + transcript API both failed).
**Method:** 7-agent workflow (wquku5w6e) — extract Ross's full methodology → cross-reference vs CHILI code → dedup → prioritize.
**Directive:** operator wants ALL gaps implemented (not top-3), each flag-gated (no dark flags), parity-safe, replay-validated, instant per-flag rollback (evolve-not-devolve).

## Honest verdict (the workflow's own synthesis)

CHILI **already** has the hard parts of Ross's edge: explosive uncapped WS selection, the Ross pillar scorer, pullback-break + first-pullback + dip-buy entries, OFI/L2 exit locks, per-broker risk caps, equity-relative compounding. Memory corroborates: the live conversion bottleneck is **NO-SETUP / market-state** (203 reaped : 2 entered, dominant reason `waiting_for_break` — names QUALIFY but the tape never thrusts), NOT a missing gate.

So most of these 16 items **prevent bad trades on weak tape** (discipline) rather than **create good ones** (alpha). The two with genuine PnL upside: **#2 measured-move target** (the MEGA-USD give-back lever, real $ already lost) and **#4 theme/sympathy detector** (the only selection-breadth swing — could surface 1000% sympathy movers CHILI structurally can't rank today). Ross's actual edge — being early/obvious on the day's 1-2 real movers — is already ~70-80% present; these take it to ~85%. The last gap is market-state no code closes on a quiet day.

## Already shipped (28 capabilities — NOT gaps)

RVOL≥5x percentile selection · gap/already-moving pillar · low-float pillar · intraday-impulse freshness · Ross small-cap profile · WS uncapped universe (#731) · pullback-break (raw/retest/runaway) · first-pullback (#733) · deep-reclaim dip-buy (#720) · halt-resume dip (#597) · premarket tick-break + 3:45ET warming (#595/#712) · dip-vs-dump discriminators (#734) · conviction break-candle + topping-tail · ATR-aware pullback tolerances · VWAP-hold gate · verticality chase-veto · breakout-or-bailout exit · L2 hidden-seller veto (#699/#704) · OFI+micro-price tilt (#699) · OFI-exhaustion lock + sell-into-strength ladder (#703/#704) · class-aware R:R + structural stops + cushion trail · daily 200-SMA/round#/swing S&R (#23) · catalyst tilt + hot-tape inversion · profit-giveback 50% halt · per-broker daily-loss caps (#727/#728/#729) · streak risk multiplier · day-cushion risk ladder (#664) · hot/cold regime size · equity-relative compounding.

## The 16 gaps (priority order)

| # | Gap | Edge | Cost | Replay-validatable | Status |
|---|-----|------|------|--------------------|--------|
| 1 | Front-side / back-side session state machine (bench a faded mover once 9<20-EMA / MACD-cross-down / lost-9EMA-on-pullback) | HIGH | low | yes (06/10-12 round-trips) | TODO |
| 2 | Measured-move + round-number first-scale target (2nd leg≈1st; scale into round#) | HIGH | low | yes (MEGA give-back) | TODO |
| 3 | Absolute RVOL + change eligibility FLOOR on top of percentile (≥5x / ≥10%, hot-tape raises) | HIGH | low | yes | TODO |
| 4 | Theme/sympathy auto-detector + leader→peer tilt (the 1000%-mover lever) | HIGH | med | partial | TODO |
| 5 | Float-rotation explosiveness pillar (volume/float rotation) | MED | low | yes | TODO |
| 6 | Market-wide leading-gainer RANK tilt (top-3-5 get the eyes) | MED | low | yes | TODO |
| 7 | Pullback-ordinal throttle (1st/2nd full, 3rd+ de-rate) | MED | med | yes | TODO |
| 8 | Green-to-red intraday session breaker (round-trip into red = walk) | MED | low | yes (unit) | TODO |
| 9 | Per-symbol session loss-fatigue bench (MEGA double-loss) | LOW | low | yes | TODO |
| 10 | Liquidity-ceiling pre-trade size cap (ADV/depth) — SCALING gap, flag-OFF first | MED | med | yes | DEFER (scale-stage) |
| 11 | 200-EMA proximity-from-below entry caution | MED | low-med | yes | TODO |
| 12 | Catalyst-TYPE grading (A/medium/weak) | MED | low | yes (unit) | TODO |
| 13 | Hard late-session new-entry cutoff (~11:30 ET, hot-tape relax) | MED | low | yes | TODO |
| 14 | Shadow-Theory S/R invalidation + gap clear-air targets (couple to #2) | MED | med | yes | TODO (with #2) |
| 15 | Pegged-seller / break-then-reload dynamic L2 read | MED | high | log-only first | DEFER (L2 stability) |
| 16 | Secondary-offering / dilution-risk veto (EDGAR feed) | MED | high | yes | DEFER (no filings feed) |

## Build sequence (one logical change at a time, each validated before the next)

**Batch A (cheap · pure · high-edge · replay-validatable):** #1 → #2 (+#14) → #3 → #5 → #8 → #9
**Batch B (med-cost integration):** #6 → #7 → #11 → #12 → #13
**Batch C (selection swing):** #4 (theme detector, flag-piloted — payoff depends on sector/keyword data quality)
**Deferred (build pure fn behind flag-OFF / needs a feed):** #10 (scale-stage), #15 (L2 stability), #16 (filings feed)

Each item: isolated worktree off latest origin/main · pure helpers reusing already-computed series (no new fetches where possible) · parity test (equity byte-identical where crypto-only) · replay A/B (net-positive/neutral gate) · flag default-ON + env kill-switch · commit→PR→merge · deploy when lane flat + verify db-ping=chili.
