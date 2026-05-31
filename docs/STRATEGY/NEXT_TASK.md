# NEXT_TASK: f-position-identity-phase-5ad-orm-alias-plan

STATUS: PENDING

## Goal

Design the eventual `Trade` ORM-symbol rename/alias strategy without changing live behavior.

Phase 5AC proved the system is at an intentional compatibility boundary:

- runtime raw `trading_trades` readers are gone
- unexpected runtime mutations are gone
- high-value display loaders are already on `trading_management_envelopes`
- 94 remaining `Trade` ORM symbols are public/live/risk/research contracts

The next move is a plan and canary slice, not a broad rename.

## Recommended Work Shape

1. Audit `app/models/trading.py::Trade` and decide whether a future `ManagementEnvelope` ORM class can be introduced as:
   - a new class mapped to `trading_management_envelopes`
   - an alias/facade around the existing `Trade` class
   - or a no-op because public vocabulary must remain `Trade`
2. Define which names are external contracts and must stay stable:
   - `/trades`
   - `trade_id`
   - schema class names
   - UI labels
   - compatibility view `trading_trades`
3. Define which internal paths could eventually migrate first:
   - private helper/type-only group
   - research/reporting group
4. Add tests/canaries only if they reduce future rename risk.
5. Do not change live broker/order/reconcile/risk behavior.

## Hard Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename public `/trades`, `trade_id`, schema classes, or UI labels.
- Do not touch monitor-run, close/sell, broker/order/reconcile, PDT, cash, capital, or portfolio gates.
- Do not mechanically replace `Trade` imports in live-action files.
- No migrations unless the plan proves a concrete no-risk need.

## Verification

Run:

```powershell
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
```

## Exit Criteria

- Future ORM rename strategy is explicit.
- Public compatibility names are listed and protected.
- First safe internal migration group, if any, is identified.
- No live behavior changes.

