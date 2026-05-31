# Phase 5O - Remaining Runtime Compatibility Map

Date: 2026-05-31

## Summary

Phase 5O maps the remaining `orm_trade_symbol_compat` surface into risk-action
buckets before any further rename or adapter work.

The result is machine-checkable in
`docs/STRATEGY/phase5o_remaining_runtime_compat_map.json` and pinned by
`tests/test_phase5o_remaining_runtime_compat_map.py`.

## Inventory

Current remaining ORM compatibility surface:

```text
orm_trade_symbol_compat | 93

learning_research_reporting | 39
live_action_broker_reconcile | 15
private_helper_type_only     | 7
public_ui_schema_contract    | 14
risk_capital_gate            | 18
```

Phase 5O action buckets:

```text
adapter_candidate    | 44
future_rename_blocker | 33
leave_alone          | 16
```

## Interpretation

- `leave_alone`: public UI/API/schema vocabulary plus the core legacy model
  export surface. These names are user/API contracts or compatibility symbols,
  not cleanup targets.
- `adapter_candidate`: read-only learning/reporting consumers and private
  helper modules. These are plausible future adapter targets once focused
  parity tests exist.
- `future_rename_blocker`: live broker/reconcile and risk/capital gates. These
  must not be renamed or adapted without runtime probes because they influence
  order placement, close logic, capital allocation, PDT/cash gates, and
  position truth.

## Architect Verdict

The next implementation slice should not be a global ORM rename. The best
next move is a narrow reporting/learning adapter slice: one or two read-only
consumers, no broker writes, no capital gates, and direct parity tests.

That keeps Phase 5 moving while preserving the trading system's live safety
surface.

