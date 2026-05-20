# f-composite-reweight-no-renormalize

> ⚠️ **PARTIALLY SUPERSEDED 2026-05-18 by f-evaluation-function-fix Tier A #3** (commit `23bde18`). That commit added a stricter version of this fix: `chili_composite_min_realized_trades` floor (default 5) makes the composite score NULL outright when `realized_n_trades < 5`, rather than capping the renormalized score at 0.65. The "n=2 patterns rank above n=86 alpha" problem is now closed structurally — those patterns get NULL, not a high score. **Re-evaluate this brief**: is the softer "cap at 0.65" approach still useful as a middle ground (let CPCV-strong, no-realized patterns get *some* score for ranking but not enough to outrank realized winners)? Or has Tier A #3 made this redundant? Operator decision.

> ⚡ **ACTIVATION REMINDER (added 2026-05-16):** After this brief ships AND post-deploy Spearman crosses +0.30, the operator must manually flip `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env` and run `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker`. Until then the cohort job stays dormant. Cowork-side scheduled reminder couldn't be auto-created (tool needs interactive approval); operator may want to do so with: `/schedule "every Monday 9am, check whether docs/STRATEGY/QUEUED/f-composite-reweight-no-renormalize.md has shipped and whether CHILI_COHORT_PROMOTE_ENABLED is ready to enable"`.

> **Type:** Math/formula change in `compute_quality_composite_score` + companion `weight_sum` validation fix.
> **Parent:** `f-composite-quality-reweight-realized-evidence` (shipped 2026-05-16, commits c4cf1ba → a82cc16).
> **Status:** unblocked. Small scope, ~50 LOC. Safe to dispatch via daemon as a single deliverable.

## Why this is needed

The parent brief reweighted the composite formula toward realized PnL evidence and shipped successfully — Spearman(score, total_pnl) went from −0.7570 (p=0.0044) to −0.2587 (p=0.42). The anti-correlation is no longer statistically significant. But the brief's stricter success threshold of rho ≥ +0.30 was **not met**, and the D5 anti-correlation regression test fails-by-design to document the cause.

The cause is a re-normalization arm in `compute_quality_composite_score`. The parent brief specified that when `n_realized < 5`, the realized component contributes 0 and the remaining five non-realized weights re-normalize (each multiplied by `1 / (1 - w_realized) = 1.538`) so the composite stays in `[0, 1]`.

The unintended consequence: a pattern with `n<5` and strong non-realized components (high CPCV-saturated, high directional_wr) gets its score inflated by 53.8% to compensate for the missing realized signal. Pattern 1068 (n=4, score 0.892) outranks pattern 585 (n=85, score 0.736) **purely because 585's realized component is included** while 1068's isn't. The very thing we measured most — 85 trades of +1.63% avg PnL — is what drags 585 down to 0.736; 1068 with zero realized evidence gets a free 1.538× multiplier.

This is backwards. Patterns with proven track records should rank higher than patterns with only backtest evidence. The fix is to drop the re-normalization.

## Goal

Replace the `n_realized < 5` re-normalization with a "raw partial sum (max 0.65), no inflation" rule. Patterns without enough realized data simply cap at 0.65; patterns with realized data can reach up to 1.00.

This makes the realized component **load-bearing** for top-ranking — a pattern can't reach the top of the composite ranking without a real track record.

## Design

### Current (post-D1, broken)

```python
def compute_quality_composite_score(
    pat, directional_wr, decay, weights,
    realized_pnl_score, realized_n_trades,
):
    # ... compute cpcv_n, dsr_n, pbo_inv, wr, dec_inv normally ...

    w_realized = weights.get("realized", 0.35)

    if realized_n_trades >= 5 and realized_pnl_score is not None:
        # Full formula
        realized_component = realized_pnl_score * realized_evidence_score(
            realized_n_trades, weights.get("realized_evidence_tau", 30.0),
        )
        return (
            weights["cpcv_sharpe"] * cpcv_n
            + weights["deflated_sharpe"] * dsr_n
            + weights["pbo_inverse"] * pbo_inv
            + weights["directional_wr"] * wr
            + weights["decay_inverse"] * dec_inv
            + w_realized * realized_component
        )
    else:
        # RE-NORMALIZATION ARM (the bug)
        renorm = 1.0 / (1.0 - w_realized)  # 1.538 at default w_realized=0.35
        return renorm * (
            weights["cpcv_sharpe"] * cpcv_n
            + weights["deflated_sharpe"] * dsr_n
            + weights["pbo_inverse"] * pbo_inv
            + weights["directional_wr"] * wr
            + weights["decay_inverse"] * dec_inv
        )
```

### Proposed

```python
def compute_quality_composite_score(
    pat, directional_wr, decay, weights,
    realized_pnl_score, realized_n_trades,
):
    # ... compute cpcv_n, dsr_n, pbo_inv, wr, dec_inv normally ...

    w_realized = weights.get("realized", 0.35)

    # Non-realized partial sum (caps at 1 - w_realized = 0.65 with default weights)
    non_realized = (
        weights["cpcv_sharpe"] * cpcv_n
        + weights["deflated_sharpe"] * dsr_n
        + weights["pbo_inverse"] * pbo_inv
        + weights["directional_wr"] * wr
        + weights["decay_inverse"] * dec_inv
    )

    if realized_n_trades >= 5 and realized_pnl_score is not None:
        realized_component = realized_pnl_score * realized_evidence_score(
            realized_n_trades, weights.get("realized_evidence_tau", 30.0),
        )
        return non_realized + w_realized * realized_component

    # No re-normalization. Patterns without enough realized data cap at 0.65.
    return non_realized
```

That's it. Net change: ~10 lines edited, one function. No new helpers, no new settings.

