# NEXT_TASK: f-position-identity-phase-5s-private-router-helper-slice

STATUS: PENDING

## Goal

Convert one private router read helper off the legacy `Trade` ORM while preserving public response field names.

Phase 5R classified router/schema/UI terminology and concluded that public `trade` names stay for now. The safe next move is an internal helper conversion, not a wire-contract rename.

## Recommended Work Shape

1. Add a narrow helper to `app/services/trading/management_envelopes.py`:
   - likely `load_pattern_tagged_envelope_rows(...)`
   - read from `trading_management_envelopes`
   - return mapping rows shaped like the current `ai.py` evidence payload needs
2. Convert `app/routers/trading_sub/ai.py::_api_pattern_evidence_response(...)`:
   - remove direct `Trade` import/query for matching `pattern_tags`
   - keep response key `trades`
   - keep row fields byte-compatible
3. Add a focused source/behavior test for the helper conversion.
4. Re-run:
   - relevant `ai.py` tests
   - `tests/test_management_envelopes.py`
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response field names.
- Do not touch `api_sell_trade`, monitor active setup responses, broker sync, bracket writers, stop/exit execution, order placement, PDT, promotion, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Stop if the slice requires frontend/API coordination.

## Architect Verdict

The public vocabulary can lag the internal architecture. Move private reads to semantic helpers now; rename public contracts only after aliases and caller tests exist.
