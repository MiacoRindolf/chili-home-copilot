# Phase 5AG - AutoTrader Position Overrides Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/auto_trader_position_overrides.py`, the next
Phase 5O adapter candidate after Phase 5AF.

Verdict: this is a live control helper, not passive private/type-only code.
It can submit broker exits through `close_position_now(...)`, mutate live
management scope through adoption/unadoption, and feed live monitor/synergy
behavior through per-position overrides.

No live behavior was converted. This slice added read-only control-scope
evidence and reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5ag-position-overrides-envelope-parity-probe.py`.
- Added `tests/test_phase5ag_position_overrides_probe.py`.
- Reclassified `auto_trader_position_overrides.py` from
  `private_helper_type_only / adapter_candidate` to
  `live_action_broker_reconcile / future_rename_blocker`.
- Updated the Phase 5O map and map-coverage test counts to the current
  69-file ORM compatibility inventory.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=10 position-override control-scope checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
POSITION_OVERRIDE_CHECKS=10
POSITION_OVERRIDE_MISMATCHES=0
adopt_candidate_ids: 1 old = 1 new
close_candidate_ids: 8 old = 8 new
close_good_qty_candidate_ids: 8 old = 8 new
close_option_candidate_ids: 0 old = 0 new
close_spot_candidate_ids: 8 old = 8 new
control_row_fingerprints: 8 old = 8 new
monitor_paused_trade_ids: 0 old = 0 new
override_linked_trade_ids: 0 old = 0 new
synergy_excluded_trade_ids: 0 old = 0 new
unadopt_candidate_ids: 7 old = 7 new
```

## Verification

- `python -m py_compile scripts\d-phase5ag-position-overrides-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -c "import json; json.load(open('docs/STRATEGY/phase5o_remaining_runtime_compat_map.json', encoding='utf-8'))"`
- `pytest tests\test_phase5ag_position_overrides_probe.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --json`
- `python scripts\d-phase5ag-position-overrides-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
6 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
live_action_broker_reconcile 17 -> 18
private_helper_type_only 7 -> 6
adapter_candidate 17 -> 16
future_rename_blocker 36 -> 37
Phase 5AG position-overrides probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: the first posture check found the intended autotrader worker
dead. A duplicate container accidentally created under a worktree-derived
Compose project name was removed, then only the intended
`chili-home-copilot-autotrader-worker-1` was recreated with
`CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false`. Post-restart logs showed the
autotrader scheduler role started cleanly, and source posture returned
`COMPLETE_POSITIVE`.

## Architect Verdict

Do not convert this file through a generic adapter sweep. The read/control
scope has parity evidence now, but the helper still owns live close/adopt/
unadopt behavior. A future conversion needs runtime-object parity for close-now
broker submission, options close path, adoption stop/target seeding,
unadoption scope inference, override cleanup, and audit row writing before any
behavioral cut.
