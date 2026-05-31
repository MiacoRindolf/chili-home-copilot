# NEXT_TASK: f-position-identity-phase-5t-audit-export-envelope-helper-slice

STATUS: PENDING

## Goal

Move the read-only audit export trade-row source to a management-envelope helper while preserving the public export contract.

Phase 5S converted the private AI evidence reader and kept the public `trades` response key stable. The next safe router target is `api_audit_export(...)`, which is read-only and already framed as an export surface.

## Recommended Work Shape

1. Add a narrow helper to `app/services/trading/management_envelopes.py`:
   - likely `load_audit_export_envelope_rows(...)`
   - read from `trading_management_envelopes`
   - return exactly the fields currently emitted in the audit export `trades` section
2. Convert only `app/routers/trading_sub/trades.py::api_audit_export(...)` trade-row source.
3. Keep:
   - response section name `trades`
   - JSON field names
   - CSV section label `# TRADES`
   - CSV headers/order
4. Add parity tests for JSON and CSV export shape.
5. Re-run:
   - focused audit export tests
   - `tests/test_management_envelopes.py`
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response field names.
- Do not touch `api_sell_trade`, `/trades` CRUD, monitor active setup responses, broker sync, bracket writers, stop/exit execution, order placement, PDT, promotion, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Stop if CSV/JSON parity cannot be pinned cleanly.

## Architect Verdict

This is the last clearly safe router cleanup identified by Phase 5R. After this, the remaining router/schema surface likely needs either public compatibility aliases or live-path parity gates.
