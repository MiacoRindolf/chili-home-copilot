# NEXT_TASK: f-position-identity-phase-5ab-a-autotrader-desk-runtime-adapter-probe

STATUS: PENDING

## Goal

Add a read-only old-vs-new runtime-adapter parity probe for the live `trades` list returned by `list_pattern_linked_open_positions(...)`.

## Scope

Compare:

- current path: `Trade` ORM live desk rows
- candidate path: runtime objects loaded from physical `trading_management_envelopes`

Then feed both through the same enrichment path used by the AutoTrader desk live `trades` list.

## Must Preserve

- public response key `trades`
- public response key `paper_trades`
- row key `kind`
- row key `id`
- row key `related_alert_id`
- override lookup key shape `("trade", id)`
- `controls_supported`
- `close_supported`

## Hard Guardrails

- Do not convert the endpoint in this slice.
- Do not touch close/sell behavior.
- Do not touch override mutation behavior.
- Do not touch broker/order/reconcile/PDT/capital-gate behavior.
- Do not touch the paper-trade path except to include it unchanged in a parity payload if useful.
- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response fields.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Verification

Run:

```powershell
python -m py_compile scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py app\services\trading\autotrader_desk.py
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
python -m pytest tests\test_phase5ab_autotrader_desk_runtime_adapter_probe.py tests\test_autotrader_desk_api.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

Manual live probe may require an explicit opt-in flag, matching Phase 5AA.

## Exit Criteria

- Probe either emits `COMPLETE_POSITIVE` or documents exact field-level mismatches.
- No behavior changes.
- Analyzer raw reader bucket remains 0.

