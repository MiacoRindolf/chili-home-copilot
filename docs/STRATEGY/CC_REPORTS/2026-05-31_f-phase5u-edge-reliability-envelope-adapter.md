# Phase 5U - Edge Reliability Envelope Adapter

Date: 2026-05-31

## Summary

Converted the live closed-row source used by `edge_reliability.py` from the
legacy `Trade` ORM surface to a semantic management-envelope helper.

This is a read-only learning/reporting slice. It does not change broker/order,
close/reconcile, PDT, capital, risk, pattern promotion, or pattern demotion
behavior.

## What Changed

- Added `load_edge_reliability_live_envelope_rows(...)` in
  `app/services/trading/management_envelopes.py`.
- Updated `compute_pattern_edge_reliability(...)` to read live evidence rows
  from `trading_management_envelopes` through that helper.
- Updated `_observed_asset_slices_for_pattern(...)` to use the same helper for
  recent envelope asset-slice discovery.
- Removed the direct `Trade` ORM import from `edge_reliability.py`.
- Updated the Phase 5O compatibility map and analyzer count tests.

## Validation

- `python -m py_compile app/services/trading/management_envelopes.py app/services/trading/edge_reliability.py`
- `python scripts/analyze_phase5_remaining_trade_refs.py --json --fail-on-unexpected-runtime`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_management_envelopes.py tests/test_phase5_remaining_trade_refs.py tests/test_edge_reliability.py -q`

Result: 52 focused tests passed. Analyzer remains green with zero unexpected
runtime readers, zero unexpected runtime mutations, and zero unclassified app
references.

## Compatibility Surface

Before:

- `orm_trade_symbol_compat`: 80
- `learning_research_reporting`: 26
- `adapter_candidate`: 31

After:

- `orm_trade_symbol_compat`: 79
- `learning_research_reporting`: 25
- `adapter_candidate`: 30

## Architect Verdict

Safe narrow adapter slice. Edge reliability remains aggregate-only and still
writes the same durable brain-work snapshots. The only change is the live
evidence row source: the old compatibility `Trade` ORM read is gone, and the
semantic envelope read now owns that contract.

Continue this pattern: small, testable learning/reporting reads first; leave
broker/reconcile/risk/capital and lifecycle mutators alone unless a dedicated
parity probe proves the path.
