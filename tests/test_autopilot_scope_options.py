from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_is_option_trade_honors_asset_kind_without_snapshot() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(asset_kind="option", tags=None, indicator_snapshot=None)

    assert is_option_trade(trade) is True


def test_is_option_trade_honors_top_level_option_markers() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    assert is_option_trade(
        SimpleNamespace(
            asset_kind=None,
            tags=None,
            indicator_snapshot={"asset_kind": "option"},
        )
    ) is True
    assert is_option_trade(
        SimpleNamespace(
            asset_kind=None,
            tags=None,
            indicator_snapshot={"asset_type": "options"},
        )
    ) is True
    assert is_option_trade(
        SimpleNamespace(
            asset_kind=None,
            tags=None,
            indicator_snapshot={"options_path": True},
        )
    ) is True


def test_is_option_trade_honors_legacy_tags() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(asset_kind=None, tags="autotrader_v1 options", indicator_snapshot={})

    assert is_option_trade(trade) is True


def test_is_option_trade_nested_string_snapshot() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(
        asset_kind=None,
        tags=None,
        indicator_snapshot='{"breakout_alert":{"asset_type":"options"}}',
    )

    assert is_option_trade(trade) is True


def test_is_option_trade_honors_nested_asset_kind_snapshot() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(
        asset_kind=None,
        tags=None,
        indicator_snapshot='{"breakout_alert":{"asset_kind":"option"}}',
    )

    assert is_option_trade(trade) is True


def test_is_option_trade_plain_equity_false() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(asset_kind="equity", tags="autotrader_v1", indicator_snapshot={})

    assert is_option_trade(trade) is False


def test_asset_kind_option_quote_never_falls_back_to_stock_quote() -> None:
    from app.services.trading.broker_quotes import broker_quote_for_trade

    trade = SimpleNamespace(
        ticker="SPY",
        broker_source="robinhood",
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    with patch(
        "app.services.trading.venue.factory.get_adapter",
        side_effect=AssertionError("asset_kind option must not route to spot adapter"),
    ):
        quote = broker_quote_for_trade(trade, purpose="display")

    assert quote["price"] is None
    assert quote["source"] == "robinhood_options_unavailable"


def test_option_broker_quote_rejects_crossed_premium_market() -> None:
    from app.services.trading.broker_quotes import broker_quote_for_trade

    trade = SimpleNamespace(
        ticker="SPY",
        broker_source="robinhood",
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        },
    )
    fake_options = SimpleNamespace(
        is_enabled=lambda: True,
        find_contract=lambda *_args: {"id": "spy-729c"},
        get_quote=lambda _option_id: {
            "bid_price": "4.10",
            "ask_price": "4.00",
            "mark_price": "4.05",
        },
    )

    with patch(
        "app.services.trading.venue.factory.get_adapter",
        side_effect=AssertionError("option quote must not route to stock adapter"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        quote = broker_quote_for_trade(trade, purpose="display")

    assert quote["price"] is None
    assert quote["source"] == "robinhood_options_unavailable"
    assert quote["quote_error"] == "crossed_option_market"
