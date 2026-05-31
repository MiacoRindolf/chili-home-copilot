# Phase 5AB - Trading Scheduler Contract Audit + Scope Probe

Date: 2026-05-31

## Summary

Audited the remaining `Trade` ORM references in
`app/services/trading_scheduler.py`.

Verdict: the references are live scheduler-selection surfaces, not passive
reporting. They decide which users, tickers, and trade rows flow into:

- `price_monitor`
- `broker_position_price_monitor`
- `daytrade_fast_monitor`
- `crypto_stop_monitor`
- `pattern_position_monitor`
- event-driven pattern-position monitor triggers

Because those selections can affect stop/target evaluation and monitor
dispatch, this task did not mechanically convert the scheduler.

Instead, it added a read-only old-vs-new parity probe:

`scripts/d-phase5ab-trading-scheduler-scope-parity-probe.py`

## Live Result

Manual live run:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=9 scheduler scope checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
SCHEDULER_SCOPE_CHECKS=9
SCHEDULER_SCOPE_MISMATCHES=0
```

Matched scopes:

- `price_monitor_user_ids`
- `price_monitor_pattern_tickers`
- `broker_position_user_ids`
- `broker_position_pattern_tickers`
- `daytrade_fast_user_ids`
- `crypto_stop_user_ids`
- `crypto_stop_counts_by_user`
- `pattern_position_user_ids`
- `pattern_trigger_trade_ids`

## Verification

- `python -m py_compile scripts\d-phase5ab-trading-scheduler-scope-parity-probe.py`
- `pytest tests\test_phase5ab_trading_scheduler_scope_probe.py -q`
- Live probe with `PHASE5AB_ALLOW_LIVE_PROBE=true` and live `DATABASE_URL`

Result: 6 tests passed and the live probe emitted `COMPLETE_POSITIVE`.

## Architect Verdict

Good to proceed to a narrow scheduler-scope helper conversion in the next
slice. Keep the conversion scoped to selection queries only. Do not change
job cadence, stop evaluation, dispatch behavior, broker/reconcile behavior, or
pattern monitor semantics.

