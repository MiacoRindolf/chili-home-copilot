# NEXT_TASK: f-position-identity-phase-5l-i-orm-symbol-contract-audit

STATUS: QUEUED

## Goal

Audit and pin the remaining legacy `Trade` ORM symbol surface by contract group
without renaming the ORM class, public `/trades` API fields, `trade_id`, or any
live broker/order/close path.

## Current State

Phase 5L-H centralized the literal relation-symbol surface:

```text
compatibility_relation_symbol | 1
app/models/trade_relation_symbols.py
unexpected runtime readers     | 0
unexpected runtime mutations   | 0
```

The remaining surface is now mostly ORM symbol compatibility:

```text
orm_trade_symbol_compat | 96 files
```

Those files are already grouped by the classifier into:

```text
public_ui_schema_contract
live_action_broker_reconcile
risk_capital_gate
learning_research_reporting
private_helper_type_only
```

## Recommended Work Shape

1. Run `scripts/analyze_phase5_remaining_trade_refs.py --json` and capture the
   `orm_contract_groups` distribution.
2. Pin expected group counts and/or representative paths in tests so accidental
   new legacy `Trade` dependencies are visible.
3. Convert only low-risk private-helper wording or comments where it improves
   clarity.
4. Do not rename `Trade`, `trade_id`, public API fields, schemas, or UI labels.
5. Re-run:
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - Phase 5K and Phase 5I live probes

## Guardrails

- No mechanical ORM rename.
- No schema migration.
- No broker/order/close/reconcile behavior changes.
- No public API/UI rename.
- Do not touch the dirty live root.
