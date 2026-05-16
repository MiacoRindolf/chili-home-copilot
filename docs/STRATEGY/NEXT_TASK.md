# NEXT_TASK: f-multiple-testing-discipline (Phase E of evidence-fidelity)

STATUS: DONE

## Goal

**Phase E (final) of evidence-fidelity-architecture.** Fix the DSR
multiple-testing correction wiring — `cpcv_gate.py:120` hardcoded
`n_hypotheses_tested=1`, which effectively disabled the DSR (Bailey &
López de Prado 2014) deflation. Add Benjamini-Hochberg family-level
FDR accounting per Harvey-Liu-Zhu (2016), shadow-logged for 7 days
behind a default-off flag (`chili_family_fdr_enabled`).

## Brief

`docs/STRATEGY/QUEUED/f-multiple-testing-discipline.md`

Parent: `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

Prior phases shipped:
- A `ca1705f` — canonical outcome split
- B `51da8cc` — execution-truth wiring
- C `340215f` — triple-barrier label scheduler
- D `e5a04e5` — netedge live wiring

## Deliverables (per brief)

1. `promotion_gate._count_variants_in_family` helper (D1+D2)
2. `cpcv_gate.py:120` — replace hardcoded `=1` with family count (D1)
3. Migration 242 — `pattern_family_trial_log` table (D3)
4. `app/services/trading/family_fdr.py` — BH math + shadow logger (D4)
5. `cpcv_adaptive_gate.py` integration + `chili_family_fdr_enabled` flag (D5+D6)
6. `tests/test_multiple_testing_discipline.py` — 17 tests, all PASS (D7)
7. CC_REPORT `2026-05-14_multiple-testing-discipline.md` (D8)

## Hard constraints

- Default fallback when no family info: `n_hypotheses_tested=1` (no regression)
- Flag-gated: `chili_family_fdr_enabled` defaults False; BH adjustment shadow-logged
- Reads Phase A `corrected_*` columns (no autotrader / venue / broker touched)
- Migration additive only (new table + index)
- TEST_DATABASE_URL must end in `_test`

## Result

17 tests PASS. Phase E (final) of evidence-fidelity arc complete.
