# Phase 5AB-B - Trading Scheduler Scope Conversion

Date: 2026-05-31

## Summary

Converted the Phase 5AB-proven scheduler scope-discovery reads in
`app/services/trading_scheduler.py` from direct `Trade` ORM queries to
management-envelope helpers.

This changes only the selection queries that decide which users and tickers
the existing scheduler jobs should consider. It does not change scheduler
cadence, stop evaluation, stop dispatch, broker/reconcile behavior, risk,
capital, PDT, portfolio gates, public `/trades` vocabulary, or any close/order
path.

## Converted

- price-monitor user ids
- price-monitor pattern tickers
- broker-backed monitor user ids
- broker-backed pattern tickers
- daytrade/scalp fast-monitor user ids
- crypto stop-monitor user ids
- crypto stop-monitor per-user existence check
- pattern-position monitor user ids

The new helpers live in:

`app/services/trading/management_envelopes.py`

## Intentionally Not Converted

`trigger_pattern_monitor_for_tickers(...)` still loads `Trade` ORM objects and
passes them to `run_pattern_position_monitor_for_trades(...)`.

Reason: that downstream API is an object-shaped live monitor contract. Phase
5AB proved the selected ids match, but did not prove that runtime
management-envelope objects can replace SQLAlchemy `Trade` identity semantics
inside `pattern_position_monitor`. That needs a separate runtime-object parity
probe.

## Verification

- `python -m py_compile app\services\trading\management_envelopes.py app\services\trading_scheduler.py`
- `pytest tests\test_phase5ab_b_trading_scheduler_scope_conversion.py tests\test_management_envelopes.py tests\test_phase5ab_trading_scheduler_scope_probe.py tests\test_phase5_remaining_trade_refs.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
- `PHASE5AB_ALLOW_LIVE_PROBE=true python scripts\d-phase5ab-trading-scheduler-scope-parity-probe.py`
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`

Results:

```text
50 passed, 1 warning
Phase 5AB: COMPLETE_POSITIVE, 9 scheduler scope checks matched, 0 mismatches
Phase 5K: COMPLETE_POSITIVE, 6 live-path aggregate checks matched
Phase 5I: COMPLETE_POSITIVE, fresh post-rename data clean
Analyzer: orm_trade_symbol_compat=71, raw reader bucket 0
```

The file-level analyzer count remains 71 because
`trading_scheduler.py` still intentionally contains the event-driven
`Trade` object handoff described above.

## Architect Verdict

Safe to ship. This is a runtime reader conversion, but only for proven
selection scopes. The next safe slice is a read-only runtime-object probe for
the event-driven pattern monitor handoff before considering any conversion of
`trigger_pattern_monitor_for_tickers(...)`.
