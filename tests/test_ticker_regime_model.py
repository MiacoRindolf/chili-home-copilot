"""Phase L.20 - unit tests for the pure ticker-regime model.

All tests are synthetic OHLCV; no network, no DB, no side effects.
"""
from __future__ import annotations

import math
import random
from datetime import date

import pytest

from app.services.trading.ticker_regime_model import (
    TICKER_REGIME_CHOPPY,
    TICKER_REGIME_MEAN_REVERT,
    TICKER_REGIME_NEUTRAL,
    TICKER_REGIME_TREND_DOWN,
    TICKER_REGIME_TREND_UP,
    OHLCVSeries,
    TickerRegimeConfig,
    TickerRegimeInput,
    compute_snapshot_id,
    compute_ticker_regime,
)


def _build_series(
    ticker: str,
    closes: list[float],
    *,
    asset_class: str = "equity",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> OHLCVSeries:
    h = tuple(highs if highs is not None else [c * 1.01 for c in closes])
    lo = tuple(lows if lows is not None else [c * 0.99 for c in closes])
    return OHLCVSeries(
        ticker=ticker,
        asset_class=asset_class,
        closes=tuple(closes),
        highs=h,
        lows=lo,
    )


def _build_input(
    series: OHLCVSeries,
    *,
    as_of: date = date(2026, 4, 17),
    config: TickerRegimeConfig | None = None,
) -> TickerRegimeInput:
    return TickerRegimeInput(
        as_of_date=as_of,
        series=series,
        config=config or TickerRegimeConfig(),
    )


# ---------------------------------------------------------------------------
# Determinism + basic API
# ---------------------------------------------------------------------------


def test_compute_snapshot_id_deterministic():
    a = compute_snapshot_id(date(2026, 4, 17), "AAPL")
    b = compute_snapshot_id(date(2026, 4, 17), "AAPL")
    assert a == b
    assert len(a) == 16


def test_compute_snapshot_id_case_insensitive_ticker():
    a = compute_snapshot_id(date(2026, 4, 17), "aapl")
    b = compute_snapshot_id(date(2026, 4, 17), "AAPL")
    assert a == b


def test_compute_snapshot_id_empty_ticker_raises():
    with pytest.raises(ValueError):
        compute_snapshot_id(date(2026, 4, 17), "")


def test_compute_snapshot_id_non_date_raises():
    with pytest.raises(TypeError):
        compute_snapshot_id("2026-04-17", "AAPL")  # type: ignore[arg-type]


def test_frozen_dataclasses_are_immutable():
    cfg = TickerRegimeConfig()
    with pytest.raises(Exception):
        cfg.min_bars = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Trending series
# ---------------------------------------------------------------------------


def _noisy_trend(drift: float, n: int = 120, seed: int = 42, sigma: float = 0.01) -> list[float]:
    """Drift + Gaussian-like log-return noise (deterministic PRNG).

    ``drift`` is per-bar mean log-return, ``sigma`` is the per-bar
    standard deviation. A positive ``drift`` with small ``sigma``
    yields a realistic trending series whose first-difference series
    has genuine positive autocorrelation only if the drift is strong
    relative to sigma.
    """
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n):
        shock = rng.gauss(0.0, sigma)
        closes.append(closes[-1] * math.exp(drift + shock))
    return closes


def _mean_revert_series(n: int = 120, seed: int = 7) -> list[float]:
    """Alternating +/- step with small PRNG noise.

    Guaranteed negative lag-1 autocorrelation even with light noise.
    """
    rng = random.Random(seed)
    closes = [100.0]
    step = 0.015
    for i in range(n):
        mult = (1.0 + step) if i % 2 == 0 else (1.0 - step)
        jitter = 1.0 + rng.gauss(0.0, 0.001)
        closes.append(closes[-1] * mult * jitter)
    return closes


def _random_walk(n: int = 200, seed: int = 11, sigma: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(rng.gauss(0.0, sigma)))
    return closes


def test_trending_up_classifies_trend_up():
    # Strong drift + moderate noise → ADX + AC(1) signal trend; cum-ret
    # is positive → label should be trend_up.
    closes = _noisy_trend(drift=0.015, n=150, sigma=0.012, seed=42)
    out = compute_ticker_regime(_build_input(_build_series("UP", closes)))
    assert out.ticker_regime_label == TICKER_REGIME_TREND_UP
    assert out.ticker_regime_numeric == 1
    assert out.trend_score >= 2.0
    assert out.coverage_score > 0.5


def test_trending_down_classifies_trend_down():
    closes = _noisy_trend(drift=-0.015, n=150, sigma=0.012, seed=99)
    out = compute_ticker_regime(_build_input(_build_series("DN", closes)))
    assert out.ticker_regime_label == TICKER_REGIME_TREND_DOWN
    assert out.ticker_regime_numeric == -1
    assert out.trend_score >= 2.0


def test_uptrend_ac1_positive():
    closes = _noisy_trend(drift=0.01, n=150, sigma=0.008, seed=123)
    out = compute_ticker_regime(_build_input(_build_series("UP", closes)))
    assert out.ac1 is not None and out.ac1 > 0.0


# ---------------------------------------------------------------------------
# Mean-reverting series
# ---------------------------------------------------------------------------


def test_zigzag_mean_revert_classifies_mean_revert():
    closes = _mean_revert_series(n=120, seed=7)
    out = compute_ticker_regime(_build_input(_build_series("MR", closes)))
    assert out.ticker_regime_label == TICKER_REGIME_MEAN_REVERT
    assert out.ticker_regime_numeric == 0
    assert out.ac1 is not None and out.ac1 < -0.05


