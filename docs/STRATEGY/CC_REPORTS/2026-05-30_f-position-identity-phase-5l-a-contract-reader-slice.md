# Phase 5L-A: Contract Reader Slice

**Date:** 2026-05-30
**Status:** SHIPPED
**Commit:** `75e234c`

## Summary

Phase 5L starts the contract-hardening work after Phase 5K closed. This slice
moved two low-risk readers from the legacy compatibility name to the semantic
management-envelope relation:

- `attribution_service._closed_pattern_live_stats`
- `cost_aware_gate._coinbase_tca_backing_usable_samples`

It also introduced shared relation constants in `management_envelopes.py` so
future readers do not hand-write the physical relation name.

No broker/order/reconcile writer path changed.

## Code Changes

- Added:
  - `MANAGEMENT_ENVELOPES_RELATION = "trading_management_envelopes"`
  - `LEGACY_TRADES_COMPAT_RELATION = "trading_trades"`
- Converted attribution closed-pattern live stats to read
  `MANAGEMENT_ENVELOPES_RELATION`.
- Converted Coinbase TCA usable-sample counts to read
  `MANAGEMENT_ENVELOPES_RELATION`.
- Kept the Coinbase cap flag relation helper intact; its envelope side now
  references the shared constant.
- Added two source canaries so these readers do not drift back to
  `FROM trading_trades`.

## Verification

```text
python -m py_compile app\services\trading\management_envelopes.py app\services\trading\attribution_service.py app\services\trading\cost_aware_gate.py

python -m pytest \
  tests\test_attribution_service_performance.py::test_closed_pattern_live_stats_reads_management_envelopes_contract \
  tests\test_cost_aware_gate.py::test_tca_backing_sample_reader_uses_management_envelopes_contract \
  -q

2 passed
```

The broader cost-aware test file was not used for the final gate because the
shared test DB fixture hit the same pre-body slowdown/deadlock pattern already
noted in earlier Phase 5K work. The new canaries passed directly.

Live probes after the change:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
```

Live smoke:

```text
live_vs_research_by_pattern(user_id=1, days=90, limit=5)
  -> ok=True, patterns=5

_coinbase_tca_backing_usable_samples("AAVE-USD", "long", 30)
  -> 3
```

Runtime:

- Recreated `chili` and `autotrader-worker`.
- Both services came back up.
- Fresh logs showed no relation/query/schema errors for these readers.

## Architect Note

This is deliberately boring. The old and new relations are already parity-clean,
but naming matters now: readers should say "management envelopes" when that is
what they mean. This slice removes two more stale mental-model anchors without
touching live money state transitions.

The remaining writer/order/broker/reconcile paths are not next. The next useful
slice is an allow-list canary that prevents new raw live-reader SQL against
`trading_trades` while preserving the compatibility view for legacy contracts.
