# CC Report: Phase 5X Setup Vitals Envelope Tickers

Date: 2026-05-31
Branch: `codex/phase5x-learning-reporting-adapter-slice-9`

## Summary

Converted the setup-vitals ticker discovery reader from direct `Trade` ORM reads
to the semantic management-envelope surface.

This is a passive market-data refresh selector only. It decides which tickers
deserve setup-vitals refreshes by combining:

- open management-envelope tickers
- pending breakout-alert tickers

No broker/order/close/reconcile/risk/capital behavior changed.

## What Changed

- Added `load_open_setup_vitals_envelope_tickers(...)` in
  `app/services/trading/management_envelopes.py`.
- Updated `setup_vitals.monitored_tickers_for_vitals(...)` to use that helper
  for open-envelope ticker discovery.
- Left the pending `BreakoutAlert` half unchanged.
- Removed the `Trade` ORM import/read from `setup_vitals.py`.
- Updated the Phase 5 compatibility map and analyzer-count expectations.

## Verification

```text
python -m py_compile app/services/trading/management_envelopes.py app/services/trading/setup_vitals.py
python -m json.tool docs/STRATEGY/phase5o_remaining_runtime_compat_map.json
python scripts/analyze_phase5_remaining_trade_refs.py --json
TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_management_envelopes.py tests/test_setup_vitals.py tests/test_phase5_remaining_trade_refs.py -q
```

Result:

```text
41 passed
unexpected_runtime_readers = []
unexpected_runtime_mutations = []
unclassified = []
orm_trade_symbol_compat = 73
adapter_candidate = 24
learning_research_reporting = 19
```

## Risk Read

Low. This slice only changes a ticker list used for setup-vitals refresh work.
It does not place orders, close positions, alter pattern lifecycle, or gate
capital. The row semantics are intentionally narrower than the old ORM path:
`status = 'open'` tickers from `trading_management_envelopes`.

## Next Recommendation

Phase 5Y should inspect `regime_classifier.py`. It has a closed-trade
performance reader that looks like a plausible next read-only adapter candidate,
but it deserves a small parity test because regime features can feed downstream
learning behavior.
