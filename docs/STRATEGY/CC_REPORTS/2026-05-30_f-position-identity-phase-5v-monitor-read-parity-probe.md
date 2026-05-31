# CC Report: f-position-identity-phase-5v-monitor-read-parity-probe

Date: 2026-05-30
Status: SHIPPED

## Summary

Added a read-only parity probe for the remaining monitor/router read candidates identified in Phase 5U.

The probe compares old compatibility-view reads (`trading_trades`) against semantic base-table reads (`trading_management_envelopes`) for:

- `api_monitor_decisions(...)` equivalent result rows across representative users/actions.
- `api_monitor_imminent_alerts(...)` actioned-alert exclusion behavior across representative users.

The probe does not change application behavior.

## Live Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=20 monitor read checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
PARITY_CHECKS=20
PARITY_MISMATCHES=0
STOP_DECISIONS_INCLUDED=False
```

Representative checks covered:

- users: `NULL`, `1`, `7`, `13`
- monitor-decision actions: `ALL`, `hold`, `exit_now`, `tighten_stop`
- imminent-alert exclusion sets for the same users

## Verification

- `python -m py_compile scripts/d-phase5v-monitor-read-parity-probe.py`
- `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test python -m pytest tests/test_phase5v_monitor_read_parity_probe.py tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`
  - Result: `14 passed, 1 warning`
- `python scripts/d-phase5v-monitor-read-parity-probe.py`
  - Result: `COMPLETE_POSITIVE`
- `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
  - Result: `orm_trade_symbol_compat | 94`
  - Raw reader bucket remains `(none) | 0`

## Notes

`api_stop_decisions(...)` remains optional in the probe behind `PHASE5V_INCLUDE_STOP_DECISIONS=1`. The old compatibility-view stop-decision join exceeded the read-only probe timeout in live data, so it is not part of the default green gate. That keeps Phase 5W focused on the two monitor-read surfaces that are both parity-proven and fast enough to use as a repeatable gate.

## Architect Verdict

Phase 5W can safely convert the two parity-proven read-only monitor surfaces:

- `api_monitor_decisions(...)`
- `api_monitor_imminent_alerts(...)`

Do not touch:

- `api_monitor_run(...)`
- `api_sell_trade(...)`
- stop execution
- public `/trades`, `trade_id`, schema class names, or UI labels
