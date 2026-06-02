from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.trading import performance_attribution


def test_attribute_trade_short_gross_return_is_direction_aware(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=1,
        ticker="SPY",
        direction="short",
        entry_price=100.0,
        exit_price=80.0,
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["gross_return_pct"] == pytest.approx(20.0)
    assert result["alpha_pct"] == pytest.approx(20.0)


def test_attribute_trade_option_gross_return_uses_contract_multiplier(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=2,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["gross_return_pct"] == pytest.approx(16.0)
    assert result["alpha_pct"] == pytest.approx(16.0)
    assert result["estimated_cost_pct"] is None
    assert result["net_alpha_pct"] is None


def test_attribute_trade_option_uses_tca_cost_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=4,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=12,
        tca_exit_slippage_bps=18,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["estimated_cost_pct"] == pytest.approx(0.30)
    assert result["net_alpha_pct"] == pytest.approx(15.70)


def test_attribute_trade_ignores_unverified_extreme_tca_cost(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=5,
        ticker="POND-USD",
        direction="long",
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        pnl=10.0,
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=1426.0,
        tca_exit_slippage_bps=1361.0,
        avg_fill_price=None,
        broker_order_id="",
        broker_status="",
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["gross_return_pct"] == pytest.approx(10.0)
    assert result["estimated_cost_pct"] == pytest.approx(0.04)
    assert result["net_alpha_pct"] == pytest.approx(9.96)


def test_attribute_trade_option_rejects_ambiguous_underlying_price_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        performance_attribution,
        "_fetch_benchmark_return",
        lambda *_args, **_kwargs: 0.0,
    )
    trade = SimpleNamespace(
        id=3,
        ticker="SPY",
        direction="long",
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        entry_date=datetime(2026, 1, 1),
        exit_date=datetime(2026, 1, 2),
        tca_entry_slippage_bps=0,
        tca_exit_slippage_bps=0,
    )

    result = performance_attribution.attribute_trade(trade)

    assert result["error"] == "missing_return_basis"
    assert "gross_return_pct" not in result
