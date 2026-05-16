# CC_REPORT: f-multiple-testing-discipline (Phase E of evidence-fidelity)

## What shipped

Phase E (final phase) of f-evidence-fidelity-architecture. Fixes the DSR
multiple-testing correction wiring (was effectively disabled by a
hardcoded `n_hypotheses_tested=1`) and adds family-level FDR
accounting via Benjamini-Hochberg, shadow-logged for 7 days behind a
default-off flag.

Files touched (8):

- `app/services/trading/promotion_gate.py` — added
  `_count_variants_in_family(sess, pattern)` (~50 LOC). Prefers
  `hypothesis_family` (clean), falls back to `parent_id` chain (legacy).
  Always returns `>=1` so the gate never sees a degenerate count.
- `app/services/trading/brain_work/handlers/cpcv_gate.py` — replaced
  the hardcoded `n_hypotheses_tested=1` at line 120 with the computed
  family count. Imports `_count_variants_in_family` from promotion_gate.
- `app/services/trading/family_fdr.py` — **new** module. Pure-math
  `bh_adjusted_dsr_threshold(naive_threshold, m)`, `family_fdr_enabled()`
  flag predicate, `log_family_trial(...)` shadow writer,
  `family_size_for_pattern(...)` ID-only resolver, `family_best_dsr(...)`
  audit-replay helper. ~180 LOC.
- `app/services/trading/cpcv_adaptive_gate.py` — `_evaluate_adaptive`
  now takes `family_size` and surfaces `pool_threshold_naive` /
  `pool_threshold_bh` / `family_fdr_applied` on the DSR metric row.
  `maybe_apply_adaptive_gate` looks up family size + label once via the
  candidate pattern row (same pattern as the composite read), and
  shadow-writes one row to `pattern_family_trial_log` per evaluation.
- `app/config.py` — `chili_family_fdr_enabled: bool = False`.
- `app/migrations.py` — `_migration_242_pattern_family_trial_log`.
  Additive only: new table + index, FK to `scan_patterns`, no DML on
  existing tables. Idempotent (`CREATE ... IF NOT EXISTS`).
- `tests/test_multiple_testing_discipline.py` — **new**, 17 tests.
- `docs/STRATEGY/CC_REPORTS/2026-05-14_multiple-testing-discipline.md`
  (this file).

Migrations added: **1** (`242_pattern_family_trial_log`).

Verdict authority at merge: legacy. The BH-adjusted threshold is
**computed and surfaced** in the adaptive gate's shadow log on every
evaluation, but only **applied** to the verdict when
`chili_family_fdr_enabled=True`. Default OFF → byte-identical wrapper
behavior, as required by the brief's hard constraints.

## Verification

```
TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
  conda run -n chili-env python -m pytest \
  tests/test_multiple_testing_discipline.py -v -p no:asyncio
```

Result: **17 passed, 0 failed** (568.64s).

Coverage:

1. BH math (pure):
   - `bh_adjusted_dsr_threshold(0.95, 1) == 0.95` — `m=1` no-op
   - `bh_adjusted_dsr_threshold(0.95, 10) == 0.995` — naive 0.95 ⇒ BH
     0.995 (matches `alpha / m = 0.05 / 10 = 0.005`)
   - Monotone in `m`: thresholds at `m ∈ {1, 2, 5, 10, 25, 100}` are
     non-decreasing
   - Clamps for out-of-range naive; non-int `m` falls back to naive
2. Family-size resolver:
   - Uses `hypothesis_family` first; ignores inactive siblings and
     other-family patterns
   - Falls back to `parent_id` chain when family is NULL (legacy)
   - Returns 1 when no family info at all
   - Handles `None` pattern / `None` session
3. Adaptive gate surfacing:
   - DSR row carries both `pool_threshold_naive` and `pool_threshold_bh`;
     BH > naive for `family_size=10`
   - `family_size=1` ⇒ BH == naive (no-op confirmed end-to-end)
4. **Flag-off byte-identical**: 10-variant family with flag OFF returns
   the legacy `(ok, reasons)` tuple verbatim
5. Trial-log shadow write: one row per evaluation in
   `pattern_family_trial_log` with running family count snapshot
6. Migration shape verified via `information_schema.columns` lookup

Regression check on existing CPCV gate tests (run in parallel — results
will land before commit).

