from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)

    def first(self):
        return None


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        return _FakeQuery(self._rows)


def _option_trade_stub(**overrides):
    base = {
        "id": 5501,
        "user_id": None,
        "ticker": "SPY",
        "direction": "long",
        "entry_price": 1.25,
        "quantity": 2.0,
        "entry_date": datetime.utcnow(),
        "status": "open",
        "tags": None,
        "auto_trader_version": "v1",
        "management_scope": None,
        "indicator_snapshot": {
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _stock_trade_stub(**overrides):
    base = {
        "id": 5502,
        "user_id": None,
        "ticker": "MSFT",
        "direction": "long",
        "entry_price": 100.0,
        "quantity": 1.0,
        "entry_date": datetime.utcnow(),
        "status": "open",
        "tags": None,
        "asset_kind": "equity",
        "indicator_snapshot": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_unrealized_pnl_for_options_uses_premium_mark_not_underlying_quote() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {"mark_price": "1.45"}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == pytest.approx(40.0)
    fake_options.find_contract.assert_called_once_with("SPY", "2026-06-19", 729.0, "call")
    fake_options.get_quote.assert_called_once_with("opt-contract-1")


def test_unrealized_pnl_for_options_skips_when_no_premium_quote() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0


def test_unrealized_pnl_for_options_rejects_nonfinite_premium_quote() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {
        "mark_price": "Infinity",
        "bid_price": "1.40",
        "ask_price": "1.50",
    }

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0


def test_unrealized_pnl_for_options_rejects_crossed_premium_bbo() -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {
        "bid_price": "1.50",
        "ask_price": "1.40",
    }

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0


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
def test_unrealized_pnl_for_options_rejects_invalid_contract_identity(field, value) -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    trade.indicator_snapshot["breakout_alert"]["option_meta"][field] = value
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ) as adapter_cls:
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0
    adapter_cls.assert_not_called()
    fake_options.find_contract.assert_not_called()
    fake_options.get_quote.assert_not_called()


@pytest.mark.parametrize("contract", [{"id": ""}, {"id": None}, {"id": "   "}])
def test_unrealized_pnl_for_options_skips_blank_contract_id(contract) -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub()
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = contract
    fake_options.get_quote.side_effect = AssertionError("blank contract id must not fetch quote")

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0
    fake_options.get_quote.assert_not_called()


@pytest.mark.parametrize(
    ("entry_price", "quantity"),
    [
        (0.0, 2.0),
        (float("nan"), 2.0),
        (float("inf"), 2.0),
        (True, 2.0),
        (1.25, 0.0),
        (1.25, float("inf")),
        (1.25, True),
    ],
)
def test_unrealized_pnl_for_options_skips_invalid_pnl_basis(entry_price, quantity) -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _option_trade_stub(entry_price=entry_price, quantity=quantity)
    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {"mark_price": "1.45"}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option MTM must not fall back to underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0


@pytest.mark.parametrize(
    ("quote_price", "entry_price", "quantity"),
    [
        ("NaN", 100.0, 1.0),
        ("Infinity", 100.0, 1.0),
        (105.0, float("inf"), 1.0),
        (105.0, True, 1.0),
        (105.0, 100.0, float("inf")),
        (105.0, 100.0, True),
    ],
)
def test_unrealized_pnl_for_stocks_skips_invalid_pnl_basis(
    quote_price,
    entry_price,
    quantity,
) -> None:
    from app.services.trading.portfolio_risk import _compute_unrealized_pnl

    trade = _stock_trade_stub(entry_price=entry_price, quantity=quantity)

    with patch(
        "app.services.trading.market_data.fetch_quote",
        return_value={"price": quote_price},
    ):
        pnl = _compute_unrealized_pnl(_FakeDb([trade]), user_id=None)

    assert pnl == 0.0


def test_option_entry_notional_uses_contract_multiplier() -> None:
    from app.services.trading.portfolio_risk import _trade_entry_notional

    trade = _option_trade_stub()

    assert _trade_entry_notional(trade) == pytest.approx(250.0)


def test_option_entry_notional_rejects_boolean_notional_fields() -> None:
    from app.services.trading.portfolio_risk import _trade_entry_notional

    assert _trade_entry_notional(_option_trade_stub(entry_price=True)) == 0.0
    assert _trade_entry_notional(_option_trade_stub(quantity=True)) == 0.0


def test_portfolio_heat_for_options_rejects_boolean_notional_fields() -> None:
    from app.services.trading.portfolio_risk import _trade_risk_dollars

    assert _trade_risk_dollars(_option_trade_stub(entry_price=True)) == 0.0
    assert _trade_risk_dollars(_option_trade_stub(quantity=True)) == 0.0


def test_portfolio_heat_for_options_uses_premium_risk_and_multiplier(
    monkeypatch,
) -> None:
    from app import config as app_config
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    monkeypatch.setattr(
        app_config.settings,
        "chili_autotrader_options_exit_stop_pct",
        50.0,
        raising=False,
    )
    trade = _option_trade_stub(stop_loss=700.0)

    with patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        return_value=None,
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        return_value=None,
    ):
        budget = get_portfolio_risk_snapshot(_FakeDb([trade]), user_id=None, capital=10_000.0)

    assert budget.total_heat_pct == pytest.approx(1.25)
    assert budget.available_heat_pct == pytest.approx(4.75)


@pytest.mark.parametrize("bad_stop_pct", ["NaN", "Infinity", 0.0, 5000.0, True])
def test_portfolio_heat_for_options_defaults_malformed_stop_pct(
    monkeypatch,
    bad_stop_pct,
) -> None:
    from app import config as app_config
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    monkeypatch.setattr(
        app_config.settings,
        "chili_autotrader_options_exit_stop_pct",
        bad_stop_pct,
        raising=False,
    )
    trade = _option_trade_stub(stop_loss=None)

    with patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        return_value=None,
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        return_value=None,
    ):
        budget = get_portfolio_risk_snapshot(
            _FakeDb([trade]),
            user_id=None,
            capital=10_000.0,
        )

    assert budget.total_heat_pct == pytest.approx(1.25)
    assert budget.available_heat_pct == pytest.approx(4.75)
    assert budget.can_open_new is True


