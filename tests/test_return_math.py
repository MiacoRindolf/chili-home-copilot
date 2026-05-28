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


def test_trade_return_pct_option_price_fallback_requires_premium_domain() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_option_rejects_ambiguous_price_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    assert trade_return_pct(trade) is None


def test_trade_return_pct_option_rejects_implausible_premium_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert trade_return_pct(trade) is None


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


def test_paper_trade_return_pct_option_rejects_ambiguous_price_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        pnl_pct=None,
        direction="long",
        signal_json={"asset_type": "options"},
    )

    assert paper_trade_return_pct(trade) is None
