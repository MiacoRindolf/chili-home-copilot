# NEXT_TASK: f-position-identity-phase-5u-router-monitor-contract-audit

STATUS: PENDING

## Goal

Audit the remaining router/schema/UI `Trade` ORM-symbol compatibility surface after Phase 5T, and classify what can still move behind management-envelope helpers versus what must remain public compatibility contract.

Phase 5T closed the last clearly safe router helper conversion from the Phase 5R audit: audit export now reads trade rows through `load_audit_export_envelope_rows(...)` while preserving the public `trades` payload and CSV contract.

## Recommended Work Shape

1. Run the focused analyzer:
   - `python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
2. Inspect remaining router/schema/UI owners:
   - `app/routers/trading_sub/trades.py`
   - `app/routers/trading_sub/monitor.py`
   - `app/schemas/trading.py`
   - trading templates/static JS surfaces
3. Classify each remaining symbol use into:
   - public compatibility contract (`/trades`, `trade_id`, schema class names, UI labels)
   - live-path contract (broker/order/close/reconcile/PDT/capital gates)
   - private helper/reporting candidate
4. If a private helper candidate is obvious and low-risk, queue it as a narrow Phase 5V implementation slice. Otherwise stop at the audit.

## Guardrails

- Do not rename `/trades`, `trade_id`, schema classes, UI labels, or response field names.
- Do not touch broker/order/close/reconcile/PDT/capital-gate behavior.
- Do not convert public API payloads to envelope terminology yet.
- Do not one-shot rename the SQLAlchemy `Trade` class.
- Do not drop or rewrite the `trading_trades` compatibility view.

## Architect Verdict

We are no longer doing mechanical reader cleanup. The remaining work is semantic contract separation. Phase 5U should be an audit first, implementation second only if the audit finds one small private helper with clean parity.
