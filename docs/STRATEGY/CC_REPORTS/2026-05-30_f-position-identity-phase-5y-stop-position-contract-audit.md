# Phase 5Y: Stop-Position Contract Audit

Date: 2026-05-30

## Summary

Audited `/api/trading/stops/positions` after Phase 5X converted the stop-decision audit-history endpoint.

Verdict: **do not convert this endpoint directly yet.** It is not a passive table read. It is a risk-facing UI surface that builds live stop-position cards by combining management-envelope fields, broker-position truth, live quote routing, option detection, and stop-engine brain context.

## Current Contract

The endpoint currently:

- Loads open rows for the current user.
- Filters stale/duplicate broker envelopes via `filter_broker_stale_open_trades(...)`.
- Detects option rows via `is_option_trade(...)`.
- Overlays broker-authoritative entry/quantity via `broker_position_display_metrics(...)`.
- Routes quotes through `broker_quote_for_trade(...)` for broker-backed rows and `fetch_quote(...)` fallback for non-option legacy rows.
- Builds stop-engine context through `_build_brain_context(...)`.
- Computes UI state (`initial`, `breakeven`, `trailing`, `warn`, `triggered`) from current price, entry, stop, and direction.
- Returns `positions`, `suppressed_stale_trades`, and `suppressed_stale_count`.

## Field Classification

Pure management-envelope fields:

- `id`
- `ticker`
- `direction`
- `entry_price`
- `quantity`
- `stop_loss`
- `take_profit`
- `trail_stop`
- `high_watermark`
- `stop_model`
- `broker_source`
- `entry_date`
- `position_id`
- `scan_pattern_id`
- `indicator_snapshot`

Broker-truth/current-position fields:

- `broker_truth_entry_price`
- `broker_truth_quantity`
- `broker_truth_position_id`
- `broker_truth_current_envelope_id`
- `broker_truth_metrics_source`
- stale/duplicate suppression via `filter_broker_stale_open_trades(...)`

Risk-display computed fields:

- `asset_type`
- `current_price`
- `R`
- `current_r`
- `stop_distance_pct`
- `pnl_pct`
- `state`
- `brain`

Public compatibility fields:

- `positions`
- `suppressed_stale_trades`
- `suppressed_stale_count`
- `id`
- `trade_id` remains a public system-wide compatibility concept even though this endpoint uses `id`

## Why Not Convert Directly

The live helpers in this chain are still typed and tested around `Trade`-like objects:

- `filter_broker_stale_open_trades(...)`
- `broker_position_display_metrics(...)`
- `broker_quote_for_trade(...)`
- `is_option_trade(...)`
- `_build_brain_context(...)`

Many of these use attribute access and some still consult `Trade` internally for ownership checks. A blind conversion to raw dict rows would create a new untested runtime object contract in a risk-facing screen.

## Recommended Next Slice

Build a read-only candidate adapter/probe:

1. Create a lightweight management-envelope runtime object from `trading_management_envelopes` rows.
2. Feed that object through the same broker-truth, quote, option, and brain-context helpers.
3. Compare the serialized stop-position payload against the existing endpoint for live data.
4. Only if parity is clean, convert the endpoint in a later slice.

## Verification

- Read `app/routers/trading.py::api_stop_positions(...)`.
- Read broker-truth helper dependencies.
- Read option and stop-engine brain-context dependencies.
- Ran focused remaining-reference analyzer:
  - `orm_trade_symbol_compat | 94`
  - raw reader bucket: 0

No code behavior changed in this audit.

## Architect Verdict

Phase 5Y closes as an audit/no-go for immediate endpoint conversion. The endpoint can probably move later, but only after a runtime-envelope adapter proves parity. This is the correct risk posture: do the data-science probe before touching a live risk display.
