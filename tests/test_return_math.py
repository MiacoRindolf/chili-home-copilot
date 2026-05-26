from types import SimpleNamespace

import pytest

from app.services.trading.return_math import (
    paper_trade_return_pct,
    price_return_pct,
    trade_return_pct,
)


def test_price_return_pct_is_direction_aware_for_shorts() -> None:
    assert price_return_pct(100.0, 80.0, "short") == pytest.approx(20.0)
    assert price_return_pct(100.0, 120.0, "short") == pytest.approx(-20.0)


def test_trade_return_pct_option_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_option_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=None,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)
