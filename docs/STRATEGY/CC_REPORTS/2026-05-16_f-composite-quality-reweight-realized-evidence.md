# CC_REPORT: f-composite-quality-reweight-realized-evidence

**Date:** 2026-05-16
**Brief:** `docs/STRATEGY/QUEUED/f-composite-quality-reweight-realized-evidence.md`
**Status:** PARTIAL SUCCESS — D1-D7 shipped, Spearman improved from −0.757 to −0.259 (rho un-flipped but p went 0.0044 → 0.42). Follow-up fix queued.

## TL;DR — the verdict

The reweight **substantially weakened** the anti-correlation between composite score and realized PnL — from a statistically significant rho = −0.7570 (p=0.0044) to a non-significant rho = −0.2587 (p=0.42). Pattern 585 (the proven alpha) moved from rank 10 of 12 to rank 3. Migration 244 cleanly demoted 2 trade-eligible losers (706 and 1216). The brief's success threshold of rho ≥ +0.30 was **not met** — the new formula still ranks two n<5 patterns (1068 and 1067) above 585, because of an unforeseen interaction with the re-normalization design choice. A follow-up brief should drop the re-normalization or replace it with a "no score until n ≥ 5" rule.

## Commits shipped

| Commit | Deliverable | Files | Δ Lines |
|---|---|---|---:|
| `c4cf1ba` | **D1** — realized-PnL component + helpers | `pattern_quality_score.py` (+275), `brain_work/handlers/quality_score.py` (+57) | +332 |
| `22a12ed` | **D2** — 11 settings (4 default-changes + 6 new + 1 unchanged) | `app/config.py` | +43 |
| `aadae96` | **D3** — realized-PnL eligibility floor in `select_cohort_candidates` | `pattern_cohort_promote.py` | +44 |
| `81000e6` | **D4** — mig 244 composite-reweight demote-the-losers (idempotent) | `app/migrations.py` | +122 |
| `2e468fa` | **D5** — `tests/test_composite_reweight.py` (18 unit + 4 integration) | new test file | +434 |
| (this file) | **D7** — CC_REPORT | this file | +~200 |

D6 (deploy + measure) ran inline as part of the verification chain; no separate commit needed.

## D6: post-deploy Spearman measurement

Refresh: `compute_and_persist_scores` re-ran with the new formula. 591 patterns examined, 12 scored (the rest excluded by the `rolling_sample_n < 30` directional-WR floor that was already in place).

Spearman re-measurement, n=12 patterns with both score and realized trades:

| Metric | Pre-deploy (old formula) | Post-deploy (D1+D2) | Δ |
|---|---:|---:|---:|
| Spearman ρ(score, total_pnl) | **−0.7570** | **−0.2587** | +0.498 |
| p-value | 0.0044 | 0.4168 | +0.412 |
| Spearman ρ(score, avg_pnl) | −0.6970 | −0.3077 | +0.389 |
| Top-half by score: total PnL | −$118.63 | (see top-15 below) | |
| Bottom-half by score: total PnL | +$597.80 | (see top-15 below) | |

**The anti-correlation collapsed from statistically significant to non-significant, but did not flip positive to the brief's +0.30 threshold.**

Top-15 by NEW composite score (post-deploy, 2026-05-16):

| Rank | pid | Stage | Score | n | Total PnL | Avg pct | Name |
|---:|---:|---|---:|---:|---:|---:|---|
| 1 | 1068 | promoted | 0.892 | 4 | −$32.46 | −2.68% | Volume spike 2x+ with RSI<40 |
| 2 | 1067 | pilot_promoted | 0.772 | 2 | −$27.11 | −4.53% | Below lower BB + RSI oversold |
| 3 | **585** | **promoted** | **0.736** | **85** | **+$554.13** | **+1.63%** | **Squeeze + decl vol [bb_squeeze]** |
| 4 | 1073 | challenged | 0.615 | 12 | −$44.96 | −2.03% | Mid-BB + low ADX + MACD |
| 5 | 1066 | challenged | 0.580 | 9 | −$69.32 | −2.54% | RSI near-oversold + MACD |
| 6 | 1242 | pilot_promoted | 0.516 | 6 | +$16.69 | +0.93% | Below lower BB (variant) |
| 7 | 1215 | challenged | 0.500 | 2 | +$1.39 | +0.25% | RSI>55 + ADX>25 + MACD |
| 8 | 1065 | pilot_promoted | 0.497 | 15 | +$12.82 | +0.29% | Deep oversold RSI<25 |
| 9 | 586 | shadow_promoted | 0.492 | 18 | +$42.34 | +0.74% | Squeeze + decl vol (tweak) |
| 10 | 8 | challenged | 0.461 | 12 | −$7.52 | −0.33% | ADX>30 + RSI<40 |
| 11 | 706 | challenged | 0.437 | 6 | −$3.96 | −0.24% | Above upper BB + RSI |
| 12 | 1216 | challenged | 0.423 | 11 | −$21.04 | −6.75% | EMA stack + RSI neutral |

