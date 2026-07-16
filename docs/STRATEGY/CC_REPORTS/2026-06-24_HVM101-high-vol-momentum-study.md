# HVM101 — High Volatility Momentum Trading (Jess, Warrior Pro grad course) — visual study

Studied 2026-06-24 from transcripts + video key-frames (10 videos). Verdicts checked against the **deployed** lane (etfrank worktree).

## Core framing
Explicitly "a sub-strategy of Ross's." Risk-FIRST ordering ("I look at my risk first" before setups). Long momentum on small-caps, **price $3-20 sweet spot (no hard max), float <100M (majority <20M)** — but these are GUIDELINES not hard rules ("too much bias keeps you out of trades"; has traded 200M-500M floats that ran). Trade window **7-11am ET "or so", extend if momentum strong**. Psychology weighted > strategy. Binary yes/no decisions. Two named setups taught: **The Curl** (ch.7) + **Wick Reclaim** (ch.8).

## Named setups
- **The Curl** — rounding-bottom continuation off a gap-fade or mid-trend pop-fade; buy the first 1m new-high back over the 9-EMA (a cup-and-handle on the intraday).
- **Wick Reclaim** — in EXTREME momentum only: a topping-tail rejection gets overpowered and the wick is reclaimed LONG. ⚠️ Verifier caught the over-eager version: Ross's playlist actually treats *repeated* topping tails as an EXIT/avoid signal — a generic "buy the wick reclaim" entry is NOT supported and would be a backside trap. Only the narrow "violent reclaim right after a sharp rejection around a halt" is grounded.
- **9-EMA "no pocket" rule** — don't buy a pullback-break when there's a large gap between the pulling-back candle and the 9-EMA (enter at the apex, not into extension).
- **Pullback-count** — take the 1st-3rd pullback in a leg, avoid the 4th+ (extended, topping-tail prone).

## Edge verdicts (vs DEPLOYED lane)
**Already shipped (study VALIDATES CHILI) — reject as builds:**
- 9-EMA proximity/"no pocket" gate (implemented TWICE; the tighter form is empirically net-NEGATIVE live) · pullback-count/ordinal cap (live: `pullback_ordinal_recent` + raised vol-floor on 3rd+) · daily-bar S&R + 200-EMA-on-daily (live: `daily_levels.py::compute_daily_context`) · per-minute volume-rate floor (RVOL/$-vol floor live) · price/float guideline floors.

**Genuinely-new (design-more / log-only-first):**
- **Thick-tape / distribution veto** (high cumulative volume, ~no net range progress = distribution) — promising but unproven; verifier (rightly) recommends LOG-ONLY measurement first, not a hard veto.
- **Curl as a distinct selector branch** — real Ross pattern; verifier recommends replay-attribution first (the pullback-break gate already captures much of the geometry).
- **Opening-bell suppression** (no curl/dip-rip trigger in first ~2 min after RTH open) — grounded, narrow; verdict incomplete, re-verify (risk: missing opening continuations of premarket runners).
- **Bid-prop / spread-tightening L1 confirmer** — verdict incomplete.

## Conclusion (consistent with AS101)
HVM101 again mostly CONFIRMS CHILI is already Ross-aligned. No clean rush-shippable edge; the genuinely-new ideas are unproven and the rigorous verify flagged over-veto/regression risk on the hard-wired forms — pointing to the operator's own "log-only first → prove → wire" process. The lane's edge is in execution/discipline (already targeted by flow-veto / max-loss-circuit / broker-truth), not missing setups.
