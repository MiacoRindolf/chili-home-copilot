# CC Report: f-position-identity-phase-5r-router-schema-contract-audit

Date: 2026-05-30
Branch: codex/brain-work-done-marker-recovery

## Summary

Phase 5R is a contract audit, not a rename. The remaining router/schema/UI `Trade` terminology is partly internal debt and partly public product/API contract. A blind rename here would break callers and dashboard code.

No code behavior changed in this phase.

## Audit Result

Focused analyzer:

```text
orm_trade_symbol_compat | 95
raw reader bucket       | none
```

Router/schema/UI entries currently in the compatibility bucket:

- `app/routers/trading.py`
- `app/routers/trading_sub/ai.py`
- `app/routers/trading_sub/monitor.py`
- `app/routers/trading_sub/scanning.py`
- `app/routers/trading_sub/trades.py`
- `app/schemas/trading.py`
- `app/static/js/brain-core.js`
- `app/static/js/brain-trading-desk.js`
- `app/templates/brain/_runtime_help_modal.html`
- `app/templates/trading/_tab_screener.html`
- `app/templates/trading/_tab_trades.html`
- `app/templates/trading.html`
- `app/templates/trading_backup.html`

## Compatibility Map

### Must Stay For Now

These are public or semi-public contracts. Do not rename them without versioned aliases and frontend/API tests.

- `/api/trading/trades`
- `/api/trading/trades/{trade_id}/...`
- `/api/trading/journal` and `journal.trade_id`
- `/api/trading/stats/calendar` response field `trades`
- `/api/trading/audit/export` response section `trades`
- monitor response field `trade_id`
- dashboard/UI labels such as `Trade tab`, `Open Trade`, and trade analytics panels
- schema classes `TradeCreate`, `TradeClose`, `TradeSell`, `TradeAssignPattern`, `TradeApplyLevels`, `TradeOut`

Architect read: these names are caller vocabulary, not just implementation vocabulary.

### Private Internals That Can Move Next

These can be converted behind helper APIs while preserving payload field names:

- `app/routers/trading_sub/ai.py::_api_pattern_evidence_response(...)`
  - currently imports `Trade` only to find matching rows by `pattern_tags`
  - safe next slice: add `load_pattern_tagged_envelope_rows(...)` and keep response key `trades`
- `app/routers/trading_sub/trades.py::api_audit_export(...)`
  - read-only export; can read semantic envelopes while keeping export section `trades`
  - needs CSV/JSON parity tests before conversion

### Live/API Surfaces Requiring Parity Gates

These should not move in a casual cleanup slice:

- `app/routers/trading_sub/trades.py::api_sell_trade(...)`
  - live broker/order path
- `app/routers/trading_sub/monitor.py`
  - active monitor API still calls live broker truth, broker quotes, and pattern-position monitor
  - response contract is `trade_id`-centric and used by dashboard code
- `app/routers/trading_sub/trades.py` CRUD endpoints
  - public endpoints and service-level mutation paths still intentionally use the legacy compatibility ORM

### UI Text

Most UI instances are product copy, not ORM debt:

- "Trade analytics"
- "Top Trade Ideas"
- "Trades"
- "Open Trade"
- "Trade tab"

Those should remain until a product rename is intentionally designed. "Management envelope" is the internal architecture term; it is not yet user-facing language.

## Architect Verdict

Do not do a public rename yet. The next safe implementation slice is a private helper conversion inside `ai.py` plus, optionally, audit export read-source conversion with byte-compatible payloads.

The correct rule from here: preserve public `trade` names unless there is a compatibility alias and a test proving old callers still work.

## Verification

```text
python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime

orm_trade_symbol_compat | 95
raw reader bucket       | none
```

This phase is documentation/audit only, so no runtime tests were required.

## Next Task

`f-position-identity-phase-5s-private-router-helper-slice`

Recommended scope:

1. Add a helper for pattern-tagged management-envelope rows.
2. Convert `ai.py::_api_pattern_evidence_response(...)` to that helper while keeping the response key `trades`.
3. Optionally convert `api_audit_export(...)` to a semantic-envelope helper if JSON/CSV parity tests are straightforward.
4. Do not touch `api_sell_trade`, monitor active setup responses, or public schema names.
