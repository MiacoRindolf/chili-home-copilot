# operator-note-rvol-math-not-audit-hide

STATUS: QUEUED
PRIORITY: P0 PROCESS + MATH CORRECTION
PROPOSED: 2026-07-01
REQUESTED_BY: operator
SCOPE: Ross lane RVOL / volume-ratio fixes, setup gates, replay validation

## Operator Note

Do not "fix" the RVOL bug by merely hiding the bad field from one hard gate while keeping it as an unresolved audit artifact.

The operator's concern: this pattern turns into a future dark flag or silent blockage. If the current `vol_ratio` is wrong because it uses cumulative day volume divided by full-day ADV, the fix is to replace it with the correct math and prove the behavior through replay.

## Required Correction

Do not raise the RVOL floor blindly.

Do not ignore low RVOL everywhere.

Do not treat "stored as audit evidence" as complete.

Replace the bad input with a time-normalized volume metric:

```text
volume_pace = cumulative_volume_so_far / expected_cumulative_volume_at_this_time
```

where:

```text
expected_cumulative_volume_at_this_time =
    ADV * intraday_cumulative_volume_fraction(symbol/regime/time_of_day)
```

If per-symbol intraday curve is unavailable, use a market/session curve as fallback and mark telemetry:

```text
rvol_source = symbol_intraday_curve | market_curve_fallback | insufficient_history
rvol_pace
expected_cum_vol
actual_cum_vol
session_elapsed_fraction
curve_sample_days
fallback_reason
```

For premarket/extended hours, use a separate premarket volume pace model instead of regular-session full-day ADV math.

## Gate Behavior

The Ross hard gate should consume normalized volume pace, not raw cumulative day/full-day ADV ratio.

If normalized RVOL is unavailable:

- selection/eligibility should know it is incomplete data, not low volume;
- setup gates should fail soft or defer depending on other evidence;
- telemetry must say `rvol_incomplete`, not `low_rvol`;
- replay must include the unavailable/incomplete path.

## Replay Requirement

Before deploy, run replay comparing:

```text
old cumulative_day_volume / ADV ratio
new time-normalized RVOL pace
missing/incomplete RVOL fallback
```

Acceptance criteria:

- valid Ross-style early movers are not blocked just because the day is young;
- truly low-participation names still down-rank or down-size;
- replay shows impact on entries, missed winners, false positives, expectancy, drawdown, and sizing;
- telemetry makes every RVOL decision reconstructable.

Do not mark this complete with only unit tests. It needs replay evidence against historical opportunities.

