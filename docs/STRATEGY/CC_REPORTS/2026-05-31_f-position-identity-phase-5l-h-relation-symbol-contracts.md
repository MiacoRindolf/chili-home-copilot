# Phase 5L-H - Relation Symbol Contracts

Date: 2026-05-31

## Summary

Phase 5L-H centralized the remaining runtime-app `trading_trades` relation
symbol surface.

Before this slice, the classifier found two app-side compatibility relation
symbol owners:

```text
app/models/trading.py
app/services/trading/management_envelopes.py
```

After this slice, there is one explicit owner:

```text
app/models/trade_relation_symbols.py
```

## Changes

- Added `app.models.trade_relation_symbols` with:
  - `MANAGEMENT_ENVELOPES_RELATION`
  - `LEGACY_TRADES_COMPAT_RELATION`
  - `LEGACY_TRADE_ID_FK`
- Updated `Trade.__tablename__` and model foreign keys to use the shared
  compatibility symbols.
- Updated management-envelope helpers to import the shared relation symbols
  instead of defining their own literal.
- Updated the remaining-trade-reference canary to pin the single expected
  relation-symbol owner.

No broker, order, close, reconcile, schema, migration, capital, PDT, or
promotion behavior changed.

## Verification

```text
python -m py_compile app/models/trade_relation_symbols.py app/models/trading.py app/services/trading/management_envelopes.py scripts/analyze_phase5_remaining_trade_refs.py tests/test_phase5_remaining_trade_refs.py

python scripts/analyze_phase5_remaining_trade_refs.py --bucket compatibility_relation_symbol
compatibility_relation_symbol | 1
app/models/trade_relation_symbols.py

python scripts/analyze_phase5_remaining_trade_refs.py --fail-on-unexpected-runtime
ok=true, unexpected_runtime_readers=0, unexpected_runtime_mutations=0

pytest -q tests/test_phase5_remaining_trade_refs.py tests/test_management_envelopes.py
28 passed

pytest -q tests/test_phase5l_reader_allowlist.py tests/test_phase5_remaining_trade_refs.py
10 passed
```

## Architect verdict

This is the conservative version of relation-symbol cleanup: one canonical
compatibility contract file, no behavior change, and no ORM/public rename. The
remaining `Trade` ORM symbol surface is intentionally still present and should
be handled by a separate audit rather than a mechanical rename.