### Companion fix: weight_sum validation

`compute_and_persist_scores` currently logs `weights sum to 121.0100` (warning) because the sum includes the non-weight parameters (`realized_pnl_normalizer_pct=0.01`, `realized_evidence_tau=30.0`, `realized_window_days=90`). Cosmetic but confusing. Fix: sum ONLY the six actual weights.

```python
def _resolve_weights(settings_) -> dict:
    return {
        # weights (must sum to 1.0)
        "cpcv_sharpe":   ...,
        "deflated_sharpe": ...,
        "pbo_inverse":   ...,
        "directional_wr": ...,
        "decay_inverse": ...,
        "realized":      ...,
        # parameters (NOT summed)
        "realized_pnl_normalizer_pct": ...,
        "realized_evidence_tau":       ...,
        "realized_window_days":        ...,
    }


_WEIGHT_KEYS = (
    "cpcv_sharpe", "deflated_sharpe", "pbo_inverse",
    "directional_wr", "decay_inverse", "realized",
)


def compute_and_persist_scores(...):
    weights = _resolve_weights(settings_)
    weight_sum = sum(weights[k] for k in _WEIGHT_KEYS)
    if not (0.99 <= weight_sum <= 1.01):
        raise ValueError(  # tighten from warning to error
            f"composite weights sum to {weight_sum:.4f}, expected 1.0; "
            f"check chili_cohort_score_weight_* settings"
        )
    ...
```

The brief specified this tightening (warning → ValueError) but the parent shipped only the helpers, not the validation. Land it here.

## Deliverables

D1. **`app/services/trading/pattern_quality_score.py`** — replace the re-normalization arm. Drop the `_WEIGHT_KEYS`-scoped sum into `compute_and_persist_scores` (tighten warning to ValueError). ~20 LOC of changes total.

D2. **`tests/test_composite_reweight.py`** — flip three currently-failing tests to passing:
- `test_anti_correlation_new_formula_produces_positive_spearman` — expect rho > 0.5 (currently rho=-0.679)
- `test_anti_correlation_flips_sign` — expect old<0<new (currently both = -0.679)
- Add `test_renormalization_arm_removed` — assert that pattern with n=4 and all maxed non-realized components scores ≤ 0.65 (not 1.0 as before).
- Adjust `test_renormalization_when_realized_absent` (currently expects 1.0) to expect 0.65 cap.
- Adjust `test_realized_component_zero_when_n_below_floor` so it asserts both cases produce the same partial-sum (≤ 0.65), not the re-normalized 1.0.

D3. **Deploy + Spearman re-measurement**. Same dispatch pattern as parent's D6: force-recreate workers, run `compute_and_persist_scores`, run the Spearman probe. Success threshold: **rho ≥ +0.30 at n=12**.

D4. **CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-{N}_f-composite-reweight-no-renormalize.md`** — capture the rho number, the new top-15 ranking (expect 585 in top 3), and the regression test transitions.

## Hard constraints

- No re-normalization. Period.
- `compute_quality_composite_score` return value remains in `[0, 1]` — verify with property test if helpful.
- D5 anti-correlation regression tests (the ones currently failing in the parent commit `2e468fa`) must PASS after this fix. That is the brief's primary verification gate.
- `CHILI_COHORT_PROMOTE_ENABLED` stays OFF. Operator decides whether to flip after D3 verification.
- One commit per deliverable.
- TEST_DATABASE_URL ends in `_test`.
- Anti-truncation discipline on `pattern_quality_score.py` (wc -l + ast.parse + git diff --stat after every Edit).

## Acceptance

- Post-deploy Spearman(score, total_pnl) ≥ +0.30 (sign flip from −0.2587).
- Pattern 585 in top 3 by new composite.
- The previously-failing D5 anti-correlation tests pass.
- `weight_sum` log line shows 1.0 (no more 121.01 warning).

## Predicted Spearman with this fix

Re-running the math on the production 12-pattern data:

- Pattern 1068 (n=4): currently 0.892 (with re-normalization). Without re-normalization: cpcv_clipped=1.0, dsr=1.0, pbo_inv=1.0, wr ≈ 0.93, dec_inv ≈ 0.9 → 0.10×1.0 + 0.05×1.0 + 0.05×1.0 + 0.35×0.93 + 0.10×0.9 = 0.10 + 0.05 + 0.05 + 0.3255 + 0.09 = **0.6155**. Capped at 0.65.
- Pattern 1067 (n=2): similar to 1068. Cap around 0.60.
- Pattern 585 (n=85): cpcv_clipped=0.705, dsr=1.0, pbo_inv=1.0, wr ≈ 0.97, dec_inv ≈ 1.0, realized_pnl_score=1.0 (saturates at 1.63%/trade), evidence(85)=0.94 → non_realized = 0.10×0.705 + 0.05×1.0 + 0.05×1.0 + 0.35×0.97 + 0.10×1.0 = 0.07 + 0.05 + 0.05 + 0.34 + 0.10 = 0.610. realized contrib = 0.35 × 1.0 × 0.94 = 0.329. Total = **0.939**.

So 585 should land at ~0.94, well above the n<5 cohort capped at 0.65. The Spearman should be strongly positive. Predicted rho ≈ +0.6 to +0.8.

## Operator activation after this ships

If Spearman lands at the predicted +0.5 to +0.8, the operator can:

1. Re-run `compute_and_persist_scores` to confirm scores stabilize across multiple refreshes.
2. Wait 1 week for new realized data to accumulate (some n<5 patterns may cross to n≥5 and become trade-eligible).
3. Flip `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env`.
4. `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker`.
5. First Sunday 22:00 PT cohort job runs against the new formula.

Until then, `CHILI_COHORT_PROMOTE_ENABLED` stays OFF.
