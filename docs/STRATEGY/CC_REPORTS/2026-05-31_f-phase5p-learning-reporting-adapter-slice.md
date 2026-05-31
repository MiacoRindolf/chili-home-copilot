# Phase 5P - Learning/Reporting Adapter Slice

Date: 2026-05-31

## Summary

Phase 5P converts one read-only learning/reporting consumer away from the
legacy `Trade` ORM symbol.

`app/services/reasoning_brain/interest_graph.py` used `Trade` only to count
recent tickers for inferred interests. That read now goes through the
management-envelope read model via
`load_recent_management_envelope_tickers_for_user(...)`.

No broker/order/close/reconcile path, capital gate, public API, schema, or UI
label changed.

## Result

The remaining ORM compatibility surface dropped again:

```text
orm_trade_symbol_compat     | 93 -> 92
learning_research_reporting | 39 -> 38
adapter_candidate           | 44 -> 43
```

The Phase 5O map and canaries were updated to the new current inventory.

## Validation

```text
tests/test_reasoning_interest_graph.py                         -> passed
tests/test_phase5o_remaining_runtime_compat_map.py              -> passed
tests/test_phase5_remaining_trade_refs.py                       -> passed
tests/test_phase5l_reader_allowlist.py                          -> passed
scripts/d-phase5n-source-posture-watch.py                       -> COMPLETE_POSITIVE
```

## Architect Verdict

This is the kind of Phase 5 adapter slice that is worth repeating: a read-only
consumer, one semantic helper, direct test coverage, and a measurable reduction
in legacy ORM surface. Do not touch live broker/reconcile or risk/capital
groups until this pattern is boring.

