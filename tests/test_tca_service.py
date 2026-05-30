from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading import tca_service
from app.services.trading.tca_service import (
    apply_tca_on_trade_close,
    apply_tca_on_trade_fill,
    entry_slippage_bps,
    exit_slippage_bps,
    resolve_arrival_price,
    resolve_exit_reference_price,
)


@pytest.mark.parametrize("bad", [None, True, False, float("nan"), float("inf"), -1.0, 0.0])
def test_slippage_rejects_non_positive_nonfinite_and_boolean_prices(bad):
    assert entry_slippage_bps(100.0, bad, "long") is None
    assert entry_slippage_bps(bad, 100.0, "long") is None
    assert exit_slippage_bps(100.0, bad, "long") is None
    assert exit_slippage_bps(bad, 100.0, "long") is None


def test_apply_tca_fill_ignores_invalid_average_fill_without_entry_fallback():
    trade = SimpleNamespace(
        tca_reference_entry_price=100.0,
        avg_fill_price=float("nan"),
        entry_price=105.0,
        direction="long",
        tca_entry_slippage_bps=None,
    )

    apply_tca_on_trade_fill(trade)

    assert trade.tca_entry_slippage_bps is None


def test_apply_tca_close_ignores_invalid_reference_or_fill():
    trade = SimpleNamespace(
        tca_reference_exit_price=True,
        exit_price=99.0,
        direction="long",
        tca_exit_slippage_bps=None,
    )
    apply_tca_on_trade_close(trade)
    assert trade.tca_exit_slippage_bps is None

    trade.tca_reference_exit_price = 100.0
    trade.exit_price = float("inf")
    apply_tca_on_trade_close(trade)
    assert trade.tca_exit_slippage_bps is None


def test_resolve_arrival_price_rejects_crossed_or_nonfinite_quotes(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda _ticker: {"bid": 101.0, "ask": 100.0, "price": float("nan")},
    )

    out = resolve_arrival_price("SPY", signal_price=99.0)

    assert out["source"] == "signal_price"
    assert out["arrival_price"] == 99.0


def test_resolve_arrival_price_rejects_boolean_signal(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda _ticker: {"bid": None, "ask": None, "price": None},
    )

    out = resolve_arrival_price("SPY", signal_price=True)

    assert out["source"] == "unavailable"
    assert out["arrival_price"] is None


def test_resolve_exit_reference_price_falls_back_only_to_valid_prices(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda _ticker: {"price": float("inf")},
    )

    assert resolve_exit_reference_price("SPY", explicit=True, fill_fallback=98.5) == 98.5
    assert resolve_exit_reference_price(
        "SPY",
        explicit=float("nan"),
        fill_fallback=float("nan"),
    ) == 0.0
