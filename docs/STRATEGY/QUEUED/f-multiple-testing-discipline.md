# f-multiple-testing-discipline (Phase E of evidence-fidelity-architecture)

> **Type:** Fix `n_hypotheses_tested` wiring + add family-level FDR
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
> **Depends on:** Phase A (for family stat aggregation)

## Goal

DSR (Deflated Sharpe Ratio, Bailey & López de Prado 2014) already
deflates for multiple testing — IF you pass it the correct
`n_hypotheses_tested`. Currently `cpcv_gate.py:120` hardcodes
`n_hypotheses_tested=1`, which means **the multiple-testing correction
is effectively disabled**. Patterns get evaluated as if each were the
only hypothesis tested, which understates the hurdle.

This is NOT "raise hurdles." It's "use the hurdles correctly." The
research-backed answer to multi-pattern overfitting (Harvey-Liu-Zhu
2016) is family-level FDR (false-discovery-rate) control, not blanket
threshold inflation.

## Design

### Fix #1 (small): pass real `n_hypotheses_tested` to DSR

In `cpcv_gate.py:120` (and wherever `check_promotion_ready` is called),
compute and pass the variant count:

```python
n_hypo = _count_variants_in_family(sess, pattern)
ok, detail = check_promotion_ready(
    ensemble_rows,
    min_trades=_MIN_TRADES_FOR_GATE,
    n_hypotheses_tested=n_hypo,
    scan_pattern=pattern,
)
```

Helper:
```python
def _count_variants_in_family(sess, pattern):
    """Count siblings sharing this pattern's hypothesis_family or
    parent_id chain. Returns >=1.
    """
    fam = pattern.hypothesis_family
    if fam:
        return (
            sess.query(ScanPattern)
            .filter(ScanPattern.hypothesis_family == fam,
                    ScanPattern.active == True)
            .count() or 1
        )
    # Fallback: parent_id chain
    root_id = pattern.parent_id or pattern.id
    return (
        sess.query(ScanPattern)
        .filter(ScanPattern.parent_id == root_id,
                ScanPattern.active == True)
        .count() or 1
    )
```

### Fix #2 (medium): family-level FDR accounting

Add a `pattern_family_trial_log` table that tracks, per family, how
many variants have been tested at each threshold level. Use
Benjamini-Hochberg adjusted p-values for promotion decisions where
"family" replaces "individual pattern" as the unit of multiple-testing
control.

Schema:
```sql
CREATE TABLE pattern_family_trial_log (
  id BIGSERIAL PRIMARY KEY,
  hypothesis_family TEXT NOT NULL,
  variant_pattern_id INT NOT NULL REFERENCES scan_patterns(id),
  evaluated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  variant_dsr DOUBLE PRECISION,
  variant_pbo DOUBLE PRECISION,
  variant_promoted BOOLEAN,
  family_best_dsr_at_time DOUBLE PRECISION,
  family_variants_tested_so_far INT
);
CREATE INDEX ix_pattern_family_trial_log_fam ON pattern_family_trial_log(hypothesis_family, evaluated_at DESC);
```

Promotion-gate adapter reads this and computes BH-adjusted threshold
for the family before admitting a new variant.

### Fix #3 (safety): drought-floor

Operator's concern: tighter discipline → fewer promotions → less
learning. **Counter-tuned safety:** the Phase 2 adaptive gate already
admits "top X% of pool" via empirical percentile thresholds. Combined
with family-FDR, the system bounds the promotion rate from BOTH sides:
- Family-FDR is the FLOOR (don't promote a variant that doesn't beat
  its siblings)
- Adaptive percentile is the CEILING (don't admit more than 5% of pool)

Drought cannot exceed `1 - (max_pool_size / total_active_patterns)`
asymptotically. Math-bounded.

## Deliverables

1. **`app/services/trading/promotion_gate.py`** — replace hardcoded
   `n_hypotheses_tested=1` with computed family-count
2. **Helper:** `_count_variants_in_family(sess, pattern)` (in
   `promotion_gate.py` or a new utility)
3. **Migration N+2:** create `pattern_family_trial_log` table
4. **`app/services/trading/family_fdr.py`** — new module with
   Benjamini-Hochberg threshold computation per family
5. **Wiring point:** `cpcv_adaptive_gate.py` reads the per-family
   BH-adjusted threshold instead of just the global percentile
6. **`tests/test_multiple_testing_discipline.py`** — synthetic family
   of 10 variants, verify FDR-adjusted threshold > naive threshold;
   verify single-variant family unchanged
7. **CC_REPORT**: `docs/STRATEGY/CC_REPORTS/2026-05-14_multiple-testing-discipline.md`

## Hard constraints

- Default behavior: when no family info available, fall back to
  `n_hypotheses_tested=1` (current behavior). No silent regression
  for legacy patterns.
- Flag-gated rollout: `chili_family_fdr_enabled` (default False) gates
  the BH adjustment. Shadow-log the proposed thresholds for 7 days
  before flipping.
- Reads Phase A `corrected_*` columns (don't bias on raw realized).
- No autotrader / venue / broker touched.

## Consult gate

Family grouping rule — use `hypothesis_family` column (clean) vs derive
from `parent_id` chain (legacy patterns may not have family set)? Brief
default: prefer `hypothesis_family`, fall back to `parent_id`. CC should
confirm.

## Why this matters for the drought concern

Operator (rightly) worried: "tighter hurdles → fewer promotions →
less learning."

**Architect answer:** family-FDR is NOT tighter hurdles per pattern.
It's *correct* hurdles per family. Concretely:

Before (broken): pattern 731 is one of 12 BB-squeeze 1m variants. All
get evaluated as if they're the only hypothesis. ~8 of 12 spuriously
clear DSR≥0.95 (noise + selection bias). False discoveries.

After (fixed): the family gets ONE trial-slot. The BEST variant
(highest shrunken DSR) is admitted; siblings are not double-counted.
Total live promotions per family: 1 instead of 8. Net effect on roster:
same total promotion count, but each one is the *winner of its family*
rather than 8 noisy siblings.

**The drought doesn't worsen.** What changes: the patterns that DO
promote are higher-quality (winners of multi-variant tournaments).
That's what you want.
