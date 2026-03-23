"""Unit tests for trading brain research KPI helpers."""
import pandas as pd

from app.services.trading.research_kpis import (
    annualization_factor_from_interval,
    build_research_kpis,
    information_ratio_vs_buyhold,
    win_loss_payoff_ratio_from_trades,
)


def test_annualization_factor_daily():
    assert annualization_factor_from_interval("1d") == 252.0**0.5


def test_information_ratio_basic():
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    close = pd.Series(range(100, 130), dtype=float, index=idx)
    equity = pd.Series(100_000 + (close - close.iloc[0]) * 100, index=idx)
    ir = information_ratio_vs_buyhold(equity, close, 252.0**0.5)
    assert ir is None or isinstance(ir, float)


def test_win_loss_payoff():
    df = pd.DataFrame({"ReturnPct": [0.02, -0.01, 0.03, -0.015]})
    r = win_loss_payoff_ratio_from_trades(df)
    assert r is not None
    assert r > 0


def test_build_research_kpis_from_mock_stats():
    class _S:
        def get(self, key, default=None):
            m = {
                "Sharpe Ratio": 1.1,
                "Sortino Ratio": 1.5,
                "Calmar Ratio": 0.8,
                "Max. Drawdown [%]": -12.0,
                "Volatility (Ann.) [%]": 15.0,
                "Return (Ann.) [%]": 10.0,
                "CAGR [%]": 9.0,
                "Alpha [%]": 2.0,
                "Beta": 0.4,
                "Expectancy [%]": 0.5,
                "Profit Factor": 1.7,
                "Win Rate [%]": 55.0,
            }
            return m.get(key, default)

    idx = pd.date_range("2024-01-01", periods=20, freq="D")
    eq = pd.DataFrame({"Equity": 100_000 * (1 + pd.Series(range(20), dtype=float) * 0.001).cumprod()}, index=idx)
    close = pd.Series(100 + pd.Series(range(20), dtype=float), index=idx)
    tr = pd.DataFrame({"ReturnPct": [0.01, -0.005, 0.02]})
    kpis = build_research_kpis(
        _S(),
        equity_df=eq,
        close_series=close,
        interval="1d",
        raw_trades=tr,
    )
    assert kpis.get("sharpe_ratio") == 1.1
    assert kpis.get("sortino_ratio") == 1.5
    assert kpis.get("jensen_alpha_pct") == 2.0
    assert kpis.get("win_loss_payoff_ratio") is not None
