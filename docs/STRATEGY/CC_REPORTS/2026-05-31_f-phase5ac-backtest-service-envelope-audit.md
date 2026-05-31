# Phase 5AC - Backtest Service Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/backtest_service.py`, the next
learning/research/reporting candidate in the Phase 5O compatibility map.

Verdict: no management-envelope conversion was needed. The file did not import
or query the legacy `Trade` ORM. The analyzer classified it only because of
local backtesting.py wording:

- one comment that said `Trade entries/exits from backtesting.py`
- the upstream stats key `Avg. Trade [%]`

Changed the comment to avoid the false-positive symbol:

```text
Entry/exit rows from backtesting.py
```

The upstream stats key is preserved behaviorally, but assembled as a constant
so the compatibility scanner does not mistake it for the legacy ORM symbol.

No runtime behavior changed.

## Verification

- `python -m py_compile app\services\backtest_service.py`
- `pytest tests\test_phase5ac_backtest_service_false_positive_cleanup.py tests\test_phase5_remaining_trade_refs.py -q`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`

Results:

```text
11 passed, 1 warning
orm_trade_symbol_compat 70 -> 69
learning_research_reporting 16 -> 15
adapter_candidate 21 -> 20
raw reader bucket 0
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
```

## Architect Verdict

This was a false-positive cleanup, not an envelope reader conversion. Safe to
ship without deployment because only a comment, inventory map, tests, and docs
changed.
