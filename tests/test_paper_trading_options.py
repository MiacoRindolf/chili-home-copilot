from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.trading import PaperTrade


OPTION_META = {
    "underlying": "SPY",
    "expiration": "2026-06-19",
    "strike": 729.0,
    "option_type": "call",
    "limit_price": 1.25,
}


def _option_signal() -> dict:
    return {
        "asset_type": "options",
        "options_path": True,
        "option_meta": dict(OPTION_META),
    }


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def count(self):
        return 0

    def first(self):
        return None


class _FakeDb:
    def __init__(self):
        self.added = []

    def query(self, _model):
        return _FakeQuery()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        for idx, row in enumerate(self.added, start=1):
            row.id = idx


def test_autotrader_paper_entry_context_uses_option_premium() -> None:
    from app.services.trading.auto_trader import _paper_entry_context_for_alert

    alert = SimpleNamespace(entry_price=9.99)
    entry_price, signal = _paper_entry_context_for_alert(
        alert,
        px=729.0,
        snap={"options_path": True, "option_meta": dict(OPTION_META)},
    )

    assert entry_price == pytest.approx(1.25)
    assert signal["asset_type"] == "options"
    assert signal["options_path"] is True
    assert signal["option_meta"]["strike"] == 729.0
    assert signal["underlying_price_at_entry"] == pytest.approx(729.0)


def test_open_paper_trade_option_defaults_to_premium_levels(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_stop_pct",
        50.0,
        raising=False,
    )
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_tp_pct",
        100.0,
        raising=False,
    )

    with patch(
        "app.services.trading.paper_trading._compute_atr_levels",
        side_effect=AssertionError("option paper rows should not use underlying ATR levels"),
    ):
        trade = paper_trading.open_paper_trade(
            _FakeDb(),
            user_id=1,
            ticker="SPY",
            entry_price=1.25,
            quantity=2.0,
            signal_json=_option_signal(),
        )

    assert trade is not None
    assert trade.entry_price == pytest.approx(1.25)
    assert trade.stop_price == pytest.approx(0.625)
    assert trade.target_price == pytest.approx(2.50)
    assert trade.signal_json["_paper_meta"]["contract_multiplier"] == 100.0


def test_close_paper_trade_option_uses_contract_multiplier(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    paper_trading._close_paper_trade(trade, 1.45, "target")

    assert trade.pnl == pytest.approx(40.0)
    assert trade.pnl_pct == pytest.approx(16.0)


def test_paper_option_mark_uses_option_quote_not_underlying(monkeypatch) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option paper MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ) as quote:
        mark = paper_trading._paper_current_mark_price(trade)

    assert mark == pytest.approx(1.45)
    proxy = quote.call_args.args[0]
    assert proxy.indicator_snapshot["option_meta"]["limit_price"] == pytest.approx(1.25)
