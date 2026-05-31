from __future__ import annotations

import inspect
from datetime import datetime

from app.routers.trading_sub import trades


EXPECTED_TRADE_EXPORT_KEYS = [
    "id",
    "ticker",
    "direction",
    "quantity",
    "entry_price",
    "exit_price",
    "entry_date",
    "exit_date",
    "pnl",
    "status",
    "broker_source",
    "tca_entry_slippage_bps",
    "tca_exit_slippage_bps",
    "scan_pattern_id",
    "pattern_tags",
]


def test_audit_export_trade_row_shape_stays_stable() -> None:
    entry = datetime(2026, 5, 30, 9, 30)
    exit_ = datetime(2026, 5, 30, 10, 15)

    rows = trades._audit_export_trade_rows([
        {
            "id": 42,
            "ticker": "ABC",
            "direction": "long",
            "quantity": 3,
            "entry_price": 10.0,
            "exit_price": 11.0,
            "entry_date": entry,
            "exit_date": exit_,
            "pnl": 3.0,
            "status": "closed",
            "broker_source": "robinhood",
            "tca_entry_slippage_bps": 12.5,
            "tca_exit_slippage_bps": -4.0,
            "scan_pattern_id": 585,
            "pattern_tags": "compression_expansion",
        }
    ])

    assert list(rows[0].keys()) == EXPECTED_TRADE_EXPORT_KEYS
    assert rows[0] == {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "quantity": 3,
        "entry_price": 10.0,
        "exit_price": 11.0,
        "entry_date": "2026-05-30T09:30:00",
        "exit_date": "2026-05-30T10:15:00",
        "pnl": 3.0,
        "status": "closed",
        "broker_source": "robinhood",
        "tca_entry_slippage_bps": 12.5,
        "tca_exit_slippage_bps": -4.0,
        "scan_pattern_id": 585,
        "pattern_tags": "compression_expansion",
    }


def test_audit_export_trade_row_shape_handles_null_dates() -> None:
    row = {key: None for key in EXPECTED_TRADE_EXPORT_KEYS}

    rows = trades._audit_export_trade_rows([row])

    assert list(rows[0].keys()) == EXPECTED_TRADE_EXPORT_KEYS
    assert rows[0]["entry_date"] is None
    assert rows[0]["exit_date"] is None


def test_api_audit_export_uses_management_envelope_source() -> None:
    source = inspect.getsource(trades.api_audit_export)

    assert "load_audit_export_envelope_rows" in source
    assert "db.query(Trade)" not in source
    assert '"trades": trade_rows' in source
    assert 'output.write("# TRADES\\n")' in source
