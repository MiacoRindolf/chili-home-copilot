# NEXT_TASK: f-position-identity-phase-5o-attribution-review-helper-slice

STATUS: PENDING

## Goal

Continue reducing `Trade` ORM-symbol semantic debt in read/report code by moving attribution and post-trade review surfaces behind management-envelope helper APIs.

Phase 5N slice 1 proved the shape: low-risk analytics/reporting callers can speak management-envelope semantics without changing live execution behavior. The ORM-symbol count dropped from 105 to 103 and the raw-reader/mutation guard stayed clean.

## Recommended Work Shape

1. Target attribution/reporting only:
   - `app/services/trading/attribution_service.py`
   - `app/services/trading/performance_attribution.py`
2. Add narrow helper functions to `app/services/trading/management_envelopes.py` for the repeated closed-envelope row patterns.
3. Preserve response payloads and scoring math.
4. Add tests that prove helpers read `trading_management_envelopes`, not `trading_trades`.
5. Re-run:
   - `tests/test_management_envelopes.py`
   - attribution/performance focused tests
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename `Trade`.
- Do not touch broker sync, bracket writers, stop/exit execution, order placement, PDT, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Do not absorb unrelated dirty worktree files.

## Architect Verdict

Keep the refactor on rails: semantic helper slices for read/report code first, live-money paths only after each surface has its own parity gate. This gives the system the data-model clarity of the rename without risking a capital-path surprise.
