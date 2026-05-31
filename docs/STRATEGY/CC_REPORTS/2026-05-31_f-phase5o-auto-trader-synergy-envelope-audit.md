# Phase 5O - AutoTrader Synergy Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/auto_trader_synergy.py`, the next Phase 5O
adapter candidate after the position-overrides audit.

Verdict: this is a live capital-control gate, not passive learning/reporting.
It selects open AutoTrader v1 management envelopes, checks whether a new alert
can scale into the existing position, excludes options, honors per-position
`synergy_excluded` overrides, tracks already-used confirming patterns, and
returns additional notional plus merged stop/target/average-entry values.

No live behavior was converted. This slice added read-only scale-in scope
evidence and reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5o-auto-trader-synergy-envelope-parity-probe.py`.
- Added `tests/test_phase5o_auto_trader_synergy_probe.py`.
- Reclassified `auto_trader_synergy.py` from
  `learning_research_reporting / adapter_candidate` to
  `risk_capital_gate / future_rename_blocker`.
- Updated the Phase 5O map and map-coverage test counts.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=8 AutoTrader synergy scale-in checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
SYNERGY_CHECKS=8
SYNERGY_MISMATCHES=0
scale_in_count_by_selected_id: 7 old = 7 new
selected_option_trade_ids: 0 old = 0 new
selected_spot_trade_ids: 7 old = 7 new
selected_trade_fingerprints: 7 old = 7 new
selected_trade_ids_by_pair: 7 old = 7 new
synergy_excluded_selected_trade_ids: 0 old = 0 new
used_scale_in_pattern_ids_by_selected_id: 7 old = 7 new
v1_pair_keys: 7 old = 7 new
```

## Verification

- `python -m py_compile scripts\d-phase5o-auto-trader-synergy-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `pytest tests\test_phase5o_auto_trader_synergy_probe.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `python scripts\d-phase5o-auto-trader-synergy-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
7 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting 13 -> 12
risk_capital_gate 18 -> 19
adapter_candidate 16 -> 15
future_rename_blocker 37 -> 38
Phase 5O synergy probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: this slice did not restart services, edit `.env`, mutate the DB,
or call broker/order APIs. Postgres was healthy before live read-only probes.
The source-posture probe reported services mounted from the clean Phase5AB-D
runtime worktree with `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false`.

## Architect Verdict

Do not convert this file through a generic adapter sweep. The read scope now
has parity evidence, but the module influences real capital by returning
scale-in plans. A future conversion needs runtime-object parity for the full
`maybe_scale_in(...)` contract, including option exclusion, same-pattern skip,
used confirming-pattern skip, learned max-scale-ins, override exclusion,
notional sizing, merged stops/targets, and average-entry recomputation.

