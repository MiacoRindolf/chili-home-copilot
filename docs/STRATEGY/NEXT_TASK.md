# NEXT_TASK: f-position-identity-phase-5q-report-symbol-type-cleanup

STATUS: PENDING

## Goal

Continue reducing `Trade` ORM-symbol semantic debt with another small report/type cleanup slice.

Phase 5N, 5O, and 5P moved daily playbook, execution quality, attribution, post-trade review, AI context, journal annotations, and close-attribution annotations behind management-envelope contracts or trade-like protocols. The count is now 98, with no unsafe raw readers or mutations.

## Recommended Work Shape

1. Start with read/report/type-only services that import `Trade` only for annotations or passive reporting.
2. Prefer brain-work/reporting helper surfaces before routers, schemas, broker paths, order placement, stop/exit execution, PDT, or capital gates.
3. If a surface needs actual envelope rows, add a narrow helper to `app/services/trading/management_envelopes.py` instead of querying the legacy ORM directly.
4. Preserve payloads and operator-facing text unless the text currently says `Trade` for a management-envelope concept.
5. Re-run:
   - focused tests for touched readers
   - `tests/test_management_envelopes.py`
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename the `Trade` ORM class yet.
- Do not touch broker sync, bracket writers, stop/exit execution, order placement, PDT, promotion, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Stop before the slice becomes a live-money path or API/schema contract migration.

## Architect Verdict

The mental model is getting cleaner, and the data model is already renamed. Keep taking small semantic wins while leaving live-money behavior pinned. The full ORM/API rename comes after these low-risk type/report surfaces are drained and the remaining surface is mostly explicit live-path compatibility.
