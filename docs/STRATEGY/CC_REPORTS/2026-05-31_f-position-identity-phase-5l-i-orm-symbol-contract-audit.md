# Phase 5L-I - ORM Symbol Contract Audit

Date: 2026-05-31

## Summary

Phase 5L-I audits and pins the remaining legacy `Trade` ORM symbol surface by
contract group.

This deliberately does not rename `Trade`, `trade_id`, public API fields,
schemas, UI labels, broker/order/close paths, or database relations. It turns
the remaining compatibility surface into a monitored contract so accidental new
dependencies are visible.

## Inventory

The runtime app has no unexpected raw compatibility readers or mutations:

```text
unexpected_runtime_readers   = []
unexpected_runtime_mutations = []
```

The remaining app-side ORM symbol surface is:

```text
orm_trade_symbol_compat | 96 files

learning_research_reporting | 39
live_action_broker_reconcile | 15
private_helper_type_only     | 10
public_ui_schema_contract    | 14
risk_capital_gate            | 18
```

Representative paths are now pinned in
`tests/test_phase5_remaining_trade_refs.py` so future movement across the
contract groups is intentional rather than accidental.

## Architect verdict

The large legacy `Trade` surface is not one thing. It contains public API/UI
contracts, real broker/reconcile paths, capital/risk gates, learning/reporting
readers, and private helper/type-only imports. A mechanical ORM rename would
mix those concerns and create avoidable trading risk.

The right next step is to peel off the smallest low-risk group:
`private_helper_type_only`. That can reduce symbol noise without touching live
broker/order/close behavior.
