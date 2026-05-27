from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


def _option_snapshot() -> dict:
    return {
        "breakout_alert": {
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        }
    }


def _option_trade(*, trade_id: int = 2, entry_date: datetime | None = None):
    return SimpleNamespace(
        id=trade_id,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        entry_date=entry_date or datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        asset_kind="option",
        indicator_snapshot=_option_snapshot(),
    )


def _stock_trade(*, trade_id: int = 1, entry_date: datetime | None = None):
    return SimpleNamespace(
        id=trade_id,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=729.0,
        quantity=1.0,
        entry_date=entry_date or datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        asset_kind=None,
        indicator_snapshot={},
    )


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = list(rows)

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)


def test_ticker_level_broker_quote_skips_option_contract_rows() -> None:
    from app.services.trading.broker_quotes import open_broker_trade_for_ticker

    option = _option_trade(trade_id=2)
    stock = _stock_trade(trade_id=1)
    selected = open_broker_trade_for_ticker(_FakeDb([option, stock]), "SPY", user_id=1)

    assert selected is not None
    assert selected.id == stock.id


def test_ticker_level_broker_quote_returns_none_for_option_only() -> None:
    from app.services.trading.broker_quotes import broker_quote_for_user_ticker

    with patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        side_effect=AssertionError("ticker quote must not resolve option premium"),
    ):
        quote = broker_quote_for_user_ticker(
            _FakeDb([_option_trade()]),
            user_id=1,
            ticker="SPY",
            purpose="display",
        )

    assert quote is None
