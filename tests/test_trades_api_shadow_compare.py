from __future__ import annotations

from datetime import datetime

from app.routers.trading_sub.trades import _stable_trades_shadow_mismatches


def _route_row(**overrides):
    row = {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "entry_price": 10.25,
        "local_entry_price": 10.0,
        "exit_price": None,
        "quantity": 4.5,
        "local_quantity": 4.0,
        "entry_date": "2026-05-30T09:30:00",
        "exit_date": None,
        "status": "open",
        "pnl": None,
        "tags": "alpha",
        "notes": None,
        "broker_source": "robinhood",
        "broker_status": "filled",
        "broker_order_id": "ord-1",
        "filled_at": "2026-05-30T09:31:00",
        "avg_fill_price": 10.0,
        "tca_reference_entry_price": 9.95,
        "tca_entry_slippage_bps": 5.0,
        "tca_reference_exit_price": None,
        "tca_exit_slippage_bps": None,
        "strategy_proposal_id": 7,
        "scan_pattern_id": 585,
        "position_id": 99,
    }
    row.update(overrides)
    return row


def _envelope_row(**overrides):
    row = {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "entry_price": 10.0,
        "exit_price": None,
        "quantity": 4.0,
        "entry_date": datetime(2026, 5, 30, 9, 30),
        "exit_date": None,
        "status": "open",
        "pnl": None,
        "tags": "alpha",
        "notes": None,
        "broker_source": "robinhood",
        "broker_status": "filled",
        "broker_order_id": "ord-1",
        "filled_at": datetime(2026, 5, 30, 9, 31),
        "avg_fill_price": 10.0,
        "tca_reference_entry_price": 9.95,
        "tca_entry_slippage_bps": 5.0,
        "tca_reference_exit_price": None,
        "tca_exit_slippage_bps": None,
        "strategy_proposal_id": 7,
        "scan_pattern_id": 585,
        "position_id": 99,
    }
    row.update(overrides)
    return row


def test_shadow_compare_uses_local_values_not_broker_display_overlays():
    mismatches = _stable_trades_shadow_mismatches(
        [_route_row()],
        [_envelope_row()],
    )

    assert mismatches == []


def test_shadow_compare_flags_stable_field_mismatch():
    mismatches = _stable_trades_shadow_mismatches(
        [_route_row(local_entry_price=10.5)],
        [_envelope_row(entry_price=10.0)],
    )

    assert mismatches == [
        {
            "id": 42,
            "field": "entry_price",
            "current": 10.5,
            "envelope": 10.0,
        }
    ]
