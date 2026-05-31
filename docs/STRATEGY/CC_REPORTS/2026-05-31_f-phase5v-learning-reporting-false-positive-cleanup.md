# Phase 5V - Learning/Reporting False-Positive Cleanup

Date: 2026-05-31

## Summary

Removed four text-only `Trade` references from learning/reporting files that
were being counted as legacy ORM compatibility surfaces even though they do not
import or query the `Trade` ORM class.

Runtime behavior did not change.

## Files Cleaned

- `app/services/trading/economic_ledger.py`
- `app/services/trading/evidence_correction.py`
- `app/services/trading/pattern_trade_analysis.py`
- `app/services/trading/realized_pnl_sql.py`

These were docstring/comment naming issues only. The actual code paths remain
the same.

## Validation

- `python -m py_compile app/services/trading/realized_pnl_sql.py app/services/trading/economic_ledger.py app/services/trading/pattern_trade_analysis.py app/services/trading/evidence_correction.py`
- `python -m json.tool docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`
- `python scripts/analyze_phase5_remaining_trade_refs.py --json --fail-on-unexpected-runtime`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_phase5_remaining_trade_refs.py -q`

Result: analyzer green, 10 classifier tests passed, zero unexpected runtime
readers, zero unexpected runtime mutations, zero unclassified references.

## Compatibility Surface

Before:

- `orm_trade_symbol_compat`: 79
- `learning_research_reporting`: 25
- `adapter_candidate`: 30

After:

- `orm_trade_symbol_compat`: 75
- `learning_research_reporting`: 21
- `adapter_candidate`: 26

## Architect Verdict

Useful cleanup. This keeps the Phase 5 map focused on real runtime dependencies
instead of prose. No deploy-sensitive logic changed, but the reduced map makes
the next true adapter slice easier to choose safely.
