# Phase 5W - NetEdge Training Envelope Adapter

Date: 2026-05-31

## Summary

Converted the NetEdge calibrator's live training-row source from the legacy
`Trade` ORM surface to a semantic management-envelope helper.

This is a narrow learning/reporting adapter slice. It does not change broker,
order, close, reconcile, PDT, capital, portfolio, risk, or pattern lifecycle
behavior.

## What Changed

- Added `load_net_edge_training_envelope_rows(...)` in
  `app/services/trading/management_envelopes.py`.
- Updated `net_edge_ranker._load_training_pairs(...)` so the live outcome
  half reads from `trading_management_envelopes`.
- Left the paper-trade outcome half unchanged.
- Preserved the old live-row selection semantics:
  - `exit_date IS NOT NULL`
  - `entry_date >= cutoff`
  - `ORDER BY exit_date DESC NULLS LAST`
  - `LIMIT 2000`
- Updated Phase 5 compatibility map/count tests.

## Validation

- `python -m py_compile app/services/trading/management_envelopes.py app/services/trading/net_edge_ranker.py`
- `python -m json.tool docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`
- `python scripts/analyze_phase5_remaining_trade_refs.py --json --fail-on-unexpected-runtime`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_management_envelopes.py tests/test_net_edge_ranker_envelope_rows.py tests/test_phase5_remaining_trade_refs.py -q`

Result: 38 focused tests passed. Analyzer remains green with zero unexpected
runtime readers, zero unexpected runtime mutations, and zero unclassified app
references.

## Compatibility Surface

Before:

- `orm_trade_symbol_compat`: 75
- `learning_research_reporting`: 21
- `adapter_candidate`: 26

After:

- `orm_trade_symbol_compat`: 74
- `learning_research_reporting`: 20
- `adapter_candidate`: 25

## Architect Verdict

Safe as a narrow row-source conversion. NetEdge already has fail-open semantics
and remains non-authoritative unless explicitly configured. The calibrator input
contract is preserved while removing one more direct dependency on the legacy
`Trade` ORM compatibility surface.

Continue avoiding broad `learning.py`, lifecycle decay/promotion, and live
broker/reconcile/risk/capital paths without dedicated parity probes.