def test_portfolio_heat_for_stock_uses_explicit_stop_loss_column() -> None:
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    trade = _stock_trade_stub(entry_price=100.0, stop_loss=90.0, quantity=2.0)

    with patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        return_value=None,
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        return_value=None,
    ):
        budget = get_portfolio_risk_snapshot(
            _FakeDb([trade]),
            user_id=None,
            capital=10_000.0,
        )

    assert budget.total_heat_pct == pytest.approx(0.2)


def test_portfolio_heat_for_long_stock_stop_above_entry_has_no_entry_risk() -> None:
    from app.services.trading.portfolio_risk import _trade_risk_dollars

    trade = _stock_trade_stub(entry_price=100.0, stop_loss=105.0, quantity=2.0)

    assert _trade_risk_dollars(trade) == 0.0


def test_portfolio_heat_for_short_stock_uses_stop_above_entry() -> None:
    from app.services.trading.portfolio_risk import _trade_risk_dollars

    trade = _stock_trade_stub(
        direction="short",
        entry_price=100.0,
        stop_loss=105.0,
        quantity=2.0,
    )

    assert _trade_risk_dollars(trade) == pytest.approx(10.0)


@pytest.mark.parametrize("capital", [None, 0.0, "NaN", "not-a-number", True])
def test_portfolio_snapshot_blocks_when_capital_is_not_proven(capital) -> None:
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    with patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        side_effect=AssertionError("invalid capital must not reach VaR"),
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        side_effect=AssertionError("invalid capital must not reach CVaR"),
    ):
        budget = get_portfolio_risk_snapshot(
            _FakeDb([_stock_trade_stub()]),
            user_id=None,
            capital=capital,
        )

    assert budget.can_open_new is False
    assert budget.rejection_reason == "invalid_capital"
    assert budget.available_heat_pct == 0.0


def test_check_new_trade_allowed_blocks_invalid_capital_before_drawdown_or_sizing() -> None:
    from app.services.trading.portfolio_risk import RiskLimits, check_new_trade_allowed

    limits = RiskLimits(
        max_open_positions=10,
        max_crypto_positions=10,
        max_stock_positions=10,
        max_portfolio_heat_pct=100.0,
        max_same_ticker=10,
        max_sector_pct=100.0,
        max_avg_correlation=1.0,
    )

    with patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ), patch(
        "app.services.trading.portfolio_risk.is_breaker_tripped",
        return_value=False,
    ), patch(
        "app.services.trading.portfolio_risk.check_drawdown_breaker",
    ) as drawdown, patch(
        "app.services.trading.portfolio_risk.check_sector_concentration",
    ) as sector_check, patch(
        "app.services.trading.portfolio_risk.check_correlation_risk",
    ) as correlation_check:
        ok, reason = check_new_trade_allowed(
            _FakeDb([]),
            None,
            "SPY",
            capital="not-a-number",
            limits=limits,
        )

    assert ok is False
    assert reason == "invalid_capital"
    drawdown.assert_not_called()
    sector_check.assert_not_called()
    correlation_check.assert_not_called()


def test_portfolio_budget_counts_options_outside_stock_cap() -> None:
    from app.services.trading.portfolio_risk import get_portfolio_risk_snapshot

    rows = [
        _stock_trade_stub(ticker="MSFT"),
        _option_trade_stub(ticker="SPY"),
        _stock_trade_stub(ticker="BTC-USD", asset_kind="crypto"),
    ]

    with patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        return_value=None,
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        return_value=None,
    ):
        budget = get_portfolio_risk_snapshot(
            _FakeDb(rows),
            user_id=None,
            capital=10_000.0,
        )

    assert budget.stock_positions == 1
    assert budget.option_positions == 1
    assert budget.crypto_positions == 1


def test_option_entry_is_not_blocked_by_full_stock_cap() -> None:
    from app.services.trading.portfolio_risk import RiskLimits, check_new_trade_allowed

    limits = RiskLimits(
        max_open_positions=10,
        max_crypto_positions=10,
        max_stock_positions=1,
        max_portfolio_heat_pct=100.0,
        max_same_ticker=10,
        max_sector_pct=100.0,
        max_avg_correlation=1.0,
    )
    db = _FakeDb([_stock_trade_stub()])

    with patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ), patch(
        "app.services.trading.portfolio_risk.is_breaker_tripped",
        return_value=False,
    ), patch(
        "app.services.trading.portfolio_risk.check_drawdown_breaker",
        return_value=(False, None),
    ), patch(
        "app.services.trading.portfolio_risk.check_sector_concentration",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.portfolio_risk.check_correlation_risk",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_var",
        return_value=None,
    ), patch(
        "app.services.trading.portfolio_risk.estimate_portfolio_cvar",
        return_value=None,
    ):
        stock_ok, stock_reason = check_new_trade_allowed(
            db,
            None,
            "SPY",
            capital=10_000.0,
            limits=limits,
            asset_type="stock",
        )
        option_ok, option_reason = check_new_trade_allowed(
            db,
            None,
            "SPY",
            capital=10_000.0,
            limits=limits,
            asset_type="options",
        )

    assert not stock_ok
    assert stock_reason == "Stock cap (1) reached"
    assert option_ok
    assert option_reason == "ok"
