# NEXT_TASK: f-position-identity-phase-5ac-live-action-boundary-audit

STATUS: PENDING

## Goal

Audit the remaining `Trade` ORM-symbol surface and classify which parts are public compatibility contracts versus live-action/broker/order paths that must remain on the compatibility ORM until individually proven safe.

## Why This Comes Next

Phase 5AA/5AB moved the remaining parity-proven display loaders:

- active setup card display
- AutoTrader desk live `trades` display

The remaining surface is now mostly:

- public `/trades` compatibility
- monitor-run live action
- close/sell actions
- broker/order/reconcile paths
- PDT/capital/risk gates
- type annotations and public UI/schema labels

This is not a place for more mechanical conversion.

## Recommended Work Shape

1. Run the focused analyzer and group remaining `orm_trade_symbol_compat` files by risk:
   - public compatibility
   - live action
   - broker/order/reconcile
   - risk/capital/PDT
   - private helper/type-only
   - safe future display probe candidate
2. Produce a CC report with a go/no-go matrix.
3. Queue exactly one next safe slice, if any.

## Hard Guardrails

- Do not touch monitor-run behavior.
- Do not touch close/sell behavior.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Verification

Run:

```powershell
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
```

## Exit Criteria

- Remaining work is categorized by risk.
- No behavior changes.
- Next slice is explicit and bounded, or Phase 5 pauses at the compatibility boundary.

