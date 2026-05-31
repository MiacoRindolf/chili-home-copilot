# NEXT_TASK: f-position-identity-phase-5aa-b-active-setup-endpoint-conversion

STATUS: PENDING

## Goal

Convert the active setup card endpoint's loader to use runtime management-envelope objects from `trading_management_envelopes`, using the Phase 5AA-A parity probe as the gate.

## Why This Is Safe Now

Phase 5AA-A compared current `Trade` ORM runtime objects with candidate envelope runtime objects through the same active setup serializer chain. Live result was `COMPLETE_POSITIVE`:

- 5 old setups = 5 new setups
- 0 suppressed-stale drift
- matched=true
- `trading_management_envelopes` is the physical table (`relkind='r'`)
- `trading_trades` remains the compatibility view (`relkind='v'`)

## Required Work Shape

1. Add or reuse a helper that loads open active-setup runtime objects from `trading_management_envelopes`.
2. Convert only `api_monitor_active` / `_monitored_live_trades_with_suppressed` loader plumbing to use the proven helper.
3. Keep the serializer and helper chain intact:
   - broker-stale filtering
   - broker-position truth overlays
   - option detection
   - broker/market quote routing
   - breakout-alert and pattern enrichment
   - monitor decision enrichment
   - execution-state metadata
4. Re-run the Phase 5AA-A parity probe after conversion.

## Hard Guardrails

- Do not touch `api_monitor_run`.
- Do not touch sell/close behavior.
- Do not touch stop execution/evaluation/dispatch.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Verification

Run:

```powershell
python -m py_compile app\routers\trading_sub\monitor.py app\services\trading\management_envelopes.py
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_monitor_api_execution_state.py tests\test_phase5aa_active_setup_runtime_adapter_probe.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
$env:DATABASE_URL='postgresql://chili:chili@localhost:5433/chili'
python scripts\d-phase5aa-active-setup-runtime-adapter-probe.py
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

## Exit Criteria

- Active setup endpoint behavior is unchanged.
- Phase 5AA-A live probe remains `COMPLETE_POSITIVE`.
- Focused tests pass.
- Analyzer raw reader bucket remains 0.

