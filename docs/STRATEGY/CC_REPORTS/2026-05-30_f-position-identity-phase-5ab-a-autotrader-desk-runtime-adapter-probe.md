# Phase 5AB-A — AutoTrader Desk Runtime-Adapter Probe

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** read-only parity probe; no endpoint behavior changed

## Summary

Added `scripts/d-phase5ab-autotrader-desk-runtime-adapter-probe.py`, a read-only old-vs-new parity probe for the live `trades` list returned by the AutoTrader desk.

The probe compares:

- current path: `Trade` ORM live desk rows
- candidate path: runtime objects loaded from physical `trading_management_envelopes`

Both paths are then fed through the same live desk enrichment contract:

- broker-stale filtering
- extra position-identity zero/closed suppression
- pattern-name enrichment
- monitor-scope classification
- option/crypto quote routing
- broker-position truth overlays
- override lookup by `("trade", id)`
- `controls_supported` / `close_supported` flags
- unrealized PnL calculation

## Live Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
matched=true
old_trades=5
new_trades=5
old_suppressed=0
new_suppressed=0
quote_cache_entries=5
fallback_cache_entries=0
trading_management_envelopes=r
trading_trades=v
```

## Guardrails Preserved

The probe did not touch:

- AutoTrader desk endpoint behavior
- paper-trade rows
- close/sell behavior
- override mutation behavior
- broker/order/reconcile/PDT/capital-gate behavior
- `/trades`, `trade_id`, schema names, UI labels, response fields, or compatibility view semantics

## Verification

```text
python -m py_compile scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py app\services\trading\autotrader_desk.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5ab_autotrader_desk_runtime_adapter_probe.py tests\test_autotrader_desk_api.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 25 passed

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
PHASE5AB_ALLOW_LIVE_PROBE=true
python scripts\d-phase5ab-autotrader-desk-runtime-adapter-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: 0
```

## Architect Verdict

The live desk `trades` list is ready for a narrow conversion to load runtime objects from `trading_management_envelopes`. Keep the paper-trade path and all mutation/action endpoints unchanged. Preserve public row keys and the `("trade", id)` override/close compatibility contract.
