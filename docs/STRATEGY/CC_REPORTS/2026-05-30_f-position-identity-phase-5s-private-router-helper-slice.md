# CC Report: f-position-identity-phase-5s-private-router-helper-slice

Date: 2026-05-30
Branch: codex/brain-work-done-marker-recovery

## Summary

Phase 5S is shipped as the first private router helper conversion after the Phase 5R contract audit.

`ai.py::_api_pattern_evidence_response(...)` no longer reads the legacy `Trade` ORM class for pattern-tag evidence. It now reads recent pattern-tagged management envelopes through a semantic helper while preserving the public response key `trades` and the existing row shape.

No public API field names changed. No broker sync, order placement, close, stop, reconcile, PDT, promotion, or capital-gate path changed.

## What Changed

Added helper contract:

- `load_pattern_tagged_envelope_rows(...)`

Converted private router internals:

- `app/routers/trading_sub/ai.py::_api_pattern_evidence_response(...)`
  - removed direct `Trade` import/query
  - keeps keyword matching in Python, as before
  - keeps `trades_out` and response key `trades` for frontend/API compatibility

## Audit Result

After Phase 5R:

```text
orm_trade_symbol_compat | 95
```

After Phase 5S:

```text
orm_trade_symbol_compat | 94
raw reader bucket       | none
unexpected readers      | 0
unexpected mutations    | 0
unclassified            | 0
```

File removed from the ORM-symbol bucket in this slice:

- `app/routers/trading_sub/ai.py`

## Architect Verdict

This is the right boundary: private router internals can move to semantic envelope helpers, while public API vocabulary remains stable.

The next safe target is the read-only audit export in `trades.py`. It can likely read management envelopes while still returning the public `trades` section, but it needs JSON/CSV parity tests before changing.

## Verification

```text
py_compile:
app/services/trading/management_envelopes.py
app/routers/trading_sub/ai.py

pytest:
tests/test_management_envelopes.py
tests/test_phase5s_private_router_helper.py
tests/test_phase5_remaining_trade_refs.py
tests/test_phase5l_reader_allowlist.py

Result: 19 passed
```

Analyzer:

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime

orm_trade_symbol_compat | 94
raw reader bucket       | none
```

## Next Task

`f-position-identity-phase-5t-audit-export-envelope-helper-slice`

Recommended scope:

1. Add a helper for audit/export management-envelope rows.
2. Convert only `api_audit_export(...)` read-source internals.
3. Keep response section name `trades`, CSV headers, and JSON field names stable.
4. Add JSON/CSV parity tests before claiming completion.
5. Do not touch `api_sell_trade`, `/trades` CRUD, monitor active setup responses, or schemas.
