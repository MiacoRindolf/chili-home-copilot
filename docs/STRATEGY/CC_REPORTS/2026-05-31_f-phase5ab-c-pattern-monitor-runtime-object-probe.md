# Phase 5AB-C - Pattern Monitor Runtime-Object Probe

Date: 2026-05-31

## Summary

Added a read-only parity probe for the remaining scheduler `Trade` ORM object
handoff:

`trading_scheduler.trigger_pattern_monitor_for_tickers(...) -> run_pattern_position_monitor_for_trades(...)`

Phase 5AB-B converted the scheduler's user/ticker/count selection reads, but
left this handoff alone because it passes actual runtime objects into live
pattern-position monitor logic. This slice proves whether candidate
management-envelope runtime objects are behaviorally equivalent for the fields
and broker-stale filtering that the downstream monitor observes.

Probe:

`scripts/d-phase5ab-c-pattern-monitor-runtime-object-probe.py`

## What It Checks

- Old ticker scope from `trading_trades` compatibility view vs new ticker scope
  from `trading_management_envelopes`.
- Old `Trade` ORM object ids vs new envelope runtime object ids.
- Field parity for the object-visible fields used by pattern monitoring,
  quote/P&L helpers, option detection, and broker truth:
  `id`, `ticker`, `user_id`, `status`, `related_alert_id`,
  `scan_pattern_id`, `entry_price`, `direction`, `broker_source`,
  `stop_loss`, `take_profit`, `asset_kind`, `tags`, `indicator_snapshot`,
  `auto_trader_version`, `trade_type`, `position_id`, fill/entry timestamps,
  quantities, and sync metadata.
- Broker-stale projection parity through
  `filter_broker_stale_open_trades(...)`.

The probe does not execute the monitor, emit alerts, place orders, close
positions, or write monitor decisions.

## Live Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=8 pattern-monitor runtime objects matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
OLD_TICKERS=8
NEW_TICKERS=8
RUNTIME_OBJECTS_OLD=8
RUNTIME_OBJECTS_NEW=8
FIELD_MISMATCHES=0
BROKER_TRUTH_MATCH=True
```

## Verification

- `python -m py_compile scripts\d-phase5ab-c-pattern-monitor-runtime-object-probe.py`
- `pytest tests\test_phase5ab_c_pattern_monitor_runtime_object_probe.py tests\test_phase5ab_trading_scheduler_scope_probe.py tests\test_phase5_remaining_trade_refs.py -q`
- Live probe with `PHASE5AB_C_ALLOW_LIVE_PROBE=true`

Result: `22 passed`, live probe `COMPLETE_POSITIVE`.

## Architect Verdict

Good to queue a narrow conversion of
`trigger_pattern_monitor_for_tickers(...)` to load envelope runtime objects.
Keep the change scoped to the handoff loader only. Do not change
`pattern_position_monitor`, monitor cadence, alert rules, stop evaluation,
broker/reconcile behavior, close/order behavior, risk/capital/PDT gates, or
public `/trades` vocabulary.
