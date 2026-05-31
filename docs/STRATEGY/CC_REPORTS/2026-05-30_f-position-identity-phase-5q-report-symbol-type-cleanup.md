# CC Report: f-position-identity-phase-5q-report-symbol-type-cleanup

Date: 2026-05-30
Branch: codex/brain-work-done-marker-recovery

## Summary

Phase 5Q is shipped as a type/report cleanup slice. It removes the legacy `Trade` ORM symbol from execution feedback hook annotations and cleans two brain-work close-event text surfaces that were being counted as compatibility symbols.

No runtime query, broker sync, order placement, close, stop, reconcile, PDT, promotion, or capital-gate behavior changed.

## What Changed

Converted type-only live-envelope annotations:

- `brain_work.execution_hooks._phase_a_economic_ledger_live_shadow(...)`
- `brain_work.execution_hooks.on_live_trade_closed(...)`
- `brain_work.execution_hooks.on_broker_reconciled_close(...)`

The functions still accept the same trade-like objects and still use the same attribute access. Only the annotation changes from the legacy ORM class to `Any`.

Cleaned close-event wording:

- `brain_work.handlers.quality_score`
- `brain_work.handlers.regime_ledger`

The wording now describes `trade-close outcome events` instead of a capitalized legacy `Trade` concept.

## Audit Result

Phase 5M baseline:

```text
orm_trade_symbol_compat | 105
```

After Phase 5P:

```text
orm_trade_symbol_compat | 98
```

After Phase 5Q:

```text
orm_trade_symbol_compat | 95
raw reader buckets       | none
unexpected readers      | 0
unexpected mutations    | 0
unclassified            | 0
```

Files removed from the ORM-symbol bucket in this slice:

- `app/services/trading/brain_work/execution_hooks.py`
- `app/services/trading/brain_work/handlers/quality_score.py`
- `app/services/trading/brain_work/handlers/regime_ledger.py`

## Architect Verdict

This was intentionally tiny and safe. The feedback hooks are close to live execution, so this slice avoided logic changes and only removed stale semantic coupling from annotations and explanatory text.

The remaining 95-file surface is now less about easy type cleanup and more about API/router contracts, strategy services, broker/order paths, capital/risk readers, and UI text. Continue, but keep the same discipline: semantic helper contracts first, parity gates for anything live-money adjacent, and no one-shot ORM class rename.

## Verification

```text
py_compile:
app/services/trading/brain_work/execution_hooks.py
app/services/trading/brain_work/handlers/quality_score.py
app/services/trading/brain_work/handlers/regime_ledger.py

pytest:
tests/test_phase5q_type_cleanup.py
tests/test_live_trade_close_emitter_coverage.py
tests/test_phase5_remaining_trade_refs.py
tests/test_phase5l_reader_allowlist.py

Result: 20 passed
```

Analyzer:

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime

orm_trade_symbol_compat | 95
raw reader bucket       | none
```

Note: an attempted single-test selector in `tests/test_execution_truth_wiring.py` did not match a test name. The type-only hook import is covered by `tests/test_live_trade_close_emitter_coverage.py`.

## Next Task

`f-position-identity-phase-5r-router-schema-contract-audit`

Recommended scope:

1. Audit router/schema/UI `Trade` terminology and separate product-facing legacy API contracts from internal ORM-symbol debt.
2. Do not rename public response fields yet.
3. Produce a compatibility map before changing routers or schemas.
4. Only convert private helper internals where payloads remain byte-compatible.
