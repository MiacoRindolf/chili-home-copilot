# CC Report: f-position-identity-phase-5u-router-monitor-contract-audit

Date: 2026-05-30
Status: CLOSED

## Summary

Audited the remaining router/schema/UI `Trade` ORM-symbol compatibility surface after Phase 5T. Verdict: do **not** rename public router/schema/UI contracts yet, and do **not** one-shot rename the SQLAlchemy `Trade` class.

The remaining router-facing uses are semantically load-bearing. They are either public compatibility contracts (`/trades`, `trade_id`, schema class names, UI labels), live-path contracts (sell/close/monitor/stop execution behavior), or small read-only candidates that need old-vs-new parity before conversion.

## Evidence

Focused analyzer:

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime

orm_trade_symbol_compat | 94
raw reader bucket | (none) | 0
```

Router/schema/UI inspection:

- `app/routers/trading_sub/trades.py`
  - Public `/api/trading/trades` CRUD, close, delete, assign-pattern, apply-levels, and sell endpoints remain public compatibility contracts.
  - `api_sell_trade(...)` is a live broker/manual close path and must not move without a dedicated parity/soak.
  - Phase 5T already moved the audit export trade-section read behind `load_audit_export_envelope_rows(...)`.
- `app/routers/trading_sub/monitor.py`
  - Active monitor setup rendering still uses `Trade` objects because broker-position truth, option detection, quote selection, and execution-state display all accept management-envelope objects today.
  - `api_monitor_run(...)` passes `Trade` objects to `run_pattern_position_monitor_for_trades(...)`; this is live behavior, not a safe rename target.
  - `api_monitor_decisions(...)` and `api_monitor_imminent_alerts(...)` are read-only candidates, but they affect monitor UI surfacing and need old-vs-new parity before conversion.
- `app/routers/trading.py`
  - `_resolve_open_trade_for_ticker(...)` and AI plan linkage write public `trade_id` values back into chat/trading flows.
  - AutoTrader position override/close/adopt/unadopt routes expose `{trade_id}` as a public API path contract.
  - Stop positions and stop decisions are monitor/control surfaces; stop positions use broker truth and stop-engine context, so they are not safe for blind conversion.
- `app/schemas/trading.py`
  - `TradeCreate`, `TradeClose`, `TradeSell`, `TradeOut`, `JournalCreate.trade_id`, and `JournalOut.trade_id` are public API schema contracts.
- UI templates/static JS
  - The `Trades` tab, `Trade #...` labels, journal trade rows, monitor card `trade_id`, and fetch URLs under `/api/trading/trades...` are user/API compatibility surfaces, not internal implementation details.

## Architect Verdict

Phase 5T was the last safe helper conversion from the Phase 5R audit. Phase 5U confirms the project is now past mechanical cleanup.

The next work must be parity-first:

1. Probe old vs new monitor-decision/imminent-alert behavior.
2. If old/new match, convert the narrow read-only monitor candidate.
3. Leave public `/trades`, `trade_id`, schema names, UI labels, and live broker/stop/close paths alone until each has its own compatibility strategy.

## What Not To Do

- Do not rename `/trades`.
- Do not rename `trade_id` payloads.
- Do not rename Pydantic schema classes.
- Do not change monitor card UI labels.
- Do not convert live sell/close/stop/monitor-run paths without a feature flag and parity evidence.
- Do not remove the `trading_trades` compatibility view.

## Next

`f-position-identity-phase-5v-monitor-read-parity-probe`

Build a read-only probe for:

- `api_monitor_decisions(...)` old `Trade` join vs envelope join.
- `api_monitor_imminent_alerts(...)` old actioned-alert `Trade` exists predicate vs envelope exists predicate.
- Optional: `api_stop_decisions(...)` old stop-decision `Trade` join vs envelope join.

Only convert code after parity is proven.
