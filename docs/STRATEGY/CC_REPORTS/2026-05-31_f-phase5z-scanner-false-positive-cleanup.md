# Phase 5Z - Scanner False-Positive Cleanup

Date: 2026-05-31

## Summary

Phase 5Z audited the next learning/reporting candidates after Phase 5Y:

- `app/services/trading/scanner.py`
- `app/services/trading/market_data.py`
- `app/services/trading_scheduler.py`

Only one safe change was made: `scanner.py` imported `Trade as _Trade` inside
`evolve_strategy_weights(...)`, but never used `_Trade`. The import was removed
and pinned with a small regression test.

This is a false-positive cleanup, not a behavioral conversion. No scanner
selection, broker/order/close/reconcile, capital/risk/PDT, lifecycle, or public
contract behavior changed.

## Architect Verdict

Ship the scanner cleanup and defer the other two candidates.

`market_data._resolve_implausibility_anchor(...)` reads the most recent open
trade entry price as a quote-plausibility anchor. That path can affect
`fetch_quote(...)` callers, so it is live market-data behavior, not passive
reporting. It needs an old-vs-new parity probe before any reader swap.

`trading_scheduler.py` still owns live monitor scheduling around price/stop and
broker-position work. That surface is too close to live runtime behavior for a
small cleanup slice.

## Verification

- `python -m py_compile app\services\trading\scanner.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `python scripts\analyze_phase5_remaining_trade_refs.py --json`
- `pytest tests\test_phase5z_scanner_false_positive_cleanup.py tests\test_phase5_remaining_trade_refs.py -q`

Result: 11 tests passed. The analyzer stayed green with no unexpected runtime
readers or mutations.

## Counts

The compatibility counts intentionally remain unchanged:

```text
orm_trade_symbol_compat     | 72
adapter_candidate           | 23
learning_research_reporting | 18
future_rename_blocker       | 33
leave_alone                 | 16
```

`scanner.py` remains listed by the broad analyzer because the file still
contains user-facing/public vocabulary around trades. The removed `_Trade`
symbol was the actual unused import in this slice.

## Next

Queue a read-only parity probe for the market-data quote-plausibility anchor
before any conversion:

`f-phase5aa-market-data-anchor-parity-probe`