AST parse-clean on every modified file:

```
conda run -n chili-env python -c "import ast; \
  ast.parse(open('app/services/trading/cpcv_adaptive_gate.py').read()); \
  ast.parse(open('app/services/trading/family_fdr.py').read()); \
  ast.parse(open('app/services/trading/promotion_gate.py').read()); \
  ast.parse(open('app/services/trading/brain_work/handlers/cpcv_gate.py').read()); \
  ast.parse(open('app/migrations.py').read()); \
  ast.parse(open('app/config.py').read()); \
  ast.parse(open('tests/test_multiple_testing_discipline.py').read()); \
  print('OK')"
# OK
```

## Surprises / deviations

1. **Pytest-asyncio collection error** — pre-existing
   `AttributeError: 'Package' object has no attribute 'obj'` from
   pytest-asyncio 0.23.3 vs pytest 9.0.2. Worked around with
   `-p no:asyncio` (pytest plugin disable). Affects every pytest run,
   not specific to this phase. Worth a separate cleanup task to pin
   compatible pytest-asyncio.
2. **Adaptive gate shadow log unchanged** — I added new fields
   (`pool_threshold_naive`, `pool_threshold_bh`, `family_size`,
   `family_fdr_applied`) to the DSR `metric_row` dict, but
   `_write_eval_log` explicitly maps only the legacy keys into its
   INSERT payload, so the `cpcv_adaptive_eval_log` schema is unchanged.
   Phase F could widen that schema if operators want the BH columns
   directly queryable; for now `pattern_family_trial_log` is the
   authoritative BH-audit surface.
3. **`promotion_gate.py` line numbers** — the brief named lines
   880-930 / 933 for the call-site replacement, but the only
   *hardcoded* `=1` lives at `cpcv_gate.py:120` (the brain_work
   handler). `promotion_gate.finalize_promotion_with_cpcv` already
   parameterizes `n_hypotheses_tested` correctly; the helper is added
   there because the handler imports from `promotion_gate`. This
   matches the brief's intent — fix the call site that hardcoded 1.
4. **Consult-gate question answered as defaulted**: family grouping
   rule = prefer `hypothesis_family` column, fall back to `parent_id`
   chain. Implemented as defaulted. No deviation.

## Deferred

- **7-day soak window**: not flipping `chili_family_fdr_enabled` yet.
  Default OFF per brief. Operator flips after `pattern_family_trial_log`
  has accumulated divergence evidence.
- **Wider audit schema in `cpcv_adaptive_eval_log`**: deferred to
  Phase F or beyond. Today's BH-audit surface is the dedicated
  `pattern_family_trial_log` table.
- **PBO / median-Sharpe FDR adjustment**: only DSR is BH-adjusted in
  this phase. PBO is bounded above (lower is better) — the same BH
  trick on its upper-CI doesn't add value at single-family granularity;
  median Sharpe lacks a closed-form null. Keeping the BH treatment to
  DSR matches the brief's "fix wrongly-disabled correction" framing
  rather than expanding scope.
- **Roster-replay tool**: a CLI that walks `pattern_family_trial_log`
  and recomputes "which promotions would have been blocked / admitted
  by BH" is a natural Phase F follow-up. Not in this phase's
  deliverables.

## Open questions for Cowork

1. **Flip cadence after soak.** The brief's drought-floor argument
   assumes BH bounds promotion *count* from below; in practice we'll
   know whether that holds once
   `SELECT hypothesis_family, COUNT(*) FROM scan_patterns WHERE active GROUP BY 1`
   has had a week of trial-log rows. Should we wait for an explicit
   "flip" task brief, or autopilot after 7 days if divergence is
   bounded? Default: wait for an explicit task.
2. **Hypothesis-family backfill.** Many legacy patterns have
   `hypothesis_family=NULL` (the helper falls back to `parent_id`).
   `parent_id` chains exist on most evolved variants but not on
   hand-seeded patterns. Worth a separate hygiene task to label every
   active pattern with a family string so the trial-log row is keyed
   (rows with no family are skipped today — they bypass BH entirely,
   matching the `=1` legacy floor).
3. **`pytest-asyncio` pin.** The 0.23.3 collection error on pytest 9
   is real and recurring. Worth a one-off cleanup PR.
