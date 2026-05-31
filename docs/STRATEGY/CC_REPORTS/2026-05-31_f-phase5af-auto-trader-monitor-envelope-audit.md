# Phase 5AF - AutoTrader Monitor Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/auto_trader_monitor.py`, the next
ORM-symbol candidate after Phase 5AE.

Verdict: this is a live exit monitor, not a passive monitor/report. It selects
open management envelopes, partitions option and crypto rows away from the
equity exit lane, seeds missing stop/target levels, submits Robinhood exits,
and can trip loss kill switches.

No live behavior was converted. This slice added read-only scope evidence and
reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5af-auto-trader-monitor-scope-parity-probe.py`.
- Added `tests/test_phase5af_auto_trader_monitor_scope_probe.py`.
- Reclassified `auto_trader_monitor.py` from
  `learning_research_reporting / adapter_candidate` to
  `live_action_broker_reconcile / future_rename_blocker`.

## Live Probe Result

With `PHASE5AF_USER_ID=1`:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 AutoTrader monitor scope checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
USER_ID=1
AUTOTRADER_MONITOR_CHECKS=6
AUTOTRADER_MONITOR_MISMATCHES=0
selected_ids: 8 old = 8 new
crypto_ids: 7 old = 7 new
equity_monitor_ids: 1 old = 1 new
option_ids: 0 old = 0 new
scope_counts: 1 old = 1 new
```

## Verification

- `python -m py_compile scripts\d-phase5af-auto-trader-monitor-scope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `pytest tests\test_phase5af_auto_trader_monitor_scope_probe.py tests\test_phase5_remaining_trade_refs.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
- `python scripts\d-phase5af-auto-trader-monitor-scope-parity-probe.py` with live opt-in and `PHASE5AF_USER_ID=1`
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`

Results:

```text
13 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting 14 -> 13
live_action_broker_reconcile 16 -> 17
adapter_candidate 18 -> 17
future_rename_blocker 35 -> 36
Phase 5AF monitor scope probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
```

## Architect Verdict

Do not convert this file through a generic adapter sweep. The selection scope
has parity evidence now, but the runtime object contract is live-money exit
behavior. A future conversion should first prove full runtime-object parity
through broker-stale filtering, option/crypto delegation, level seeding, quote
evaluation, pending-exit state, and kill-switch side effects.
