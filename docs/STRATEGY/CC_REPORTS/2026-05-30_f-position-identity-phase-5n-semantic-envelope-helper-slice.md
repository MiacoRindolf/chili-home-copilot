# CC Report: f-position-identity-phase-5n-semantic-envelope-helper-slice

Date: 2026-05-30
Branch: codex/brain-work-done-marker-recovery

## Summary

Phase 5N slice 1 is shipped as a low-risk semantic cleanup. It moves two analytics/reporting consumers off direct `Trade` ORM reads and behind management-envelope helper APIs:

- daily playbook recent-performance summary
- execution-quality / implementation-shortfall reports

No broker sync, order placement, close, stop, reconcile, or capital-gate behavior changed.

## What Changed

Added management-envelope helper contracts:

- `ClosedEnvelopePerformanceSummary`
- `summarize_closed_envelope_performance(...)`
- `load_closed_envelope_execution_rows(...)`

Converted callers:

- `daily_playbook._recent_performance(...)` now summarizes closed management envelopes through the helper.
- `execution_quality.compute_execution_stats(...)` now loads closed management-envelope rows through the helper.
- `execution_quality.compute_implementation_shortfall(...)` now uses the same helper row source.

The execution-quality row handling now accepts mapping rows, which keeps it decoupled from the legacy `Trade` ORM class.

## Audit Result

Phase 5M baseline:

```text
orm_trade_symbol_compat | 105
```

After Phase 5N slice 1:

```text
orm_trade_symbol_compat | 103
raw reader buckets       | none
unexpected readers      | 0
unexpected mutations    | 0
unclassified            | 0
```

The two removed ORM-symbol files are:

- `app/services/trading/daily_playbook.py`
- `app/services/trading/execution_quality.py`

## Architect Verdict

This is the right kind of rename work: behavior-sliced, read-side first, and small enough to verify. The system now has a repeatable pattern for reducing `Trade` ORM-symbol debt without touching live-money execution paths.

The full ORM class rename is still not the next move. The remaining 103 files include broker/order/reconcile paths and capital gates; those need individual parity gates or explicit helper contracts. But the read/report brain can continue moving to semantic helper APIs safely.

## Verification

```text
py_compile:
app/services/trading/management_envelopes.py
app/services/trading/daily_playbook.py
app/services/trading/execution_quality.py

pytest:
tests/test_management_envelopes.py
tests/test_execution_quality_envelope_helpers.py
tests/test_execution_quality_performance.py
tests/test_phase5_remaining_trade_refs.py
tests/test_phase5l_reader_allowlist.py

Result: 16 passed
```

Analyzer:

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime

orm_trade_symbol_compat | 103
raw reader bucket       | none
```

## Next Task

`f-position-identity-phase-5o-attribution-review-helper-slice`

Recommended scope:

1. Convert attribution/post-trade review readers to helper APIs.
2. Prefer `attribution_service.post_trade_review(...)` and `performance_attribution.py` before touching live broker or capital-gate paths.
3. Keep the `Trade` ORM class name unchanged.
4. Re-run the Phase 5M/5N analyzer and confirm the ORM-symbol count falls again without unsafe raw-reader regression.
