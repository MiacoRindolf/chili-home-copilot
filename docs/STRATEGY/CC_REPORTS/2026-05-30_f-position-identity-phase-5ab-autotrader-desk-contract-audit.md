# Phase 5AB — AutoTrader Desk Contract Audit

**Date:** 2026-05-30
**Status:** CLOSED
**Scope:** audit only; no behavior changed

## Summary

Audited `app/services/trading/autotrader_desk.py::list_pattern_linked_open_positions(...)`, the AutoTrader desk position-list surface. This is not a passive table read. It is a mixed display/control contract:

- live open envelope display
- broker-stale suppression
- broker-position truth overlays
- option/crypto quote routing
- paper-trade display
- per-position overrides
- UI control flags
- close capability flags

Architect verdict: do not convert blindly. The next safe slice is a read-only runtime-adapter parity probe for the live `trades` list only. Keep the paper-trade path and all mutation/control endpoints unchanged.

## Field Classification

Passive display fields:

- `kind`
- `id`
- `ticker`
- `direction`
- `entry_price`
- `entry_date`
- `quantity`
- `stop_loss`
- `take_profit`
- `scan_pattern_id`
- `pattern_name`
- `monitor_scope`
- `related_alert_id`
- `broker_source`
- `asset_type`
- `auto_trader_v1`
- `scale_in_count`
- `tags`
- `opened_today_et`
- `current_price`
- `unrealized_pnl_usd`
- `unrealized_pnl_pct`
- `quote_source`

Broker-truth/risk display fields:

- `broker_truth_entry_price`
- `broker_truth_quantity`
- `broker_truth_position_id`
- `broker_truth_current_envelope_id`
- `broker_truth_metrics_source`
- `suppressed_stale_trades`

Control/action-affordance fields:

- `overrides`
- `controls_supported`
- `close_supported`

Public compatibility fields that must not be renamed:

- response key `trades`
- response key `paper_trades`
- row key `kind`
- row key `id`
- row key `related_alert_id`
- override lookup key shape `("trade", id)` / `("paper", id)`

## Why A Probe Comes Before Conversion

The live `trades` loader currently uses:

```python
db.query(Trade)
  .filter(Trade.user_id == user_id, Trade.status == "open", live_autopilot_trade_filter())
  .order_by(Trade.id.desc())
```

A candidate envelope-runtime loader can match this from `trading_management_envelopes`, but the resulting object must still satisfy helper contracts used by:

- `filter_broker_stale_open_trades`
- `broker_stale_open_trade_snapshot`
- `classify_live_autopilot_trade_scope`
- `is_option_trade`
- `_broker_quote_price_for_trade`
- `broker_position_display_metrics`
- `_opened_today_et`
- `list_position_overrides`

Because the same payload exposes close/override affordances, a conversion should be gated by a live old-vs-new parity probe before endpoint code changes.

## Verification

```text
python -m py_compile app\services\trading\autotrader_desk.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_autotrader_desk_api.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 19 passed

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
# raw reader bucket: 0
```

## Next

Phase 5AB-A: add a read-only AutoTrader desk runtime-adapter parity probe. Compare current `Trade` ORM live desk rows with candidate runtime objects loaded from `trading_management_envelopes`, then feed both through the same enrichment path. Do not convert the endpoint until the probe is green.