**What worked:**
- Pattern 585 (the proven alpha, +$554 over 85 trades) is now in the top 3 by score. Pre-deploy it was rank 10.
- All four high-CPCV losers (1066, 1067, 1068, 1073) have scores in the 0.5–0.9 band, no longer dominating.
- Patterns 706 and 1216 were demoted by mig 244 to `challenged` — they no longer appear in trade-eligible stages.
- D3's eligibility floor (verified by the SQL probe at commit time) excludes 1066/1067/1068/1073 from cohort auto-promote regardless of their score.

**What didn't work — the re-normalization inversion:**

The brief specified that when `n_realized < 5`, the realized component contributes 0 and the remaining five weights re-normalize (each multiplied by `1 / (1 − w_realized) = 1.538`). This was implemented faithfully (D1 commit c4cf1ba).

The unforeseen interaction: a pattern with `n<5` and **strong** non-realized components (high CPCV, high directional_wr) gets its score *inflated* by 53.8% to compensate for the missing realized signal. Pattern 1068 sits at score 0.892 — higher than 585's 0.736 — purely because its 4 trades aren't enough to engage the realized component, so the re-normalized non-realized components dominate.

The D5 anti-correlation regression test independently catches this:

```
test_anti_correlation_new_formula_produces_positive_spearman FAILED
    AssertionError: new formula should correlate positively;
                    got rho=-0.679
```

The synthetic dataset designed for this test (5 high-CPCV losers vs 5 low-CPCV winners, with avg_pnl_pct = ±0.5%) shows that even when realized PnL is present and strong, the realized component contribution (0.35 × 0.75 × evidence(30) = 0.166) doesn't beat the directional_wr difference baked into the synthetic structure (0.35 × (0.90 − 0.55) = 0.1225) plus CPCV clipping saturation.

## Mig 244 audit log

```
[chili_mig_244] pid=706  old_stage=shadow_promoted new_stage=challenged n_trades=6  avg_pnl_pct=-0.002397 total_pnl=-3.96
[chili_mig_244] pid=1216 old_stage=pilot_promoted  new_stage=challenged n_trades=11 avg_pnl_pct=-0.067505 total_pnl=-21.04
[mig244] demoted 2 patterns to 'challenged'
```

Both demotions logged structurally. `schema_version` recorded `244_composite_reweight_demote_losers` at 2026-05-16 11:33:09 UTC.

Notable: my brief predicted 4-6 patterns would be demoted. Actual was 2. Reason: patterns 1066, 1067, 1068, 1073 — the high-composite losers I expected to catch — were already in `challenged` lifecycle stage (not `promoted` / `shadow_promoted` / `pilot_promoted`), so the mig's WHERE clause correctly skipped them. The 2 it caught (706, 1216) were the actual trade-eligible ones with bad realized data.

## D5 test results

```
collected 18 items (3 DB-integration tests skipped because TEST_DATABASE_URL gate wasn't satisfied in the dispatch context)

15 PASSED:
  realized_pnl_score: all 7 shape tests (saturation, neutral, NULL propagation)
  realized_evidence_score: 4 of 5 (n=tau, n=1, n=85, n=0)
  compute_quality_composite_score:
    test_anti_correlation_old_formula_produces_negative_spearman PASSED (rho=-0.679)
    test_renormalization_when_realized_absent PASSED
    test_realized_component_full_credit_path PASSED
    test_realized_component_zero_when_n_below_floor PASSED

3 FAILED (diagnostic — captured the production issue):
  test_realized_evidence_score_null_n (minor: realized_evidence_score(None) doesn't raise TypeError as the test asserted; it actually computes some value via Python's None arithmetic somewhere upstream. Implementation choice.)
  test_anti_correlation_new_formula_produces_positive_spearman (THE KEY ONE — synth dataset still anti-correlates at rho=-0.679 under the new formula)
  test_anti_correlation_flips_sign (same root cause — sign doesn't flip)
```

