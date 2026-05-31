# Phase 5AD - Alerts Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/alerts.py`, which the Phase 5O map had
classified as a read-only learning/reporting candidate.

Verdict: `alerts.py` is **not** a passive reporting file. Its legacy `Trade`
ORM surface is live behavior:

- `_check_open_positions(...)` is the legacy price-monitor fallback. It reads
  open management envelopes, may reconcile stale broker positions, and dispatches
  stop/target alerts if the newer stop engine fails.
- `_execute_proposal(...)` is a live order/proposal surface. It creates
  management envelopes, writes execution events, links decision packets, and may
  place broker orders.
- `_open_trades_per_sector(...)` feeds a proposal concentration gate by counting
  open long envelopes by sector.

No live behavior was converted. Instead this task corrected the compatibility
map and added read-only parity evidence for the read surfaces that could be
converted later.

## What Changed

- Added `scripts/d-phase5ad-alerts-envelope-parity-probe.py`.
- Added `tests/test_phase5ad_alerts_envelope_parity_probe.py`.
- Moved `alerts.py` in the Phase 5O compatibility map from
  `learning_research_reporting / adapter_candidate` to
  `live_action_broker_reconcile / future_rename_blocker`.
- Updated the Phase 5 analyzer contract grouping so future audits do not treat
  `alerts.py` as a passive reader.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=3 alerts.py read-scope checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
ALERTS_SCOPE_CHECKS=3
ALERTS_SCOPE_MISMATCHES=0
legacy_fallback_open_position_ids: 8 old = 8 new
legacy_fallback_open_position_rows: 8 old = 8 new
sector_cap_counts: 1 old = 1 new
```

## Verification

- `python -m py_compile scripts\d-phase5ad-alerts-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `pytest tests\test_phase5ad_alerts_envelope_parity_probe.py tests\test_phase5_remaining_trade_refs.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --json`
- `python scripts\d-phase5ad-alerts-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
13 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting 15 -> 14
live_action_broker_reconcile 15 -> 16
adapter_candidate 20 -> 19
future_rename_blocker 33 -> 34
Phase 5AD alerts probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
Source posture COMPLETE_POSITIVE
```

## Architect Verdict

Do not convert `alerts.py` directly in the current slice. The read scopes now
have parity evidence, but the file also creates envelopes and places orders.
Any future conversion should split the file by contract: sector-cap read helper
first, legacy fallback object adapter second, proposal execution writer last
and only after a dedicated writer/runtime-object probe.
