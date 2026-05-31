# NEXT_TASK: f-position-identity-phase-5r-router-schema-contract-audit

STATUS: PENDING

## Goal

Classify the remaining router/schema/UI `Trade` terminology before changing public contracts.

Phase 5Q reduced the internal ORM-symbol compatibility count to 95. The easy type/report-only cleanup is slowing down; the remaining surface increasingly includes API response schemas, routers, UI templates, live broker/order/reconcile paths, and strategy services.

## Recommended Work Shape

1. Start with `app/routers/trading_sub/*.py`, `app/schemas/trading.py`, and the trading templates/static JS entries that still appear in the `orm_trade_symbol_compat` bucket.
2. Produce a compatibility map:
   - product/API fields that must stay named `trade` for now
   - internal helper variables that can safely become `envelope`
   - surfaces that need versioned response fields or docs before changing
3. Only implement changes where payloads remain byte-compatible and tests can pin that.
4. Re-run:
   - relevant router/schema/UI tests
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Guardrails

- Do not rename public API fields unless a compatibility alias is added and tested.
- Do not touch broker sync, bracket writers, stop/exit execution, order placement, PDT, promotion, or capital gates.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Stop before a router/schema change requires coordinated frontend/API migration.

## Architect Verdict

The next risk is not database correctness; it is breaking callers by "cleaning up" names that are really public contracts. Audit first. Convert private helper internals only where the wire contract stays unchanged.
