# NEXT_TASK: f-phase5o-remaining-runtime-compat-map

STATUS: QUEUED

## Goal

Map the remaining 93 `orm_trade_symbol_compat` files into "leave alone",
"adapter candidate", and "future rename blocker" buckets so the next Phase 5
slice is chosen by trading risk rather than by text-search convenience.

## Current State

Phase 5L-I pinned the ORM contract groups. Phase 5L-J removed the low-risk
private-helper wording hits. Phase 5M/N made live source posture observable and
guarded.

The remaining ORM symbol surface is real compatibility, not obvious comment
noise:

```text
orm_trade_symbol_compat  | 93
learning_research_reporting | 39
live_action_broker_reconcile | 15
private_helper_type_only     | 7
public_ui_schema_contract    | 14
risk_capital_gate            | 18
```

## Recommended Work Shape

1. Generate the current `orm_trade_symbol_compat` inventory.
2. For each contract group, identify which files are:
   - public/stable contracts to leave alone
   - read-only reporting/learning consumers
   - live broker/capital/risk paths requiring extra probes before change
   - plausible adapter-helper candidates
3. Produce a CC report and, if useful, a JSON or Markdown map under
   `docs/STRATEGY/`.
4. Pick the next implementation slice only after the map is explicit.

## Guardrails

- No mechanical ORM rename.
- No schema migration.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No broker/order/close/reconcile behavior changes in the mapping slice.
- Keep Phase 5M/N source-posture guard green.

