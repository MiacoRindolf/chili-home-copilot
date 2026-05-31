# NEXT_TASK: f-position-identity-phase-5ab-b-autotrader-desk-live-loader-conversion

STATUS: PENDING

## Goal

Convert only the live `trades` loader inside `list_pattern_linked_open_positions(...)` to use runtime objects from physical `trading_management_envelopes`.

## Why This Is Safe Now

Phase 5AB-A live parity probe is green:

- `COMPLETE_POSITIVE`
- 5 old `Trade` rows = 5 new envelope runtime rows
- matched=true
- 0 suppressed-stale drift
- broker-truth / quote / override / control fields matched

## Required Work Shape

1. Add or reuse a helper that loads live AutoTrader desk envelope runtime objects from `trading_management_envelopes`.
2. Convert only the live `trades` query in `list_pattern_linked_open_positions(...)`.
3. Keep the existing enrichment loop intact.
4. Keep the paper-trade query and paper row serialization unchanged.
5. Re-run the Phase 5AB-A live probe after conversion.

## Hard Guardrails

- Do not touch close/sell behavior.
- Do not touch override mutation behavior.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not touch paper-trade rows except as unchanged payload members.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Verification

Run:

```powershell
python -m py_compile app\services\trading\autotrader_desk.py app\services\trading\management_envelopes.py scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_phase5ab_autotrader_desk_runtime_adapter_probe.py tests\test_autotrader_desk_api.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
$env:DATABASE_URL='postgresql://chili:chili@localhost:5433/chili'
$env:PHASE5AB_ALLOW_LIVE_PROBE='true'
python scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

## Exit Criteria

- Live desk `trades` list behavior unchanged.
- Phase 5AB-A live probe remains `COMPLETE_POSITIVE`.
- Focused tests pass.
- Analyzer raw reader bucket remains 0.
