# NEXT_TASK: f-position-identity-phase-5ab-autotrader-desk-contract-audit

STATUS: PENDING

## Goal

Audit the AutoTrader desk position-list runtime object contract before any conversion away from direct `Trade` ORM objects.

## Why This Comes Next

The active setup display path is now converted and verified. The remaining user-facing desk surface in `app/services/trading/autotrader_desk.py` is riskier than a passive read: it combines open position display with broker-truth overlays, option/crypto quote routing, position overrides, controls metadata, and close capability flags.

Do not convert it blindly.

## Recommended Work Shape

1. Inspect `list_pattern_linked_open_positions(...)` in `app/services/trading/autotrader_desk.py`.
2. Classify each field in its returned payload:
   - passive display field
   - broker-truth/risk display field
   - override-control field
   - close/sell-control field
   - public API/UI compatibility field
3. Write a read-only old-vs-new runtime-adapter probe only if the display contract can be isolated from the action/control contract.
4. If parity is green, queue a later narrow conversion. If not, document blockers.

## Hard Guardrails

- Do not touch close/sell behavior.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not touch override mutation behavior.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Verification

At minimum:

```powershell
python -m py_compile app\services\trading\autotrader_desk.py
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_autotrader_desk_api.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

## Exit Criteria

- Audit report identifies exactly which desk fields are safe display reads versus control/action contracts.
- No behavior changes unless a dedicated parity probe proves safety.
- Analyzer raw reader bucket remains 0.

