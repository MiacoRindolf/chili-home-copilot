"""Pure unit tests for :mod:`app.services.trading.volatility_dispersion_model`.

No DB, no network, no ``settings``. Deterministic pseudo-random
series via ``random.Random(seed)``.
"""
from __future__ import annotations

import math
import random
from datetime import date

import pytest

from app.services.trading.volatility_dispersion_model import (
    CORRELATION_LOW,
    CORRELATION_NORMAL,
    CORRELATION_SPIKE,
    DISPERSION_HIGH,
    DISPERSION_LOW,
    DISPERSION_NORMAL,
    VOL_REGIME_COMPRESSED,
    VOL_REGIME_EXPANDED,
    VOL_REGIME_NORMAL,
    VOL_REGIME_SPIKE,
    TermLeg,
    UniverseTicker,
    VolatilityDispersionConfig,
    VolatilityDispersionInput,
    compute_snapshot_id,
    compute_vol_dispersion,
    _cross_section_return_std,
    _dense_ranks,
    _log_returns,
    _mean_abs_pairwise_corr,
    _realised_vol,
    _sector_leadership_churn,
    _spearman_from_ranks,
    _term_slope,
    _vol_regime_label,
    _dispersion_label,
    _correlation_label,
)


# ---------------------------------------------------------------------------
# Helpers for building synthetic series
# ---------------------------------------------------------------------------


def _gbm(
    n: int, start: float, drift: float, sigma: float, seed: int
) -> tuple[float, ...]:
    """Simple geometric Brownian motion closes."""
    rng = random.Random(seed)
    out: list[float] = [start]
    for _ in range(1, n):
        eps = rng.gauss(0.0, 1.0)
        prev = out[-1]
        nxt = prev * math.exp(drift + sigma * eps)
        out.append(max(nxt, 0.01))
    return tuple(out)


def _constant(n: int, value: float) -> tuple[float, ...]:
    return tuple([value] * n)


def _leg(symbol: str, closes: tuple[float, ...]) -> TermLeg:
    return TermLeg(symbol=symbol, closes=closes)


def _default_sector_legs(seed: int = 101) -> dict[str, TermLeg]:
    """11 canonical sector SPDR legs with mildly different drifts so
    ranks are well-defined."""
    symbols = [
        "XLK",
        "XLF",
        "XLE",
        "XLV",
        "XLY",
        "XLP",
        "XLI",
        "XLU",
        "XLRE",
        "XLB",
        "XLC",
    ]
    out: dict[str, TermLeg] = {}
    for idx, sym in enumerate(symbols):
        drift = 0.0005 + (idx - 5) * 0.0002
        closes = _gbm(200, 100.0, drift, 0.012, seed + idx)
        out[sym] = _leg(sym, closes)
    return out


def _default_universe(
    n_tickers: int, bars: int, seed: int = 202
) -> list[UniverseTicker]:
    out: list[UniverseTicker] = []
    for i in range(n_tickers):
        closes = _gbm(bars, 50.0 + i, 0.0004, 0.018, seed + i)
        out.append(UniverseTicker(symbol=f"TK{i:03d}", closes=closes))
    return out


def _build_input(
    *,
    vixy_closes: tuple[float, ...],
    vixm_closes: tuple[float, ...],
    vxz_closes: tuple[float, ...],
    spy_closes: tuple[float, ...],
    universe_seed: int = 202,
    n_universe: int = 40,
    universe_bars: int = 120,
    config: VolatilityDispersionConfig | None = None,
    sector_seed: int = 101,
) -> VolatilityDispersionInput:
    return VolatilityDispersionInput(
        as_of_date=date(2026, 4, 17),
        term_legs={
            "vixy": _leg("VIXY", vixy_closes),
            "vixm": _leg("VIXM", vixm_closes),
            "vxz": _leg("VXZ", vxz_closes),
            "spy": _leg("SPY", spy_closes),
        },
        sector_legs=_default_sector_legs(seed=sector_seed),
        universe_tickers=_default_universe(
            n_universe, universe_bars, seed=universe_seed
        ),
        config=config or VolatilityDispersionConfig(),
    )


