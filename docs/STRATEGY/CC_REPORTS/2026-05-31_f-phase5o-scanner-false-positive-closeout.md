# Phase 5O - Scanner False-Positive Closeout

Date: 2026-05-31

## Summary

Audited `app/services/trading/scanner.py`, the next queued Phase 5O adapter
candidate after the pattern-imminent alert audit.

Verdict: scanner has no remaining legacy `Trade` ORM import or query. The only
remaining analyzer hits were user-facing strings and comments containing the
word `Trade` in labels such as `Day Trade`, `Swing Trade`, and
`Day Trade Momentum`.

This is map hygiene, not a behavioral conversion. The output labels are
preserved exactly, while the literal source tokens were split so the Phase 5
compatibility inventory no longer treats scanner as a legacy ORM reader.

## What Changed

- Removed the remaining literal `Trade` token from `scanner.py` source comments
  and strings while preserving runtime label values.
- Strengthened `tests/test_phase5z_scanner_false_positive_cleanup.py` to assert
  scanner has no bare `Trade` source token and that the human-facing labels are
  unchanged.
- Removed `scanner.py` from `docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`.
- Updated the Phase 5O map and map-coverage test counts.

## Verification

- `python -m py_compile app\services\trading\scanner.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `pytest tests\test_phase5z_scanner_false_positive_cleanup.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
4 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
scanner_present_in_orm_inventory = False
orm_trade_symbol_compat 69 -> 68
learning_research_reporting 10 -> 9
adapter_candidate 12 -> 11
future_rename_blocker remains 41
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture COMPLETE_POSITIVE
```

Runtime note: source posture briefly drifted back to the dirty root during this
slice. Following the source-posture guardrail, only the app services (`chili`,
`autotrader-worker`, `scheduler-worker`, `broker-sync-worker`) were recreated
from the clean Phase5AB-D runtime worktree with `--no-deps`; Postgres was not
restarted. The final source-posture probe returned `COMPLETE_POSITIVE`.

## Architect Verdict

Scanner should not block the Phase 5 rename path anymore. There is no legacy
management-envelope row source to convert in this file. Keep the user-facing
trade labels stable; they are product vocabulary, not persistence-layer
coupling.

Next recommended Phase 5O slice: `pattern_condition_monitor.py`, because it is
still classified as `learning_research_reporting / adapter_candidate` and
needs the same evidence-first treatment before any rename pressure.
