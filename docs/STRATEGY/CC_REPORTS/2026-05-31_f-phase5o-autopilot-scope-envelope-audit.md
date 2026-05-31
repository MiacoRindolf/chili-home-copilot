# Phase 5O - Autopilot Scope Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/autopilot_scope.py`, the next Phase 5O adapter
candidate after the AutoTrader synergy audit.

Verdict: this is a live ownership and entry-scope gate, not a harmless private
type helper. It classifies open management envelopes for the Autopilot desk and
monitor, detects option rows, counts AutoTrader v1 ownership for a symbol, and
feeds `check_autopilot_entry_gate(...)`, the mutual-exclusion guard between
AutoTrader v1 and momentum_neural.

No live behavior was converted. This slice added read-only scope/ownership
evidence and reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5o-autopilot-scope-envelope-parity-probe.py`.
- Added `tests/test_phase5o_autopilot_scope_probe.py`.
- Reclassified `autopilot_scope.py` from
  `private_helper_type_only / adapter_candidate` to
  `risk_capital_gate / future_rename_blocker`.
- Updated the Phase 5O map and map-coverage test counts.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 autopilot-scope checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
AUTOPILOT_SCOPE_CHECKS=6
AUTOPILOT_SCOPE_MISMATCHES=0
live_autopilot_trade_ids: 8 old = 8 new
option_trade_ids: 0 old = 0 new
scope_by_trade_id: 8 old = 8 new
scope_row_fingerprints: 8 old = 8 new
v1_open_counts_by_user_symbol: 7 old = 7 new
v1_owned_symbols: 7 old = 7 new
```

## Verification

- `python -m py_compile scripts\d-phase5o-autopilot-scope-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `pytest tests\test_phase5o_autopilot_scope_probe.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `python scripts\d-phase5o-autopilot-scope-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
7 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
private_helper_type_only 6 -> 5
risk_capital_gate 19 -> 20
adapter_candidate 15 -> 14
future_rename_blocker 38 -> 39
Phase 5O autopilot-scope probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: this slice did not restart services, edit `.env`, mutate the DB,
or call broker/order APIs. It is evidence-only under the current PM/control
plane freeze.

## Architect Verdict

Do not convert this module through a generic adapter sweep. The helper is small,
but it sits directly in the entry-control path. A future behavior-preserving
conversion needs runtime-object parity for `live_autopilot_trade_filter()`,
`is_option_trade(...)`, `classify_live_autopilot_trade_scope(...)`,
`_count_v1_open_trades(...)`, `find_symbol_owner(...)`, and
`check_autopilot_entry_gate(...)`.

