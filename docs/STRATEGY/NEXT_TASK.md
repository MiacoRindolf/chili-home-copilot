# NEXT_TASK: f-position-identity-phase-5l-j-private-helper-orm-surface

STATUS: QUEUED

## Goal

Reduce the `private_helper_type_only` legacy `Trade` ORM symbol surface where it
is genuinely low-risk, without touching public API/UI names, broker/order/close
paths, reconcile logic, schema, or database relations.

## Current State

Phase 5L-I pinned the remaining app-side ORM contract groups:

```text
orm_trade_symbol_compat | 96 files

learning_research_reporting | 39
live_action_broker_reconcile | 15
private_helper_type_only     | 10
public_ui_schema_contract    | 14
risk_capital_gate            | 18
```

Representative paths are pinned in
`tests/test_phase5_remaining_trade_refs.py`.

## Recommended Work Shape

1. List only `private_helper_type_only` entries from
   `scripts/analyze_phase5_remaining_trade_refs.py --json`.
2. Split them into:
   - true model/export compatibility (`app/models/trading.py`,
     `app/services/trading/__init__.py`) that should remain
   - helper modules where a protocol/type alias or local wording cleanup can
     reduce `Trade` imports
3. Convert only one or two obvious helper imports if the code path is
   type-only and tests are direct.
4. Update the Phase 5L-I contract counts intentionally.
5. Re-run:
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - Phase 5K and Phase 5I live probes

## Guardrails

- No mechanical ORM rename.
- No schema migration.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- No broker/order/close/reconcile behavior changes.
- Do not touch the dirty live root.