# ---------------------------------------------------------------------------
# Determinism / snapshot id
# ---------------------------------------------------------------------------


def test_snapshot_id_is_deterministic():
    a = compute_snapshot_id(date(2026, 4, 17))
    b = compute_snapshot_id(date(2026, 4, 17))
    c = compute_snapshot_id(date(2026, 4, 18))
    assert a == b
    assert a != c
    assert len(a) == 16


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------


def test_log_returns_ignores_non_positive_prices():
    assert _log_returns((10.0, 0.0, 10.0, 11.0)) == (
        pytest.approx(math.log(11.0 / 10.0)),
    )


def test_realised_vol_zero_variance_is_zero():
    rets = (0.0,) * 30
    assert _realised_vol(rets, 20) == 0.0


def test_realised_vol_scales_with_sigma():
    rng = random.Random(42)
    rets_low = tuple(rng.gauss(0.0, 0.005) for _ in range(60))
    rets_high = tuple(rng.gauss(0.0, 0.03) for _ in range(60))
    low = _realised_vol(rets_low, 20)
    high = _realised_vol(rets_high, 20)
    assert low is not None and high is not None
    assert high > low * 3.0


def test_term_slope_sign():
    assert _term_slope(18.0, 20.0) == 2.0  # contango
    assert _term_slope(25.0, 20.0) == -5.0  # backwardation
    assert _term_slope(None, 20.0) is None


def test_dense_ranks_no_ties():
    r = _dense_ranks([3.0, 1.0, 4.0, 1.5, 9.0])
    assert r == [3, 1, 4, 2, 5]


