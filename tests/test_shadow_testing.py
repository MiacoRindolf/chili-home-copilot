from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.shadow_testing import _extract_trade_returns


def test_extract_trade_returns_options_use_contract_aware_realized_pnl() -> None:
    now = datetime(2026, 5, 28, 12, 0)
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=5000.0,
        direction="long",
        entry_date=now - timedelta(days=2),
        exit_date=now,
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == pytest.approx([16.0])
    assert hold_days == pytest.approx([2.0])


def test_extract_trade_returns_skips_unpriced_option_legacy_pct() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        pnl_pct=17755.61,
        direction="long",
        entry_date=datetime(2026, 5, 28, 12, 0),
        exit_date=datetime(2026, 5, 28, 13, 0),
        signal_json={"asset_type": "options", "option_meta": {"strike": 700.0}},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == []
    assert hold_days == []


def test_extract_trade_returns_keeps_stock_legacy_pct() -> None:
    trade = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=3.5,
        direction="long",
        entry_date=None,
        exit_date=None,
        signal_json={"asset_type": "stock"},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == pytest.approx([3.5])
    assert hold_days == pytest.approx([1.0])
