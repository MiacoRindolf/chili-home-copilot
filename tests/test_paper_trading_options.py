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

    def commit(self):
        return None


def _paper_rows(db: _FakeDb) -> list[PaperTrade]:
    return [row for row in db.added if isinstance(row, PaperTrade)]


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


def test_option_signal_honors_nested_options_path() -> None:
    from app.services.trading import paper_trading

    assert paper_trading._is_option_signal(
        {"breakout_alert": {"options_path": True}},
    )
    assert paper_trading._is_option_signal(
        {"breakout_alert": {"options_path": "yes"}},
    )
    assert not paper_trading._is_option_signal(
        {"options_path": "false", "breakout_alert": {"options_path": "false"}},
    )


def test_close_paper_trade_nested_options_path_uses_contract_multiplier(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json={"breakout_alert": {"options_path": True}},
    )

    paper_trading._close_paper_trade(trade, 1.45, "target")

    assert trade.pnl == pytest.approx(40.0)
    assert trade.pnl_pct == pytest.approx(16.0)


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


def test_auto_enter_option_signal_uses_asset_gate_and_meta_contract_quantity(
    monkeypatch,
) -> None:
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
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "confidence": 0.9,
        "option_meta": {**OPTION_META, "quantity": 2},
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ) as risk_gate, patch(
        "app.services.trading.portfolio_risk.size_position",
        side_effect=AssertionError("option contracts must not use share sizing"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 1
    risk_gate.assert_called_once()
    assert risk_gate.call_args.kwargs.get("asset_type") == "options"
    trade = _paper_rows(db)[0]
    assert trade.quantity == 2
    assert trade.entry_price == pytest.approx(1.25)
    assert trade.stop_price == pytest.approx(0.625)
    assert trade.target_price == pytest.approx(2.50)


def test_auto_enter_option_signal_sizes_contracts_with_multiplier(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "stop_price": 0.75,
        "confidence": 0.9,
        "option_meta": dict(OPTION_META),
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 1
    trade = _paper_rows(db)[0]
    assert trade.quantity == 1
    assert trade.signal_json["_paper_meta"]["contract_multiplier"] == 100.0
