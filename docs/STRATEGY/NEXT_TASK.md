# NEXT_TASK: f-position-identity-phase-5p-context-report-helper-slice

STATUS: PENDING

## Goal

Continue shrinking `Trade` ORM-symbol semantic debt by converting another small read/report slice to management-envelope helper APIs.

Phase 5N and 5O moved daily playbook, execution-quality, pattern attribution, and post-trade review off direct `Trade` ORM reads. The count is now 101, with no unsafe raw readers or mutations.

## Recommended Work Shape

1. Start with `app/services/trading/ai_context.py`.
2. If the helper shape is still obvious, add one small journal/report/context surface.
3. Add narrow helper APIs to `app/services/trading/management_envelopes.py`.
4. Preserve response payloads and text output.
5. Re-run:
   - focused tests for the touched readers
   - `tests/test_management_envelopes.py`
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename `Trade`.
- Do not touch broker sync, bracket writers, stop/exit execution, order placement, PDT, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Stop before the slice becomes a live-money path.

## Architect Verdict

Keep taking the clean base hits. The data model is already renamed; the remaining work is mental-model cleanup. Read/report helper slices give us that clarity without putting capital paths in motion.