def test_mean_revert_has_vr5_below_one():
    closes = _mean_revert_series(n=120, seed=13)
    out = compute_ticker_regime(_build_input(_build_series("MR", closes)))
    assert out.vr_5 is not None and out.vr_5 < 1.0


# ---------------------------------------------------------------------------
# Random / neutral / short cases
# ---------------------------------------------------------------------------


def test_constant_closes_returns_neutral_zero_coverage():
    closes = [100.0] * 80
    out = compute_ticker_regime(_build_input(_build_series("FLAT", closes)))
    assert out.ticker_regime_label == TICKER_REGIME_NEUTRAL
    assert out.coverage_score == 0.0
    assert out.ac1 is None
    assert out.hurst is None
    assert out.payload["reason"] == "zero_variance"


def test_short_history_returns_neutral():
    closes = [100.0 * (1.0 + 0.002 * i) for i in range(20)]
    out = compute_ticker_regime(_build_input(_build_series("SHORT", closes)))
    assert out.ticker_regime_label == TICKER_REGIME_NEUTRAL
    assert out.coverage_score == 0.0
    assert out.bars_used == 20
    assert out.bars_missing > 0
    assert out.payload["reason"] == "insufficient_bars"


def test_random_walk_has_near_zero_ac1():
    closes = _random_walk(n=250, seed=11, sigma=0.012)
    out = compute_ticker_regime(_build_input(_build_series("RW", closes)))
    assert out.ac1 is not None
    assert abs(out.ac1) < 0.2
    assert out.hurst is not None and 0.3 < out.hurst < 0.7
    assert out.ticker_regime_label in (
        TICKER_REGIME_CHOPPY,
        TICKER_REGIME_NEUTRAL,
    )


# ---------------------------------------------------------------------------
# ADX proxy
# ---------------------------------------------------------------------------


def test_adx_proxy_higher_for_stronger_trend():
    weak = _noisy_trend(drift=0.001, n=150, sigma=0.005, seed=7)
    strong = _noisy_trend(drift=0.02, n=150, sigma=0.005, seed=7)
    out_weak = compute_ticker_regime(_build_input(_build_series("W", weak)))
    out_strong = compute_ticker_regime(_build_input(_build_series("S", strong)))
    assert out_weak.adx_proxy is not None
    assert out_strong.adx_proxy is not None
    assert out_strong.adx_proxy >= out_weak.adx_proxy


def test_adx_proxy_none_on_zero_atr():
    closes = [100.0] * 80
    out = compute_ticker_regime(_build_input(_build_series("FLAT", closes)))
    assert out.adx_proxy is None


# ---------------------------------------------------------------------------
# Config threshold edges
# ---------------------------------------------------------------------------


def test_tighter_trend_thresholds_disqualify_weak_trend():
    closes = _noisy_trend(drift=0.001, n=150, sigma=0.015, seed=23)
    tight = TickerRegimeConfig(
        ac1_trend=0.8, hurst_trend=0.9, vr_trend=5.0, adx_trend=80.0,
    )
    out = compute_ticker_regime(_build_input(_build_series("W", closes), config=tight))
    assert out.trend_score < 2.0
    assert out.ticker_regime_label != TICKER_REGIME_TREND_UP


def test_loose_mean_revert_threshold_allows_weak_mean_revert():
    closes = _mean_revert_series(n=120, seed=29)
    loose = TickerRegimeConfig(
        ac1_mean_revert=-0.01, hurst_mean_revert=0.49, vr_mean_revert=0.99,
    )
    out = compute_ticker_regime(_build_input(_build_series("MR", closes), config=loose))
    assert out.mean_revert_score >= 2.0


# ---------------------------------------------------------------------------
# Coverage / payload
# ---------------------------------------------------------------------------


def test_coverage_score_is_between_zero_and_one():
    closes = _noisy_trend(drift=0.004, n=120, sigma=0.01)
    out = compute_ticker_regime(_build_input(_build_series("UP", closes)))
    assert 0.0 <= out.coverage_score <= 1.0


def test_payload_echoes_config():
    closes = _noisy_trend(drift=0.004, n=120, sigma=0.01)
    cfg = TickerRegimeConfig(ac1_trend=0.02)
    out = compute_ticker_regime(_build_input(_build_series("UP", closes), config=cfg))
    assert out.payload["config"]["ac1_trend"] == 0.02
    assert "returns_count" in out.payload


def test_last_close_matches_final_bar():
    closes = _noisy_trend(drift=0.004, n=120, sigma=0.01)
    out = compute_ticker_regime(_build_input(_build_series("UP", closes)))
    assert out.last_close is not None
    assert math.isclose(out.last_close, closes[-1], rel_tol=1e-9)


def test_snapshot_id_stable_across_repeat():
    closes = _noisy_trend(drift=0.004, n=120, sigma=0.01)
    inp = _build_input(_build_series("UP", closes))
    a = compute_ticker_regime(inp).snapshot_id
    b = compute_ticker_regime(inp).snapshot_id
    assert a == b


def test_empty_series_returns_neutral():
    series = OHLCVSeries(ticker="EMPTY", asset_class="equity", closes=(), highs=(), lows=())
    out = compute_ticker_regime(_build_input(series))
    assert out.ticker_regime_label == TICKER_REGIME_NEUTRAL
    assert out.coverage_score == 0.0
    assert out.last_close is None
    assert out.payload["reason"] == "insufficient_bars"
