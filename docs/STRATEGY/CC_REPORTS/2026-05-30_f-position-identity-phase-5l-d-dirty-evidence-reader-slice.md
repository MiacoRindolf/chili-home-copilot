# Phase 5L-D: Dirty Evidence Reader Slice

**Date:** 2026-05-30
**Status:** SHIPPED
**Commit:** `1588a11`

## Summary

Reduced the remaining dirty evidence/model reader surface without absorbing the
unrelated local edits already present in those files.

Converted these reader SQL surfaces from the legacy compatibility view name to
the semantic management-envelope relation:

- `app/services/trading/pattern_regime_ledger.py`
- `app/services/trading/pattern_survival/features.py`

No broker/order/reconcile writer path changed. The `trading_trades`
compatibility view stays intentionally alive.

## Code Changes

- Imported `MANAGEMENT_ENVELOPES_RELATION` from
  `app.services.trading.management_envelopes`.
- Replaced direct `FROM trading_trades` reads in regime-ledger and survival
  feature queries with `FROM {MANAGEMENT_ENVELOPES_RELATION}`.
- Removed the converted lines from `tests/test_phase5l_reader_allowlist.py`.
- Used isolated blob-level staging so pre-existing data-science edits in those
  dirty files were not committed with this slice.

## Verification

Current worktree:

```text
python -m pytest tests\test_phase5l_reader_allowlist.py -q
1 passed

python -m py_compile app\services\trading\pattern_regime_ledger.py app\services\trading\pattern_survival\features.py tests\test_phase5l_reader_allowlist.py

python scripts\d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

python scripts\d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

Clean temporary checkout:

```text
python -m pytest tests\test_phase5l_reader_allowlist.py -q
1 passed

python -m py_compile app\services\trading\pattern_regime_ledger.py app\services\trading\pattern_survival\features.py tests\test_phase5l_reader_allowlist.py
```

Runtime:

- Recreated `chili`, `scheduler-worker`, and `brain-work-dispatcher`.
- All three came back running; `chili` and `brain-work-dispatcher` were healthy.
- Fresh logs showed no relation/schema/runtime errors for the touched paths.

## Architect Note

This closes the safe reader-reduction portion of Phase 5L. The remaining
`trading_trades` surfaces are no longer "rename the table in a query" work.
They are compatibility contracts, writer/order/broker/reconcile code, or
trade-id semantic readers whose meaning still depends on the management-envelope
identity layer.

The next move should be a semantic-contract phase, not another blind cleanup
pass.

## Next

Queue Phase 5L-E: define explicit trade-id semantic reader contracts for the
remaining live surfaces, starting with autotrader retry/probation/open-by-lane,
bracket watchdog, and Coinbase orphan adoption. These should move behind named
management-envelope helper APIs before any further compatibility-view retirement.
