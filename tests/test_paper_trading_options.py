from __future__ import annotations

from datetime import datetime, timedelta
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


class _RowsQuery(_FakeQuery):
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeDb:
    def __init__(self, rows=None):
        self.added = []
        self.commits = 0
        self._rows = list(rows or [])

    def query(self, _model):
        if self._rows:
            return _RowsQuery(self._rows)
        return _FakeQuery()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        for idx, row in enumerate(self.added, start=1):
            row.id = idx

    def commit(self):
        self.commits += 1
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


def test_paper_option_exit_quote_requires_executable_side() -> None:
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
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.55,
            "mark_price": 2.55,
            "executable_price": 2.05,
            "source": "robinhood_options",
        },
    ) as quote:
        exit_price = paper_trading._paper_current_mark_price(trade, purpose="exit")

    assert exit_price == pytest.approx(2.05)
    assert quote.call_args.kwargs["purpose"] == "exit"


def test_paper_option_exit_refuses_mark_without_executable_side() -> None:
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
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.55,
            "mark_price": 2.55,
            "executable_price": None,
            "source": "robinhood_options",
        },
    ):
        exit_price = paper_trading._paper_current_mark_price(trade, purpose="exit")

    assert exit_price is None


def test_check_paper_exits_option_target_waits_for_executable_bid(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    trade = PaperTrade(
        id=101,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            **_option_signal(),
            "_paper_meta": {"expiry_days": 5, "trailing_enabled": False},
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.65,
            "mark_price": 2.65,
            "executable_price": 2.10,
            "source": "robinhood_options",
        },
    ) as quote, patch(
        "app.services.trading.paper_trading._paper_dynamic_monitor_decision",
        return_value=None,
    ):
        result = paper_trading.check_paper_exits(db, user_id=1)

    assert result == {"checked": 1, "closed": 0, "trailing_updated": 0}
    assert trade.status == "open"
    assert db.commits == 0
    assert quote.call_args.kwargs["purpose"] == "exit"


def test_shadow_option_stale_janitor_cancels_without_pnl_when_no_executable_quote(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=201,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow() - timedelta(hours=3),
        signal_json={
            **_option_signal(),
            "auto_trader_v1": True,
            "paper_shadow": True,
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 1.25,
            "mark_price": 1.25,
            "executable_price": None,
            "source": "robinhood_options_unavailable",
        },
    ), patch(
        "app.services.trading.paper_trading._apply_slippage",
        side_effect=AssertionError("unquoted stale option shadows must not book a fill"),
    ):
        result = paper_trading.prune_autotrader_paper_shadow_capacity(
            db,
            user_id=1,
            max_open=100,
            max_age_hours=1,
            buffer=5,
        )

    assert result["closed"] == 1
    assert result["cancelled"] == 1
    assert result["stale_closed"] == 0
    assert result["stale_cancelled"] == 1
    assert trade.status == paper_trading.PAPER_TRADE_STATUS_CANCELLED
    assert trade.exit_reason == paper_trading.PAPER_SHADOW_STALE_NO_QUOTE_REASON
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.pnl_pct is None
    assert trade.signal_json[paper_trading.PAPER_SHADOW_CAPACITY_EVICTION_META_KEY][
        "pnl_recorded"
    ] is False
    assert db.commits == 1


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
