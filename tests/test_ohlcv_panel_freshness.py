from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.trading import breadth_relstr_service
from app.services.trading import cross_asset_service
from app.services.trading import market_data
from app.services.trading.macro_regime_model import TREND_MISSING, TREND_UP


def _daily_frame(*, days_ago: int, rows: int = 30) -> pd.DataFrame:
    end_date = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    end = pd.Timestamp(end_date, tz="UTC")
    index = pd.date_range(end=end, periods=rows, freq="D")
    closes = [100.0 + float(i) for i in range(rows)]
    return pd.DataFrame({"Close": closes}, index=index)


def test_cross_asset_leg_rejects_stale_daily_ohlcv(monkeypatch):
    stale_df = _daily_frame(days_ago=10)
    monkeypatch.setattr(
        market_data,
        "fetch_ohlcv_df",
        lambda *args, **kwargs: stale_df,
    )
    monkeypatch.setattr(
        cross_asset_service.settings,
        "brain_cross_asset_max_ohlcv_age_days",
        7,
        raising=False,
    )

    leg = cross_asset_service._build_asset_leg("SPY", want_daily=True)

    assert leg.missing is True
    assert leg.last_close is None
    assert leg.returns_daily == ()


def test_cross_asset_leg_accepts_fresh_daily_ohlcv(monkeypatch):
    fresh_df = _daily_frame(days_ago=1)
    monkeypatch.setattr(
        market_data,
        "fetch_ohlcv_df",
        lambda *args, **kwargs: fresh_df,
    )
    monkeypatch.setattr(
        cross_asset_service.settings,
        "brain_cross_asset_max_ohlcv_age_days",
        7,
        raising=False,
    )

    leg = cross_asset_service._build_asset_leg("SPY", want_daily=True)

    assert leg.missing is False
    assert leg.last_close == pytest.approx(129.0)
    assert leg.ret_1d == pytest.approx(129.0 / 128.0 - 1.0)
    assert len(leg.returns_daily) == 29


def test_breadth_member_rejects_stale_daily_ohlcv(monkeypatch):
    stale_df = _daily_frame(days_ago=10)
    monkeypatch.setattr(
        market_data,
        "fetch_ohlcv_df",
        lambda *args, **kwargs: stale_df,
    )
    monkeypatch.setattr(
        breadth_relstr_service.settings,
        "brain_breadth_relstr_max_ohlcv_age_days",
        7,
        raising=False,
    )

    member = breadth_relstr_service._build_universe_member("XLK")

    assert member.missing is True
    assert member.trend == TREND_MISSING
    assert member.direction == TREND_MISSING


def test_breadth_member_rejects_unstamped_daily_ohlcv(monkeypatch):
    unstamped_df = pd.DataFrame(
        {"Close": [100.0 + float(i) for i in range(30)]}
    )
    monkeypatch.setattr(
        market_data,
        "fetch_ohlcv_df",
        lambda *args, **kwargs: unstamped_df,
    )

    member = breadth_relstr_service._build_universe_member("XLK")

    assert member.missing is True
    assert member.trend == TREND_MISSING
    assert member.direction == TREND_MISSING


def test_breadth_member_accepts_fresh_daily_ohlcv(monkeypatch):
    fresh_df = _daily_frame(days_ago=1)
    monkeypatch.setattr(
        market_data,
        "fetch_ohlcv_df",
        lambda *args, **kwargs: fresh_df,
    )
    monkeypatch.setattr(
        breadth_relstr_service.settings,
        "brain_breadth_relstr_max_ohlcv_age_days",
        7,
        raising=False,
    )

    member = breadth_relstr_service._build_universe_member("XLK")

    assert member.missing is False
    assert member.last_close == pytest.approx(129.0)
    assert member.prev_close == pytest.approx(128.0)
    assert member.momentum_20d == pytest.approx(129.0 / 109.0 - 1.0)
    assert member.trend == TREND_UP
