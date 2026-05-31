# Phase 5X: Stop-Decision Read Helper Conversion

Date: 2026-05-30

## Summary

Converted `/api/trading/stops/decisions` from a direct `StopDecision` + `Trade` ORM join to a semantic management-envelope helper while preserving the public response contract.

This was intentionally limited to the stop-decision audit-history endpoint. Stop execution, active stop rendering, monitor-run, sell/close, broker/order/reconcile/PDT/capital gates, `/trades`, `trade_id`, schema names, and UI labels were not changed.

## What Changed

- Added `load_stop_decision_envelope_rows(...)` to `app/services/trading/management_envelopes.py`.
- Converted `app/routers/trading.py::api_stop_decisions(...)` to call the helper.
- Added `_stop_decision_rows(...)` to preserve the exact response fields:
  - `id`
  - `trade_id`
  - `as_of_ts`
  - `state`
  - `old_stop`
  - `new_stop`
  - `trigger`
  - `reason`
  - `executed`
- Added tests for the helper SQL and router response shape.

## Performance Finding

The all-trades stop-decision endpoint has a large backing table:

- `trading_stop_decisions`: about 98k rows
- `trading_management_envelopes`: about 725 rows
- Existing useful index: `(trade_id, as_of_ts DESC)`

The safe query shape is a scoped-envelope lateral lookup:

```sql
WITH scoped AS MATERIALIZED (
  SELECT id FROM trading_management_envelopes WHERE user_id IS NOT DISTINCT FROM :uid
),
per_trade AS (
  SELECT d.*
  FROM scoped s
  CROSS JOIN LATERAL (
    SELECT ...
    FROM trading_stop_decisions
    WHERE trade_id = s.id
    ORDER BY as_of_ts DESC, id DESC
    LIMIT :limit
  ) d
)
SELECT * FROM per_trade ORDER BY as_of_ts DESC, id DESC LIMIT :limit;
```

Important planner note: adding `NULLS LAST` to the stop-decision ordering defeated the existing index path and made the live read slow. The final helper preserves the old endpoint's plain `DESC` semantics and uses the index.

Live smoke after the fix:

- all-trades stop decisions, cold: 50 rows in about 468 ms
- all-trades stop decisions, warm: 50 rows in about 11 ms
- single-trade stop decisions: 10 rows in about 2 ms

## Verification

- `python -m py_compile app/services/trading/management_envelopes.py app/routers/trading.py`
- `pytest tests/test_management_envelopes.py tests/test_phase5x_stop_decision_read_conversion.py tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`
  - Result: 24 passed, 1 existing SQLAlchemy warning
- `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
  - `orm_trade_symbol_compat | 94`
  - raw reader bucket: 0
- Live helper smoke against `chili` succeeded with the timings above.

## Architect Verdict

Phase 5X is a good conversion. It removes another router read from direct `Trade` ORM coupling and improves the stop-decision audit endpoint's read path without touching any live stop behavior.

Next: audit the stop-position rendering surface before touching it. It is user-facing and closer to live risk display than the stop-decision audit log, so it should be a contract audit first, not a blind conversion.
