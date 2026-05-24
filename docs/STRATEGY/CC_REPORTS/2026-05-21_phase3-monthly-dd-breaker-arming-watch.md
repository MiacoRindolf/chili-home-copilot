# Monthly-DD-breaker arming-watch — 2026-05-21

**Scheduled task:** `phase3-monthly-dd-breaker-arming-watch` (daily, 07:00 local)
**Status:** read-only watch; no code or flag changes.

## Arming progress

n = **26** CHILI-attributed close-days (need 30). Trajectory:

- 2026-05-16: n=21
- 2026-05-19: n=23 (+2 in 3 days)
- 2026-05-21: n=26 (+3 in 2 days)

Window: 2026-04-20 → 2026-05-21 (32 calendar days; 277 attributed close-trades).
Last-5-days pace = 1.0 close-day/day. SKILL-baseline pace = 0.78/day (5.44/wk).

Remaining: **4 close-days**.
**Projected arm date: 2026-05-25 (recent pace) → 2026-05-26 (baseline pace)** — i.e. **4–5 calendar days from today**. This is plausibly the last daily watch before crossing.

**Recommendation:** no action. Continue watching daily; expect "READY TO ARM" message Mon-Tue next week.

## Current flag state

- `chili_monthly_dd_breaker_enabled`: **OFF** (no env var in `chili` container; `settings_kv` table still doesn't exist in this DB — flag is env-only).
- Matches f-phase3-stop-bleed's default-OFF ship posture.

## Preview threshold (informational only — not authoritative until n≥30)

Computed from the 26d sample using the same math as `_monthly_dd_threshold` (portfolio_risk.py:909-969):

- mean_d = **+$13.55** (down from $20.01 at n=21 — recent five sessions added −$5.23, −$41.33, −$8.49, −$28.54, −$3.65)
- std_d = **$54.42** (essentially flat vs $57.90 at n=21 — outliers +$200.84 and −$76.70 persist)
- K = 2.0σ (default)
- threshold = 30·mean_d − 2·√30·std_d = **−$189.55**

Current 30-day CHILI-attributed realized PnL = **+$303.28** over 273 trades.
Headroom = +$303.28 − (−$189.55) = **+$492.83**.

**Day-1 trip risk: NO** (positive numerator vs negative threshold). The 2026-05-16 ARCHITECT-FLAG about no_pattern bleed tripping the breaker on arm-day is **resolved** by the f-monthly-dd-breaker-numerator-symmetrize ship (commit `fdfe15d`) — the numerator and denominator now share scope and the attributed-only window is currently profitable.

## Watch items

- **Mean-day is decaying.** From +$20.01 (n=21) → +$13.55 (n=26) as the +$200/+$111 outliers age and recent days lean negative. If the trend continues, the day-of-arming headroom may narrow significantly. Worth noting but not actionable yet.
- **std is dominated by two days** (+$200.84 on 05-10 and −$76.70 on 05-13). When these age out of the 180d window in November the threshold will tighten materially.

## Next watch

Tomorrow 07:00. Surface "READY TO ARM" message when n crosses 30 with the live-computed threshold + current attributed-30d numerator + headroom.
