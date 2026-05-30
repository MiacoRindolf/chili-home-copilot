# CC Report: f-position-identity-phase-5m-orm-symbol-contract-audit

Date: 2026-05-30
Branch: codex/brain-work-done-marker-recovery

## Summary

Phase 5M is complete. The remaining runtime `Trade` ORM-symbol surface is now measurable as a contract, not a rename blocker.

The physical rename remains healthy:

- `trading_management_envelopes` is the base table.
- `trading_trades` remains the compatibility view.
- Runtime raw readers/mutations against `trading_trades` remain at zero unexpected files.
- The only unresolved ambiguity is semantic: many runtime files still import or type against the legacy `Trade` ORM class.

I did not rename the ORM class and did not touch broker/order/close behavior.

## What Changed

- Extended `scripts/analyze_phase5_remaining_trade_refs.py` with `--bucket`.
- Added `filter_inventory_by_bucket()` so focused audits can print only one classifier bucket.
- Kept `--fail-on-unexpected-runtime` global, even when output is bucket-filtered, so a focused ORM-symbol audit still fails if an unsafe runtime raw reader or mutation appears elsewhere.
- Added test coverage proving the bucket filter narrows entries without hiding the global unsafe-runtime verdict.

## Audit Result

Focused command:

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

Result:

```text
orm_trade_symbol_compat | 105
raw reader buckets       | none
unexpected readers      | 0
unexpected mutations    | 0
unclassified            | 0
```

High-level classification of the 105 runtime ORM-symbol files:

| Class | Count | Architect Read |
|---|---:|---|
| Misc runtime symbol users | 31 | Mixed surface; do not mass-rename. Needs smaller review slices. |
| Analytics / learning / reporting | 24 | Best next semantic-helper candidate pool. Mostly read-side value, lower live-order risk. |
| Live decision / risk / sizing | 20 | Keep compatibility ORM for now. These affect capital gates and sizing. |
| Live broker / order / reconcile | 14 | Do not rename yet. These are the highest blast-radius paths. |
| API / UI / schema surface | 13 | Rename later as an API/product naming project, not as a DB migration. |
| Compatibility model exports | 3 | Intentional anchors until the ORM class rename phase. |

## Data-Science Read

The rename risk is no longer table-level correctness. It is semantic coupling.

From a data model perspective, the system now has the right entities: decisions, management envelopes, and broker-authoritative positions. But a large amount of runtime code still says `Trade`, and that word now means several different things depending on caller context:

- a mutable management envelope,
- an old compatibility ORM symbol,
- a live broker-managed position surrogate,
- a reporting unit,
- or an API/UI object still named for the old mental model.

That is not a reason to stop. It is a reason to avoid a one-shot class rename. A one-shot `Trade -> ManagementEnvelope` edit would be mechanically easy but operationally sloppy: it would touch broker, order, stop, reconcile, risk, reporting, API, UI, and learning paths at once.

## Architect Verdict

Do not do the full ORM class rename yet.

Phase 5M proves the raw relation surface is safe and the remaining issue is symbol semantics. The right next move is Phase 5N: carve off low-risk read/report/helper surfaces and make them speak management-envelope semantics through helper APIs. Leave broker/order/reconcile writers and capital gates on the compatibility ORM until each live path has its own parity gate or flag.

This preserves the best property of the refactor so far: every step has been reversible, observable, and tied to a precise behavioral surface.

## Verification

```text
tests/test_phase5_remaining_trade_refs.py
tests/test_phase5l_reader_allowlist.py
9 passed
```

Analyzer:

```text
bucket | files
-------+------
orm_trade_symbol_compat | 105

raw reader bucket | files
------------------+------
(none) | 0
```

## Next Task

`f-position-identity-phase-5n-semantic-envelope-helper-slice`

Recommended scope:

1. Pick analytics/learning/reporting files from the 24-file candidate pool.
2. Add or extend semantic helper functions in `app/services/trading/management_envelopes.py`.
3. Convert only read/report callers to helper APIs.
4. Keep live broker/order/reconcile, sizing, and capital-gate paths unchanged.
5. Re-run the Phase 5M bucket audit to verify the ORM-symbol count moves down without reintroducing unsafe raw readers.
