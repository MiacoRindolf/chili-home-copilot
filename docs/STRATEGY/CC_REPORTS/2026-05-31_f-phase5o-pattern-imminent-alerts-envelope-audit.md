# Phase 5O - Pattern Imminent Alerts Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/pattern_imminent_alerts.py`, the next Phase 5O
adapter candidate after the pattern-position monitor audit.

Verdict: this is a live selection gate, not passive learning/reporting. The
module reads open AutoTrader v1 positions through `_open_autotrader_position_keys(...)`
and uses those `(scan_pattern_id, ticker)` keys to deflect same-pattern/same-ticker
imminent candidates before alert generation. That can suppress future AutoTrader
inputs.

No alert behavior was converted. This slice added read-only deflection-scope
evidence and reclassified the file as a future rename blocker.

## What Changed

- Added `scripts/d-phase5o-pattern-imminent-alerts-envelope-parity-probe.py`.
- Added `tests/test_phase5o_pattern_imminent_alerts_probe.py`.
- Reclassified `pattern_imminent_alerts.py` from
  `learning_research_reporting / adapter_candidate` to
  `risk_capital_gate / future_rename_blocker`.
- Updated the Phase 5O map and map-coverage test counts.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=5 pattern-imminent deflection checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
PATTERN_IMMINENT_CHECKS=5
PATTERN_IMMINENT_MISMATCHES=0
deflection_row_fingerprints: 7 old = 7 new
deflection_trade_ids: 7 old = 7 new
keys_by_user: 1 old = 1 new
pattern_ticker_keys: 7 old = 7 new
user_pattern_ticker_keys: 7 old = 7 new
```

## Verification

- `python -m py_compile scripts\d-phase5o-pattern-imminent-alerts-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `pytest tests\test_phase5o_pattern_imminent_alerts_probe.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `python scripts\d-phase5o-pattern-imminent-alerts-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
6 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting 11 -> 10
risk_capital_gate 20 -> 21
adapter_candidate 13 -> 12
future_rename_blocker 40 -> 41
Phase 5O pattern-imminent probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: the first source-posture check during this slice reported
`autotrader-worker` and `scheduler-worker` had drifted back onto the dirty root.
Following the source-posture guardrail, only the app services (`chili`,
`autotrader-worker`, `scheduler-worker`, `broker-sync-worker`) were recreated
from the clean Phase5AB-D runtime worktree with `--no-deps`; Postgres was not
restarted. Post-correction source posture returned `COMPLETE_POSITIVE`, all app
services were running from the clean worktree, and Phase 5K/5I remained positive.

## Architect Verdict

Do not convert this module through a generic adapter sweep. The deflection scope
has parity evidence, but it is a candidate-generation gate. A future conversion
needs explicit behavior parity for `_open_autotrader_position_keys(...)` inside
`gather_imminent_candidate_rows(...)`, including user scoping, open/working
status semantics, AutoTrader v1 ownership, suppressed diagnostics, and the
`open_position_deflected` skip counter.

Next recommended Phase 5O slice: `scanner.py`, because scanner references are
still classified as learning/reporting, but scanner output can feed candidate
generation and deserves the same evidence-first treatment.
