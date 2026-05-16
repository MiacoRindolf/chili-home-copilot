# NEXT_TASK: f-composite-quality-reweight-realized-evidence

STATUS: DONE

## Outcome

D1-D7 shipped over 6 commits (c4cf1ba, 22a12ed, aadae96, 81000e6, 2e468fa, + CC_REPORT).

**Spearman re-measurement:**
- Pre-deploy: rho = **−0.7570**, p = 0.0044 (statistically significant anti-correlation)
- Post-deploy: rho = **−0.2587**, p = 0.42 (no longer statistically significant)

**Magnitude reduction:** 66% (the anti-correlation collapsed from strong to noise).

**Brief's success threshold not met** (asked rho ≥ +0.30; got −0.26). Pattern 585 (the alpha) moved from rank 10 of 12 to rank 3, but two n<5 patterns (1068, 1067) still rank above it because of an unforeseen interaction with the re-normalization design choice (when n<5, the formula multiplies the five non-realized weights by 1/0.65 = 1.538, which inflates strong-backtest-weak-realized patterns above proven winners).

**Migration 244 demoted 2 patterns:**
- pid=706 (Above upper BB + RSI, n=6, avg=−0.24%, total=−$3.96): shadow_promoted → challenged
- pid=1216 (EMA stack + RSI neutral, n=11, avg=−6.75%, total=−$21.04): pilot_promoted → challenged

CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-16_f-composite-quality-reweight-realized-evidence.md`.

## Follow-up queued

The next brief should drop the re-normalization arm and use a "raw partial sum (max 0.65), no inflation" rule when n_realized < 5. Expected effect: pattern 585 tops the ranking, n<5 patterns cap below it, anti-correlation regression test passes.

To be authored: `docs/STRATEGY/QUEUED/f-composite-reweight-no-renormalize.md`.

A second, smaller fix in the same follow-up: the `weight_sum` validation in `compute_and_persist_scores` currently includes non-weight parameters (normalizer_pct, evidence_tau, window_days), giving the spurious "weights sum to 121.01" warning observed in D6 deploy logs. Cosmetic only — no behavior impact.

## Pending operator actions

- `CHILI_COHORT_PROMOTE_ENABLED` stays **OFF**. The new formula is safer than the old, but the top-of-ranking inversion (1068/1067 above 585) means flipping the flag would still promote losers. Do not flip until the follow-up fix lands.
- `git push` the 5-commit chain (c4cf1ba → 2e468fa) when ready. Not pushed automatically — operator's call.

## Source brief preserved

`docs/STRATEGY/QUEUED/f-composite-quality-reweight-realized-evidence.md` (amended 2026-05-16 during plan-gate to drop the pattern_family_trial_log insert and correct the w_norm normalizer note).
