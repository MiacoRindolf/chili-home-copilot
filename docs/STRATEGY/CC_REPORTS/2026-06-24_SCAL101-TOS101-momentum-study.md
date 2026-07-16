# SCAL101 (Max Myszka) + TOS101 (Danny) — Ross-aligned momentum grad courses — visual study

Studied 2026-06-24 (transcripts + key-frames; 10 + 11 videos). Both are Warrior grads teaching Ross-style small-cap momentum, NOT Ross himself. Verdicts vs the **deployed** lane.

## SCAL101 — Scalping Small Cap Momentum (Max)
**New/notable concepts:**
- Price universe $2-$30, **sweet spot $5-$15** (size comfort: 10-15k shares); avoid <$2 (no range) + >$30 (too-fast swings).
- **Non-monotonic volume preference** ("obvious stock = highest volume, BUT too-high volume = choppy") → an **inverted-U** volume score, not monotonic "more is better."
- **Hybrid trade** = buy the dip off support THEN ride momentum (dip-then-continuation) — his most profitable.
- **5-trades/day hard cap** to force A+ selectivity (psychological + selectivity throttle).
- Higher-float-can-squeeze-BIGGER caveat (float not pure "lower=better").

**Verdicts:** reverse-split/secondary-offering veto = already in CHILI (catalyst hyper-mover gate). Most others null/unverified (workflow verify partial). Genuinely-new survivors: non-monotonic volume score, per-day trade-count budget, prior-day-PnL size damper — all design-more/unproven.

## TOS101 — Momentum Trading (Danny, ThinkOrSwim)
**Core:** long-biased 95%, small-caps, price-action off KEY LEVELS via L2 + Time&Sales. **3 strategies = breakouts / dips-within-strength / halts+resumptions** — map 1:1 to CHILI's `pullback_break_confirmation` / micro-pullback dip-buy / halt-resume dip-buy.
**Reinforcements (not new edges, but external support for open tasks):**
- **Structural, consistent stops** (prev 1m/5m candle low, 9MA, 20MA) — NOT a fixed dollar/clock. Strong support for Task #4 (kill the 300s magic clock) + "adaptive, no magic numbers."
- **Dips only "within the context of strength"** → the dip-buy should be gated by the larger move still being front-side (supports the front_side_state gate — and the chasing_top recalibration in flight).
- Hot/cold regime governs overtrade + fixed-target-vs-level-exit (argues against fixed targets when hot).
- Order-type mastery = "part of my edge" (reinforces marketable-LIMIT/maker-only entry — the 0-fills fix).

## Conclusion (5 courses now: BA101, AS101, HVM101, SCAL101, TOS101)
Overwhelmingly consistent: **CHILI already implements Ross's playbook comprehensively.** The courses VALIDATE the build; they do not expose shippable gaps. The genuinely-new survivors are unproven refinements (non-monotonic volume, fake-catalyst guard, thick-tape veto, curl-selector, prior-day-PnL damper) for the operator's "log-only first → prove → wire" pipeline — NOT rush-ships. The lane's profit edge is **execution + discipline** (flow-veto / max-loss-circuit / broker-truth — already in progress), plus the ONE genuinely-broken thing found: **E1 `chasing_top` over-veto** (recalibration in flight → unlocks the anti-chase veto).
