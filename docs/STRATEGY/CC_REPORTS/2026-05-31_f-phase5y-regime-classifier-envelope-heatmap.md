# CC Report: Phase 5Y Regime Classifier Envelope Heatmap

Date: 2026-05-31
Branch: `codex/phase5y-regime-classifier-envelope-parity`

## Summary

Converted the regime/scanner Sharpe heatmap's closed-live-row source from the
legacy `Trade` ORM to the semantic management-envelope surface.

This heatmap is a reporting/analysis reader. It calculates 30-day realized
simple returns by regime and scanner bucket. It does not place orders, close
positions, change risk gates, or promote/demote patterns.

## What Changed

- Added `load_regime_scanner_heatmap_envelope_rows(...)` in
  `app/services/trading/management_envelopes.py`.
- Updated `regime_classifier.build_regime_scanner_sharpe_heatmap(...)` to use
  that helper for its closed-live trade input.
- Kept pattern lookup and regime-snapshot lookup unchanged.
- Removed the direct `Trade` ORM reader from `regime_classifier.py`.
- Updated the Phase 5 compatibility map and analyzer-count expectations.

## Verification

```text
python -m py_compile app/services/trading/management_envelopes.py app/services/trading/regime_classifier.py
python -m json.tool docs/STRATEGY/phase5o_remaining_runtime_compat_map.json
python scripts/analyze_phase5_remaining_trade_refs.py --json
TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_management_envelopes.py tests/test_regime_classifier_performance.py tests/test_phase5_remaining_trade_refs.py -q
```

Result:

```text
42 passed
unexpected_runtime_readers = []
unexpected_runtime_mutations = []
unclassified = []
orm_trade_symbol_compat = 72
adapter_candidate = 23
learning_research_reporting = 18
```

## Risk Read

Low. The reader pulls closed envelopes only and the resulting heatmap is an
observability/reporting surface. It does not alter live execution or lifecycle
state.

## Next Recommendation

Continue adapter-slice work on another passive learning/reporting candidate.
Good next inspection targets are `scanner.py`, `market_data.py`, or
`trading_scheduler.py`. Avoid broad `learning.py`, lifecycle decay, broker,
reconcile, and capital-gate surfaces.
