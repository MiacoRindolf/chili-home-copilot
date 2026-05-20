# Monthly-DD-breaker arming-watch — 2026-05-16

**Scheduled task:** `phase3-monthly-dd-breaker-arming-watch` (daily, 07:00 local)
**Status:** read-only watch; no code or flag changes.

## Arming progress

n = **21** CHILI-attributed close-days (need 30). Baseline in SKILL.md was 20 → today's `2026-05-16 09:00` close added one day.

- Window: 2026-04-20 → 2026-05-16 (27 calendar days; 237 attributed close-trades)
- Observed rate: 21 / 27 ≈ **5.44 close-days per week** (slightly above SKILL.md's ~5/wk estimate)
- Remaining: 9 close-days
- **Projected arm date: 2026-05-28 — 2026-05-29** (~12 calendar days from today)

**Recommendation:** no action. Continue watching daily.

## Current flag state

- `chili_monthly_dd_breaker_enabled`: **OFF** (no env var in `chili` container; `settings_kv` table doesn't exist in this DB)
- This matches f-phase3-stop-bleed's default-OFF ship posture. Correct.

## ARCHITECT-FLAG — likely day-1 trip on arming

Preview threshold (informational only, **not authoritative until n≥30**) computed from the 21d sample: **−$34.09** at K=2σ (mean_d=$20.01, std_d=$57.90).

But the breaker numerator is `monthly_pnl` over **ALL closed trades** (portfolio_risk.py:1088–1101), not just CHILI-attributed. Current 30-day all-closed realized PnL = **−$1,216.11** over 338 trades.

If armed today: −$1,216 ≤ −$34 → **breaker would trip immediately**, because the no_pattern bucket (per 2026-05-15 quant audit: −$1,560 / 30d) dominates the numerator while it's invisible to the denominator. Operator should expect a day-1 trip when the flag flips, and may want to either (a) drain no_pattern flow first, or (b) gate the breaker numerator on `scan_pattern_id IS NOT NULL` to keep the no_pattern bleed out of the CHILI-attributed breaker's purview — a small architecture decision worth making before arm-day.

## Next watch

Tomorrow 07:00. Surface "READY TO ARM" message when n crosses 30.