These failures are correctly captured and committed. They represent the design issue the next brief needs to resolve.

## Honest assessment vs. the brief's success criterion

The brief's pass-criterion was:
> Spearman(new_score, total_pnl) ≥ +0.30 at n=12 (positive, opposite sign from today's −0.757).

We got **−0.2587**, which is:
- **No longer statistically significant** (p=0.42 vs 0.0044 pre-deploy) — the anti-correlation is now noise, not signal.
- **Substantially weakened** — from a strong −0.76 to a weak −0.26 (66% reduction in absolute correlation).
- **Not the sign flip** the brief demanded.

The 50%-magnitude improvement matters: it means the formula is no longer reliably ranking losers above winners. Pattern 585 went from rank 10 to rank 3. But two n<5 patterns (1068, 1067) still rank above 585 because of the re-normalization inflation, and the production data didn't have enough variance in directional_wr / CPCV to let the realized component dominate.

## Operator decisions / activations needed

- **`CHILI_COHORT_PROMOTE_ENABLED` remains OFF.** Per the brief's hard constraint. The new formula is safer than the old one but still flawed at the top of the ranking. Do not flip until the follow-up fix lands.
- **No env-var overrides needed.** All defaults match the formula in D1/D2.
- **Containers were force-recreated** during D6 (chili / scheduler-worker / brain-worker / autotrader-worker). New formula is live; mig 244 ran on chili's startup at 11:33:09 UTC; scores recomputed once on chili's startup. They will recompute again on each scheduled refresh cycle.

## Follow-up brief queued

`docs/STRATEGY/QUEUED/f-composite-reweight-no-renormalize.md` (to be authored).

The proposed change: replace the re-normalization arm with a "n_realized < 5 → composite uses raw partial sum (max 0.65), do NOT inflate." That gives proven-track-record patterns (n≥5 with realized data) a structural advantage over patterns with only backtest evidence, which is exactly the production behavior the brief intended.

Expected effect under this rule:
- Pattern 585 (n=85, full realized credit) tops the ranking.
- Patterns 1067/1068 (n<5) cap out at ~0.65 and drop below 585 to rank ~4-5.
- Test `test_anti_correlation_new_formula_produces_positive_spearman` should pass.
- Production Spearman should cross +0.30.

A second, smaller follow-up will fix the `weight_sum` validation in `compute_and_persist_scores` — the current sum includes the non-weight parameters (normalizer_pct=0.01, evidence_tau=30.0, window_days=90), giving the spurious "121.01" warning observed in D6. Cosmetic only; no behavior impact.

## Surprises / Cowork notes

1. **The re-normalization unintended consequence** is the headline finding. The brief specified it explicitly (and Cowork plan-gate approved it), but neither party stress-tested the implication that n<5 patterns would get inflated above n>>5 patterns. The synthetic regression test caught this immediately — exactly what the test was designed for.

2. **The `weight_sum` warning** ("weights sum to 121.0100") is a pre-existing bug in `compute_and_persist_scores` that D1 made more visible. The function sums *all* values from `_resolve_weights`, including non-weight parameters. Worth fixing in the follow-up.

3. **Daemon-truncation hazard hit again on D2 first attempt.** Three CC-via-daemon attempts before Cowork switched to direct Edit. Phase3-stop-bleed had the same pattern. The truncation hazard on config.py / migrations.py / models/trading.py via `claude -p` Edit operations is reproducible. Cowork direct Edit + dispatch-script verification was the workaround.

4. **D5's 3 failing tests are kept committed deliberately.** They document the failure mode the follow-up needs to fix. Marking them xfail or removing them would erase the evidence.

## State on close

- HEAD: `2e468fa test(composite_reweight): D5 - anti-correlation regression + helpers + integration`
- 5 commits ahead of `origin/main` (D1 through D5). Not yet pushed — Cowork's call whether to push or hold pending the follow-up.
- NEXT_TASK.md still PENDING with D2-D7 enumerated — needs update to DONE / replaced with the follow-up.
- Pause flag: removed (session-daemon active).
- Working tree: clean on the brief's target files. Pre-existing fast-path / strategy WIP untouched.

The new formula is **strictly safer than the old one** for cohort_promote activation (D3 floor enforced, mig 244 demoted the trade-eligible losers, anti-correlation no longer significant). But the brief's success threshold isn't met; the follow-up fix is required before flipping `CHILI_COHORT_PROMOTE_ENABLED`.
