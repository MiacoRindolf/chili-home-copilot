# NEXT_TASK: f-phase5ab-d-pattern-monitor-handoff-conversion

STATUS: QUEUED

## Goal

Convert only `trading_scheduler.trigger_pattern_monitor_for_tickers(...)` from
loading SQLAlchemy `Trade` ORM rows to loading envelope-shaped runtime objects
from `trading_management_envelopes`.

## Evidence

Phase 5AB-C live probe:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
RUNTIME_OBJECTS_OLD=8
RUNTIME_OBJECTS_NEW=8
FIELD_MISMATCHES=0
BROKER_TRUTH_MATCH=True
```

The downstream pattern monitor's observed runtime-object fields and
broker-stale projection matched between current `Trade` objects and candidate
management-envelope objects.

## Scope

- Add or reuse a management-envelope helper that loads open pattern/plan
  runtime objects for a ticker set.
- Convert only the loader inside
  `trigger_pattern_monitor_for_tickers(...)`.
- Keep `run_pattern_position_monitor_for_trades(...)` and
  `pattern_position_monitor.py` behavior unchanged.
- Keep the public function name and call sites unchanged.

## Guardrails

- No scheduler cadence changes.
- No stop evaluation or dispatch rule changes.
- No broker/order/close/reconcile behavior changes.
- No risk/capital/PDT/portfolio gate changes.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- If the live Phase 5AB-C probe regresses, stop and do not convert.

## Exit Criteria

- Focused tests pin that the scheduler handoff loader reads
  `trading_management_envelopes`.
- Phase 5AB-C runtime-object probe remains `COMPLETE_POSITIVE`.
- Phase 5AB scheduler scope probe remains `COMPLETE_POSITIVE`.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
