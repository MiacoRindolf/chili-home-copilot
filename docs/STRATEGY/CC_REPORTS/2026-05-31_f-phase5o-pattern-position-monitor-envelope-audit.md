# Phase 5O - Pattern Position Monitor Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/pattern_position_monitor.py`, the next Phase 5O
adapter candidate after the Autopilot scope audit.

Verdict: this is a live monitor/action-adjacent path, not passive
learning/reporting. It selects open pattern-linked and plan-level management
envelopes, filters broker-stale rows, can reconcile stale Robinhood rows,
persists `PatternMonitorDecision` rows, can tighten stored stop/target levels,
and emits `pattern_monitor` exit/tighten alerts.

No monitor behavior was converted. This slice added read-only selection-scope
evidence and reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5o-pattern-position-monitor-envelope-parity-probe.py`.
- Added `tests/test_phase5o_pattern_position_monitor_probe.py`.
- Reclassified `pattern_position_monitor.py` from
  `learning_research_reporting / adapter_candidate` to
  `live_action_broker_reconcile / future_rename_blocker`.
- Updated the Phase 5O map and map-coverage test counts.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 pattern-monitor selection checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
PATTERN_MONITOR_CHECKS=6
PATTERN_MONITOR_MISMATCHES=0
lane_by_trade_id: 8 old = 8 new
monitor_row_fingerprints: 8 old = 8 new
option_trade_ids: 0 old = 0 new
pattern_linked_trade_ids: 8 old = 8 new
plan_level_trade_ids: 0 old = 0 new
selected_trade_ids: 8 old = 8 new
```

## Verification

- `python -m py_compile scripts\d-phase5o-pattern-position-monitor-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `pytest tests\test_phase5o_pattern_position_monitor_probe.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `python scripts\d-phase5o-pattern-position-monitor-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
6 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting 12 -> 11
live_action_broker_reconcile 18 -> 19
adapter_candidate 14 -> 13
future_rename_blocker 39 -> 40
Phase 5O pattern-monitor probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: the first source-posture check during this slice reported live
services had drifted back onto the dirty root. Following the source-posture
guardrail, only the app services (`chili`, `autotrader-worker`,
`scheduler-worker`, `broker-sync-worker`) were recreated from the clean
Phase5AB-D runtime worktree with `--no-deps`; Postgres was not restarted.
Post-correction source posture returned `COMPLETE_POSITIVE` and Phase 5K/5I
remained positive.

## Architect Verdict

Do not convert this module through a generic adapter sweep. The selection scope
has parity evidence, but the runtime path can persist decisions, mutate stop
and target levels, reconcile stale broker rows, and emit exit alerts. A future
conversion needs runtime-object parity for `run_pattern_position_monitor(...)`,
`run_pattern_position_monitor_for_trades(...)`, broker-truth filtering,
plan-level evaluation, pattern-linked evaluation, alert deduplication, and
stop/target mutation semantics.

