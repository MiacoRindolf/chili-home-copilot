# Monthly-DD-breaker arming-watch — 2026-05-23

**Scheduled task:** `phase3-monthly-dd-breaker-arming-watch` (daily, 07:00 local)
**Status:** read-only watch; no code or flag changes.

## Arming progress

n = **28** CHILI-attributed close-days (need 30). Trajectory:

- 2026-05-16: n=21
- 2026-05-19: n=23
- 2026-05-21: n=26
- 2026-05-22: n=27
- 2026-05-23: n=28 (+1)

Window: 2026-04-20 → 2026-05-23 05:48 UTC (34 calendar days; 285 attributed close-trades). Last-7-days pace = 1.0 close-day/day. SKILL-baseline pace ≈ 0.82/day (5.74/wk).

Remaining: **2 close-days**.
**Projected arm date: 2026-05-25 (Sun) → 2026-05-26 (Mon)** at recent pace — i.e. **2–3 calendar days from today**. Weekend crypto closes can land on Sun, but Mon is the more likely first-crossing day. **Next daily run is very likely the first "READY TO ARM" report.**

**Recommendation:** no action. The pre-stage instructions from yesterday still apply — keep `CHILI_MONTHLY_DD_BREAKER_ENABLED=1` queued for `.env` + `docker compose up -d --force-recreate chili scheduler-worker autotrader-worker` so it can be flipped immediately on operator approval.

## Current flag state

- `chili_monthly_dd_breaker_enabled`: **OFF** (no env var in `chili` container; `settings_kv` table still doesn't exist — flag remains env-only).
- Matches f-phase3-stop-bleed's default-OFF ship posture.

## Preview threshold (informational only — not authoritative until n≥30)

Computed from the 28d sample using the same math as `_monthly_dd_threshold` (portfolio_risk.py:909-969):

- mean_d = **+$14.12** (recovered from $13.42 at n=27 — late-arriving closes revised 05-20 from −$28.54 → −$11.51 and 05-21 from −$0.87 → +$16.16; 05-22 revised slightly +$7.42 → +$6.85; 05-23 added −$0.47 so far)
- std_d = **$51.94** (tightened from $53.35 at n=27 — the 05-20 revision compressed the negative tail)
- K = 2.0σ (default)
- threshold = 30·mean_d − 2·√30·std_d = **−$145.28**

Current 30-day CHILI-attributed realized PnL = **+$431.15** over 261 trades (up +$66.91 from yesterday's +$364.24).
Headroom = +$431.15 − (−$145.28) = **+$576.43** (widened from +$545.91 yesterday).

**Day-1 trip risk: NO.** Headroom is the widest it has been since arming-watch began; both the numerator and the threshold moved in the favorable direction.

## Watch items

- **Mean-day reversed direction.** The slide from $20.01 (n=21) → $13.42 (n=27) inverted to $14.12 (n=28) as 05-20/05-21 late closes resolved positively. Worth confirming whether next 2 days continue the recovery or revert.
- **+$200.84 / −$76.70 outliers (2026-05-10 / 2026-05-13) still anchor std.** Threshold will tighten materially in November when these age past the 180d window.
- **Numerator-denominator symmetry holding.** f-monthly-dd-breaker-numerator-symmetrize (commit `fdfe15d`) continues to make this an actionable trip line; pre-fix arm-day numerator would have been the all-closed bleed.

## Next watch

Tomorrow 07:00. n=29 is very likely; n=30 plausibly hits 2026-05-25 if a Sunday close lands or 2026-05-26 otherwise. Next report should surface "READY TO ARM" with live-computed threshold, current 30-day attributed numerator, headroom, and the deploy command pre-staged.