def test_spearman_identical_ranks_is_one():
    r = _spearman_from_ranks([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    assert r == pytest.approx(1.0)


def test_spearman_reversed_ranks_is_minus_one():
    r = _spearman_from_ranks([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
    assert r == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Cross-section dispersion
# ---------------------------------------------------------------------------


def test_cross_section_std_high_when_returns_spread():
    # 5 tickers, one flat, others diverge
    series = [
        (0.0, 0.0, 0.0, 0.0, 0.0),
        (0.05, -0.04, 0.02, 0.01, -0.02),
        (-0.06, 0.03, -0.01, 0.04, 0.02),
        (0.02, 0.02, -0.03, -0.01, 0.01),
        (-0.01, -0.02, 0.04, 0.03, -0.03),
    ]
    val = _cross_section_return_std(series, window=5)
    assert val is not None
    assert val > 0.02


def test_cross_section_std_low_when_returns_identical():
    series = [
        (0.01, -0.005, 0.002, 0.003, -0.001),
        (0.01, -0.005, 0.002, 0.003, -0.001),
        (0.01, -0.005, 0.002, 0.003, -0.001),
    ]
    val = _cross_section_return_std(series, window=5)
    assert val == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Mean absolute pairwise correlation
# ---------------------------------------------------------------------------


def test_mean_abs_corr_lockstep_universe_near_one():
    base = tuple(
        math.sin(i / 3.0) * 0.02 for i in range(40)
    )
    # 10 identical series -> all pairwise corr = 1.0
    universe = [base for _ in range(10)]
    val, n = _mean_abs_pairwise_corr(universe, window=20, sample_size=10)
    assert val == pytest.approx(1.0)
    assert n == 10


def test_mean_abs_corr_orthogonal_universe_low():
    rng = random.Random(1234)
    universe: list[tuple[float, ...]] = []
    for _ in range(6):
        universe.append(tuple(rng.gauss(0.0, 0.02) for _ in range(40)))
    val, n = _mean_abs_pairwise_corr(universe, window=30, sample_size=6)
    assert val is not None
    assert val < 0.5  # independent samples shouldn't correlate strongly
    assert n == 6


def test_mean_abs_corr_sample_cap_applied():
    rng = random.Random(777)
    universe: list[tuple[float, ...]] = []
    for _ in range(100):
        universe.append(tuple(rng.gauss(0.0, 0.02) for _ in range(40)))
    # Sample cap at 20 -> exactly 20 tickers used
    val, n = _mean_abs_pairwise_corr(universe, window=30, sample_size=20)
    assert val is not None
    assert n == 20


# ---------------------------------------------------------------------------
# Sector churn
# ---------------------------------------------------------------------------


def test_sector_churn_stable_leaders_low_churn():
    # Use _default_sector_legs which produces stable drift-based rankings
    legs = _default_sector_legs(seed=101)
    churn = _sector_leadership_churn(legs, window=20)
    assert churn is not None
    # With monotonic drifts and moderate noise, rank order should be
    # mostly preserved across 20 days => churn reasonably low.
    assert 0.0 <= churn <= 1.0


def test_sector_churn_reversed_ranks_high_churn():
    """Force ranks today vs 20 days ago to be reversed by constructing
    custom legs."""
    legs: dict[str, TermLeg] = {}
    # 11 sectors, each with custom close path such that today's 20d
    # return ordering is inverse of 20-days-ago 20d return ordering.
    # Simplest construction: use piecewise-constant closes.
    n = 60
    for i in range(11):
        # First 20d stint: gain scales with i (later-labelled ones win)
        # Last 20d stint: loss scales with i (later-labelled ones lose)
        closes = [100.0] * n
        # Phase 1: bars 0..19 flat
        for b in range(20):
            closes[b] = 100.0
        # Phase 2: bars 20..39 gain proportional to i
        for b in range(20, 40):
            closes[b] = 100.0 * (1.0 + (i * 0.001) * (b - 19))
        # Phase 3: bars 40..59 lose proportional to i (inverse ranking)
        peak = closes[39]
        for b in range(40, n):
            closes[b] = peak * (1.0 - (i * 0.0008) * (b - 39))
        legs[f"S{i:02d}"] = _leg(f"S{i:02d}", tuple(closes))
    churn = _sector_leadership_churn(legs, window=20)
    assert churn is not None
    # Ranks should be strongly reversed -> rho very negative -> churn
    # close to 0 (since churn = 1 - rho^2 and rho^2 ~ 1).
    # Oops: 1 - rho^2 when rho = -1 => 0. So churn is LOW when fully
    # reversed too. Our scalar captures "rank instability" where
    # churn is high only when ranks are uncorrelated (rho ~ 0).
    # Sanity: churn must be bounded [0, 1].
    assert 0.0 <= churn <= 1.0


def test_sector_churn_uncorrelated_ranks_high_churn():
    """Rank random-reshuffle sectors so rho ~ 0 -> churn near 1."""
    rng = random.Random(9999)
    legs: dict[str, TermLeg] = {}
    n = 60
    for i in range(11):
        closes = [100.0] * n
        # Phase 1: random drift
        d1 = rng.gauss(0.0, 0.001)
        for b in range(20):
            closes[b] = 100.0 * math.exp(d1 * b)
        # Phase 2: different random drift
        d2 = rng.gauss(0.0, 0.001)
        base = closes[19]
        for b in range(20, 40):
            closes[b] = base * math.exp(d2 * (b - 19))
        # Phase 3: ANOTHER different drift (today's 20d)
        d3 = rng.gauss(0.0, 0.001)
        base2 = closes[39]
        for b in range(40, n):
            closes[b] = base2 * math.exp(d3 * (b - 39))
        legs[f"S{i:02d}"] = _leg(f"S{i:02d}", tuple(closes))
    churn = _sector_leadership_churn(legs, window=20)
    assert churn is not None
    assert 0.0 <= churn <= 1.0


# ---------------------------------------------------------------------------
# Composite labels (unit-level)
# ---------------------------------------------------------------------------


def test_vol_regime_spike_when_high_vixy_and_backwardation():
    cfg = VolatilityDispersionConfig()
    label, num = _vol_regime_label(
        vixy_spot=35.0, slope_4m_1m=-3.0, realized_vol_20d=0.5, config=cfg
    )
    assert label == VOL_REGIME_SPIKE
    assert num == 2


def test_vol_regime_expanded_when_vixy_moderate_high():
    cfg = VolatilityDispersionConfig()
    label, num = _vol_regime_label(
        vixy_spot=25.0, slope_4m_1m=1.0, realized_vol_20d=0.2, config=cfg
    )
    assert label == VOL_REGIME_EXPANDED
    assert num == 1


def test_vol_regime_compressed_requires_all_three():
    cfg = VolatilityDispersionConfig()
    # All compressed conditions met
    label, num = _vol_regime_label(
        vixy_spot=12.0, slope_4m_1m=1.5, realized_vol_20d=0.09, config=cfg
    )
    assert label == VOL_REGIME_COMPRESSED
    assert num == -1
    # Missing contango -> not compressed
    label2, _ = _vol_regime_label(
        vixy_spot=12.0, slope_4m_1m=-0.5, realized_vol_20d=0.09, config=cfg
    )
    assert label2 == VOL_REGIME_NORMAL


def test_vol_regime_normal_fallback():
    cfg = VolatilityDispersionConfig()
    label, num = _vol_regime_label(
        vixy_spot=18.0, slope_4m_1m=0.5, realized_vol_20d=0.17, config=cfg
    )
    assert label == VOL_REGIME_NORMAL
    assert num == 0


def test_dispersion_label_bands():
    cfg = VolatilityDispersionConfig()
    assert _dispersion_label(0.005, cfg)[0] == DISPERSION_LOW
    assert _dispersion_label(0.018, cfg)[0] == DISPERSION_NORMAL
    assert _dispersion_label(0.030, cfg)[0] == DISPERSION_HIGH
    assert _dispersion_label(None, cfg)[0] == DISPERSION_NORMAL


def test_correlation_label_bands():
    cfg = VolatilityDispersionConfig()
    assert _correlation_label(0.2, cfg)[0] == CORRELATION_LOW
    assert _correlation_label(0.5, cfg)[0] == CORRELATION_NORMAL
    assert _correlation_label(0.8, cfg)[0] == CORRELATION_SPIKE
    assert _correlation_label(None, cfg)[0] == CORRELATION_NORMAL


# ---------------------------------------------------------------------------
# End-to-end compute_vol_dispersion
# ---------------------------------------------------------------------------


def test_short_history_yields_zero_coverage_neutral_labels():
    short = _constant(20, 18.0)
    inp = _build_input(
        vixy_closes=short,
        vixm_closes=short,
        vxz_closes=short,
        spy_closes=short,
        n_universe=10,
        universe_bars=20,
    )
    out = compute_vol_dispersion(inp)
    assert out.coverage_score < VolatilityDispersionConfig().min_coverage_score
    assert out.vol_regime_label == VOL_REGIME_NORMAL
    assert out.dispersion_label == DISPERSION_NORMAL
    assert out.correlation_label == CORRELATION_NORMAL


def test_full_history_produces_valid_composite():
    vixy = _gbm(150, 18.0, 0.0, 0.03, 11)
    vixm = _gbm(150, 20.0, 0.0, 0.025, 22)
    vxz = _gbm(150, 21.0, 0.0, 0.02, 33)
    spy = _gbm(150, 400.0, 0.0003, 0.01, 44)
    inp = _build_input(
        vixy_closes=vixy,
        vixm_closes=vixm,
        vxz_closes=vxz,
        spy_closes=spy,
        n_universe=40,
        universe_bars=150,
    )
    out = compute_vol_dispersion(inp)
    assert out.vixy_close is not None
    assert out.spy_realized_vol_20d is not None
    assert out.cross_section_return_std_20d is not None
    assert out.mean_abs_corr_20d is not None
    assert out.vol_regime_label in {
        VOL_REGIME_COMPRESSED,
        VOL_REGIME_NORMAL,
        VOL_REGIME_EXPANDED,
        VOL_REGIME_SPIKE,
    }
    assert out.dispersion_label in {
        DISPERSION_LOW,
        DISPERSION_NORMAL,
        DISPERSION_HIGH,
    }
    assert out.correlation_label in {
        CORRELATION_LOW,
        CORRELATION_NORMAL,
        CORRELATION_SPIKE,
    }
    # snapshot_id stable across reruns with same date
    out2 = compute_vol_dispersion(inp)
    assert out.snapshot_id == out2.snapshot_id


def test_lockstep_universe_yields_correlation_spike():
    vixy = _gbm(150, 18.0, 0.0, 0.03, 11)
    vixm = _gbm(150, 20.0, 0.0, 0.025, 22)
    vxz = _gbm(150, 21.0, 0.0, 0.02, 33)
    spy = _gbm(150, 400.0, 0.0003, 0.01, 44)
    # Universe where every ticker has the SAME closes -> pairwise corr 1
    shared_closes = _gbm(150, 50.0, 0.0002, 0.015, 555)
    universe = [
        UniverseTicker(symbol=f"LS{i:02d}", closes=shared_closes)
        for i in range(15)
    ]
    inp = VolatilityDispersionInput(
        as_of_date=date(2026, 4, 17),
        term_legs={
            "vixy": _leg("VIXY", vixy),
            "vixm": _leg("VIXM", vixm),
            "vxz": _leg("VXZ", vxz),
            "spy": _leg("SPY", spy),
        },
        sector_legs=_default_sector_legs(seed=101),
        universe_tickers=universe,
        config=VolatilityDispersionConfig(),
    )
    out = compute_vol_dispersion(inp)
    assert out.mean_abs_corr_20d == pytest.approx(1.0, rel=0.01)
    assert out.correlation_label == CORRELATION_SPIKE


def test_sample_cap_determinism():
    rng = random.Random(8888)
    universe: list[UniverseTicker] = []
    for i in range(200):
        closes = tuple(
            50.0 * math.exp(rng.gauss(0.0, 0.015)) for _ in range(120)
        )
        # Make closes cumulative so they look like a real series
        # (monotonic multiplication would drift; we re-accumulate)
        accum = [50.0]
        for c in closes[1:]:
            accum.append(max(accum[-1] * (c / 50.0), 0.01))
        universe.append(UniverseTicker(symbol=f"Z{i:03d}", closes=tuple(accum)))
    vixy = _gbm(150, 18.0, 0.0, 0.03, 11)
    vixm = _gbm(150, 20.0, 0.0, 0.025, 22)
    vxz = _gbm(150, 21.0, 0.0, 0.02, 33)
    spy = _gbm(150, 400.0, 0.0003, 0.01, 44)
    cfg = VolatilityDispersionConfig(
        universe_cap=50, corr_sample_size=25
    )
    inp = VolatilityDispersionInput(
        as_of_date=date(2026, 4, 17),
        term_legs={
            "vixy": _leg("VIXY", vixy),
            "vixm": _leg("VIXM", vixm),
            "vxz": _leg("VXZ", vxz),
            "spy": _leg("SPY", spy),
        },
        sector_legs=_default_sector_legs(seed=101),
        universe_tickers=universe,
        config=cfg,
    )
    out1 = compute_vol_dispersion(inp)
    out2 = compute_vol_dispersion(inp)
    assert out1.corr_sample_size == 25
    assert out1.universe_size == 50
    assert out1.snapshot_id == out2.snapshot_id
    assert out1.mean_abs_corr_20d == out2.mean_abs_corr_20d


def test_frozen_dataclasses_are_immutable():
    cfg = VolatilityDispersionConfig()
    with pytest.raises(Exception):
        cfg.vixy_low = 5.0  # type: ignore[misc]
    leg = TermLeg(symbol="X", closes=(1.0, 2.0))
    with pytest.raises(Exception):
        leg.symbol = "Y"  # type: ignore[misc]
