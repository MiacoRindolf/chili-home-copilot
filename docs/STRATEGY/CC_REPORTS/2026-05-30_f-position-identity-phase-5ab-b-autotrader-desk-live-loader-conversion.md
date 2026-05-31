# Phase 5AB-B — AutoTrader Desk Live Loader Conversion

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** AutoTrader desk live `trades` loader only

## Summary

Converted the live `trades` loader inside `list_pattern_linked_open_positions(...)` to load read-only runtime objects from the physical `trading_management_envelopes` table.

The enrichment loop is unchanged:

- broker-stale filtering
- extra position-identity zero/closed suppression
- pattern-name enrichment
- monitor-scope classification
- option/crypto quote routing
- broker-position truth overlays
- override lookup by `("trade", id)`
- `controls_supported` / `close_supported` flags
- unrealized PnL calculation

The paper-trade path is unchanged.

## What Changed

- Added `load_autotrader_desk_live_envelope_objects(...)` to `app/services/trading/management_envelopes.py`.
- Replaced the direct `db.query(Trade)` live desk loader with that helper.
- Kept public payload keys and row shape intact.

## Guardrails Preserved

No changes to:

- paper-trade rows
- close/sell behavior
- override mutation behavior
- broker/order/reconcile/PDT/capital-gate behavior
- `/trades`, `trade_id`, schema names, UI labels, or response fields
- `trading_trades` compatibility view

## Verification

```text
python -m py_compile app\services\trading\autotrader_desk.py app\services\trading\management_envelopes.py scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5ab_autotrader_desk_loader_conversion.py tests\test_phase5ab_autotrader_desk_runtime_adapter_probe.py tests\test_autotrader_desk_api.py tests\test_management_envelopes.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 45 passed

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
PHASE5AB_ALLOW_LIVE_PROBE=true
python scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE
# old_trades=5, new_trades=5, matched=true

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: 0
```

The first focused pytest run hit a transient DB connection abort during teardown after test execution. A rerun passed cleanly (`45 passed`).

## Architect Verdict

The remaining `Trade` ORM-symbol surface is now heavily concentrated in public compatibility contracts and live-action/broker/order paths. The next move should be a live-action boundary audit, not another conversion. Monitor-run, close/sell, broker reconcile, PDT, and capital gates need explicit ownership before any further semantic-loader work.
