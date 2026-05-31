# Phase 5AA-B - Market-Data Anchor Reader Conversion

Date: 2026-05-31

## Summary

Converted the database fallback inside
`market_data._resolve_implausibility_anchor(...)` from the legacy `Trade` ORM /
`trading_trades` compatibility view path to the semantic
`trading_management_envelopes` base-table path.

Cache-first behavior is unchanged:

1. per-process known-good quote cache
2. most-recent open management envelope entry price
3. `None`, which preserves today's accept-and-seed behavior

Failure behavior is unchanged. If the DB lookup fails, the function still
returns `None` and the boundary guard keeps the existing no-anchor behavior.
The explicit rollback/close pattern remains in place to avoid idle-in-
transaction leaks.

## Live Evidence

The Phase 5AA parity probe remained green after the conversion:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=8 market-data anchor checks matched
ANCHOR_TICKERS=8
ANCHOR_MISMATCHES=0
```

Matched tickers: `AAOX`, `ABT-USD`, `ALCX-USD`, `COOKIE-USD`, `QNT-USD`,
`SAFE-USD`, `SENT-USD`, `SUP-USD`.

## Verification

- `python -m py_compile app\services\trading\market_data.py app\services\trading\management_envelopes.py scripts\d-phase5aa-market-data-anchor-parity-probe.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `pytest tests\test_management_envelopes.py tests\test_market_data_implausible_guard.py tests\test_market_data_known_good_cache_performance.py tests\test_phase5aa_market_data_anchor_parity_probe.py tests\test_phase5_remaining_trade_refs.py -q`
- `PHASE5AA_ALLOW_LIVE_PROBE=true DATABASE_URL=... python scripts\d-phase5aa-market-data-anchor-parity-probe.py`

Result: 61 tests passed; live parity remained `COMPLETE_POSITIVE`.

## Counts

```text
orm_trade_symbol_compat     | 71
adapter_candidate           | 22
learning_research_reporting | 17
future_rename_blocker       | 33
leave_alone                 | 16
```

## Architect Verdict

Safe narrow conversion. This removes one live market-data reader from the
legacy compatibility ORM surface without changing quote thresholds, provider
routing, broker/order behavior, risk/capital gates, or public contracts.

