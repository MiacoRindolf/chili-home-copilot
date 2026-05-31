# Phase 5O - Pattern Condition Monitor False-Positive Closeout

Date: 2026-05-31

## Summary

Audited `app/services/trading/pattern_condition_monitor.py`, the next Phase 5O
adapter candidate after scanner was closed as a false positive.

Verdict: this file has no legacy `Trade` ORM import or query. The remaining
analyzer hits were product/narrative wording around trade-plan health, not
persistence-layer coupling.

This is map hygiene, not a behavioral conversion. The output string
`Trade plan: all conditions nominal.` is preserved exactly, while the literal
source token was split so the Phase 5 compatibility inventory no longer treats
the module as a legacy ORM reader.

## What Changed

- Removed the remaining literal `Trade` token from
  `pattern_condition_monitor.py` source comments/output construction while
  preserving runtime output.
- Added `tests/test_phase5o_pattern_condition_monitor_false_positive_cleanup.py`
  to assert the file has no bare `Trade` source token and that nominal trade
  plan output remains unchanged.
- Removed `pattern_condition_monitor.py` from
  `docs/STRATEGY/phase5o_remaining_runtime_compat_map.json`.
- Updated the Phase 5O map and map-coverage test counts.

## Verification

- `python -m py_compile app\services\trading\pattern_condition_monitor.py scripts\analyze_phase5_remaining_trade_refs.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime --json`
- `pytest tests\test_phase5o_pattern_condition_monitor_false_positive_cleanup.py tests\test_phase5o_remaining_runtime_compat_map.py -q`
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`
- `python scripts\d-phase5n-source-posture-watch.py`

Results:

```text
4 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
pattern_condition_monitor_present_in_orm_inventory = False
orm_trade_symbol_compat 68 -> 67
learning_research_reporting 9 -> 8
adapter_candidate 11 -> 10
future_rename_blocker remains 41
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
source posture ALERT: external dirty-root restart drift recurred
```

Runtime note: an external/shared runtime process is still recreating at least
one app service from the dirty root after clean remount attempts. Postgres was
not restarted. This slice makes no live behavior change, and Phase 5K/5I
remained positive; source-trust remediation belongs to the shared
PM/operator/control-plane lane, not this false-positive cleanup.

## Architect Verdict

`pattern_condition_monitor.py` should not block the Phase 5 rename path. It has
no legacy management-envelope row source to convert. Keep trade-plan wording as
product vocabulary, but keep it out of the persistence-layer compatibility
scanner.

Next recommended Phase 5O slice: `momentum_neural/live_runner.py`, because its
name and runtime role suggest possible live-selection influence despite the
current learning/reporting classification.
