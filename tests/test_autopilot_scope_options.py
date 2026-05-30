from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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
            indicator_snapshot={"asset_class": "options"},
        )
    ) is True
    assert is_option_trade(
        SimpleNamespace(
            asset_kind=None,
            tags=None,
            indicator_snapshot={"options_path": True},
        )
    ) is True
    assert is_option_trade(
        SimpleNamespace(
            asset_kind=None,
            tags=None,
            indicator_snapshot={"option_contract_multiplier": 100.0},
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


def test_is_option_trade_honors_nested_asset_class_snapshot() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(
        asset_kind=None,
        tags=None,
        indicator_snapshot='{"breakout_alert":{"asset_class":"options"}}',
    )

    assert is_option_trade(trade) is True


def test_is_option_trade_honors_nested_contract_multiplier_snapshot() -> None:
    from app.services.trading.autopilot_scope import is_option_trade

    trade = SimpleNamespace(
        asset_kind=None,
        tags=None,
        indicator_snapshot='{"breakout_alert":{"contract_multiplier":100.0}}',
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strike", True),
        ("strike", 0.0),
        ("strike", float("nan")),
        ("strike", float("inf")),
        ("expiration", "not-a-date"),
        ("option_type", "banana"),
    ],
)
def test_option_broker_quote_rejects_invalid_contract_identity_before_adapter(
    field,
    value,
) -> None:
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
    trade.indicator_snapshot["option_meta"][field] = value

    with patch(
        "app.services.trading.venue.factory.get_adapter",
        side_effect=AssertionError("option quote must not route to stock adapter"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
    ) as adapter_cls:
        quote = broker_quote_for_trade(trade, purpose="display")

    assert quote["price"] is None
    assert quote["source"] == "robinhood_options_unavailable"
    adapter_cls.assert_not_called()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("bid_price", True),
        ("ask_price", "Infinity"),
        ("mark_price", "NaN"),
        ("adjusted_mark_price", object()),
        ("last_trade_price", -1.0),
    ],
)
def test_option_broker_quote_rejects_malformed_premium_field(field, bad_value) -> None:
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
            "bid_price": "1.40",
            "ask_price": "1.50",
            "mark_price": "1.45",
            field: bad_value,
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
    assert quote["quote_error"] == "malformed_option_quote"


def test_option_broker_quote_normalizes_contract_identity_before_lookup() -> None:
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
                "expiration": "20260619",
                "strike": "729",
                "option_type": "C",
            },
        },
    )
    fake_options = SimpleNamespace(
        is_enabled=lambda: True,
        find_contract=MagicMock(return_value={"id": "spy-729c"}),
        get_quote=MagicMock(return_value={"mark_price": "1.45"}),
    )

    with patch(
        "app.services.trading.venue.factory.get_adapter",
        side_effect=AssertionError("option quote must not route to stock adapter"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        quote = broker_quote_for_trade(trade, purpose="display")

    assert quote["price"] == pytest.approx(1.45)
    assert quote["option_expiration"] == "2026-06-19"
    assert quote["option_strike"] == pytest.approx(729.0)
    assert quote["option_type"] == "call"
    fake_options.find_contract.assert_called_once_with("SPY", "2026-06-19", 729.0, "call")
