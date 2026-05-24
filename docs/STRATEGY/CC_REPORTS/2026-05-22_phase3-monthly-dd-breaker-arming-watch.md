# Monthly-DD-breaker arming-watch — 2026-05-22

**Scheduled task:** `phase3-monthly-dd-breaker-arming-watch` (daily, 07:00 local)
**Status:** read-only watch; no code or flag changes.

## Arming progress

n = **27** CHILI-attributed close-days (need 30). Trajectory:

- 2026-05-16: n=21
- 2026-05-19: n=23 (+2 in 3 days)
- 2026-05-21: n=26 (+3 in 2 days)
- 2026-05-22: n=27 (+1 in 1 day)

Window: 2026-04-20 → 2026-05-22 (33 calendar days; 279 attributed close-trades).
Last-7-days pace = 1.0 close-day/day. SKILL-baseline pace = 0.82/day (5.73/wk).

Remaining: **3 close-days**.
**Projected arm date: 2026-05-25 (Sun) → 2026-05-26 (Mon)** at recent pace — i.e. **3–4 calendar days from today**. If weekend close activity is sparse the Monday date is more likely. **This may be the last "not yet armed" daily report; expect crossing early next week.**

**Recommendation:** no action. Continue watching daily; pre-stage the env flip (`CHILI_MONTHLY_DD_BREAKER_ENABLED=1` in `.env`, then `docker compose up -d --force-recreate chili scheduler-worker autotrader-worker`) so it's ready for operator approval at first crossing.

## Current flag state

- `chili_monthly_dd_breaker_enabled`: **OFF** (no env var in `chili` container; `settings_kv` table still doesn't exist in this DB — flag remains env-only).
- Matches f-phase3-stop-bleed's default-OFF ship posture.

## Preview threshold (informational only — not authoritative until n≥30)

Computed from the 27d sample using the same math as `_monthly_dd_threshold` (portfolio_risk.py:909-969):

- mean_d = **+$13.42** (down from $13.55 at n=26; 2026-05-21 revised from −$3.65 to −$0.87 as that day's late closes resolved, and 2026-05-22 added +$7.42)
- std_d = **$53.35** (tightened from $54.42 at n=26)
- K = 2.0σ (default)
- threshold = 30·mean_d − 2·√30·std_d = **−$181.67**

Current 30-day CHILI-attributed realized PnL = **+$364.24** over 262 trades (up from +$303.28 yesterday).
Headroom = +$364.24 − (−$181.67) = **+$545.91** (widened from +$492.83 yesterday — the +$60.96 30d-PnL gain more than offset the +$7.88 threshold tightening).

**Day-1 trip risk: NO.** Headroom is the widest it's been since arming-watch began.

## Watch items

- **Mean-day decay paused.** The slide from $20.01 (n=21) → $13.55 (n=26) flattened to $13.42 (n=27); 2026-05-22's small positive day stabilised the slope. Worth watching whether next week's incremental days continue near-flat or resume the downtrend.
- **+$200 / −$77 outliers still anchor std.** Same two days as yesterday dominate variance; threshold will tighten materially in November when those age out of the 180d window.
- **Numerator vs denominator symmetry** (f-monthly-dd-breaker-numerator-symmetrize, commit `fdfe15d`) holding — current arm-day day-1 trip risk would have been ~−$1,200 numerator vs −$181 threshold without that fix; with it, +$364 vs −$181.

## Next watch

Tomorrow 07:00. Surface "READY TO ARM" message with the live-computed threshold + current attributed-30d numerator + headroom the moment n crosses 30 (likely Mon 05-26).
