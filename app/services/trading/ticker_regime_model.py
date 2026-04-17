"""Phase L.20 - per-ticker mean-reversion vs trend regime (pure functions).

Classifies a single ticker's recent daily OHLCV into a composite
regime label in ``{trend_up, trend_down, mean_revert, choppy, neutral}``
by combining:

- **Lag-1 return autocorrelation** ``ac1``. AC(1) > 0 suggests momentum
  continuation (trend); AC(1) < 0 suggests mean reversion.
- **Variance-ratio** ``vr_5`` / ``vr_20`` (Lo-MacKinlay proxy):
  ``Var(r_Nd) / (N * Var(r_1d))``. VR > 1 = persistent / trending;
  VR < 1 = mean-reverting; VR ~ 1 = random walk.
- **Hurst exponent** ``hurst`` via rescaled-range (R/S) on log-returns.
  H > 0.5 = persistent / trending; H < 0.5 = anti-persistent /
  mean-reverting; H ~ 0.5 = random walk.
- **Trend-strength proxy** ``adx_proxy``: Kaufman-style efficiency
  ratio ``|net_change| / sum(|bar_changes|)`` over the most-recent
  ``atr_period`` bars, scaled to 0-100. Close to 100 = near-pure
  trend; close to 0 = pure noise / perfect mean-reverter. (Labelled
  ``adx_proxy`` for schema continuity even though it is not the full
  +DI/-DI directional-movement ADX.)
- **Realised volatility** ``sigma_20d`` of log-returns (stddev * sqrt(252)).

Sign of the composite is taken from the 20-day cumulative log-return:
positive cumulative return with a trend vote -> ``trend_up``;
negative -> ``trend_down``.

The pure model has **no side effects**: no DB, no network, no logging,
no config reads, no import of ``settings``. All callers wrap it with
a service-layer writer that handles OHLCV fetching, universe
iteration, mode gating, and persistence to
``trading_ticker_regime_snapshots``.

Determinism
-----------

``compute_snapshot_id(as_of_date, ticker)`` returns ``sha1(..)``
truncated to 16 hex chars. Two sweeps for the same
``(as_of_date, ticker)`` produce the same ``snapshot_id``.

Guards
------

- Zero-variance returns (constant closes) -> all scalars ``None``,
  ``coverage_score = 0.0``, label ``neutral``.
- Fewer than ``config.min_bars`` bars -> ``coverage_score = 0.0``,
  label ``neutral``, scalars ``None``.
- Any division-by-zero or NaN produces ``None`` for that single
  scalar; the composite label degrades gracefully.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICKER_REGIME_TREND_UP = "trend_up"
TICKER_REGIME_TREND_DOWN = "trend_down"
TICKER_REGIME_MEAN_REVERT = "mean_revert"
TICKER_REGIME_CHOPPY = "choppy"
TICKER_REGIME_NEUTRAL = "neutral"

_VALID_LABELS = frozenset(
    (
        TICKER_REGIME_TREND_UP,
        TICKER_REGIME_TREND_DOWN,
        TICKER_REGIME_MEAN_REVERT,
        TICKER_REGIME_CHOPPY,
        TICKER_REGIME_NEUTRAL,
    )
)


# ---------------------------------------------------------------------------
# Config + IO dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerRegimeConfig:
    """Thresholds for the pure classifier."""

    min_bars: int = 40
    min_coverage_score: float = 0.5
    # AC(1) thresholds.
    ac1_mean_revert: float = -0.05
    ac1_trend: float = 0.05
    # Hurst thresholds.
    hurst_mean_revert: float = 0.45
    hurst_trend: float = 0.55
    # Variance ratio thresholds (VR(q) / q adjusted already).
    vr_mean_revert: float = 0.95
    vr_trend: float = 1.05
    # ADX proxy trend floor (0-100 scale).
    adx_trend: float = 20.0
    # ATR period for ADX proxy.
    atr_period: int = 14


@dataclass(frozen=True)
class OHLCVSeries:
    """Daily OHLCV for a single ticker.

    ``closes`` / ``highs`` / ``lows`` are ordered oldest-first and must
    be of equal length. Empty tuples are acceptable; the classifier
    will return ``neutral`` with ``coverage_score=0.0``.
    """

    ticker: str
    asset_class: Optional[str]
    closes: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]


@dataclass(frozen=True)
class TickerRegimeInput:
    as_of_date: date
    series: OHLCVSeries
    config: TickerRegimeConfig = field(default_factory=TickerRegimeConfig)


@dataclass(frozen=True)
class TickerRegimeOutput:
    snapshot_id: str
    as_of_date: date
    ticker: str
    asset_class: Optional[str]
    last_close: Optional[float]
    sigma_20d: Optional[float]
    ac1: Optional[float]
    vr_5: Optional[float]
    vr_20: Optional[float]
    hurst: Optional[float]
    adx_proxy: Optional[float]
    trend_score: float
    mean_revert_score: float
    ticker_regime_numeric: int
    ticker_regime_label: str
    bars_used: int
    bars_missing: int
    coverage_score: float
    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Pure primitives
# ---------------------------------------------------------------------------


def _sanitize_closes(closes: Sequence[float]) -> tuple[float, ...]:
    out: list[float] = []
    for x in closes:
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f) or f <= 0.0:
            continue
        out.append(f)
    return tuple(out)


def _log_returns(closes: Sequence[float]) -> tuple[float, ...]:
    clean = _sanitize_closes(closes)
    if len(clean) < 2:
        return ()
    out: list[float] = []
    for prev, cur in zip(clean[:-1], clean[1:]):
        try:
            out.append(math.log(cur / prev))
        except (ValueError, ZeroDivisionError):
            continue
    return tuple(out)


def _mean(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    try:
        return sum(xs) / len(xs)
    except (TypeError, ValueError):
        return None


def _variance(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = _mean(xs)
    if m is None:
        return None
    total = 0.0
    for x in xs:
        total += (x - m) ** 2
    return total / (len(xs) - 1)


def _realised_vol(returns: Sequence[float]) -> Optional[float]:
    v = _variance(returns)
    if v is None or v <= 0.0:
        return None
    return math.sqrt(v) * math.sqrt(252.0)


def _ac1(returns: Sequence[float]) -> Optional[float]:
    """Lag-1 autocorrelation of ``returns``.

    Returns ``None`` for zero-variance inputs or fewer than 3 samples.
    """
    if len(returns) < 3:
        return None
    m = _mean(returns)
    if m is None:
        return None
    num = 0.0
    den = 0.0
    for i in range(1, len(returns)):
        num += (returns[i] - m) * (returns[i - 1] - m)
    for r in returns:
        den += (r - m) ** 2
    if den <= 0.0:
        return None
    return num / den


def _variance_ratio(returns: Sequence[float], q: int) -> Optional[float]:
    """Classical Lo-MacKinlay variance ratio: ``Var(r_q) / (q * Var(r_1))``.

    Uses **non-overlapping** q-period windows so the sample variance is
    unbiased (overlapping rolling windows would induce serial correlation
    between observations and underestimate the numerator for
    drift-plus-noise series).

    Interpretation:
        - VR(q) ~= 1.0 → random walk
        - VR(q) >> 1.0 → trending / persistent
        - VR(q) << 1.0 → mean-reverting / anti-persistent

    Returns ``None`` if fewer than ``2 * q`` usable returns or
    zero-variance in either numerator or denominator.
    """
    if q < 2 or len(returns) < 2 * q:
        return None
    v1 = _variance(returns)
    if v1 is None or v1 <= 0.0:
        return None
    # Non-overlapping q-period aggregate returns, oldest first; drop
    # any leading remainder so the sample is balanced.
    n = len(returns)
    remainder = n % q
    aggr: list[float] = []
    idx = remainder
    while idx + q <= n:
        aggr.append(sum(returns[idx : idx + q]))
        idx += q
    if len(aggr) < 2:
        return None
    vq = _variance(aggr)
    if vq is None:
        return None
    denom = float(q) * v1
    if denom <= 0.0:
        return None
    return vq / denom


def _hurst_rs(returns: Sequence[float], min_chunk: int = 8) -> Optional[float]:
    """Hurst exponent via rescaled-range (R/S) on returns.

    Splits the series into chunks of increasing size and fits
    ``log(R/S)`` against ``log(chunk_size)`` by OLS through a two-point
    regression over the dyadic chunk sizes that fit.
    """
    n = len(returns)
    if n < max(min_chunk * 2, 16):
        return None
    # Require positive variance overall.
    if _variance(returns) in (None, 0.0):
        return None

    chunk_sizes: list[int] = []
    size = min_chunk
    while size <= n:
        chunk_sizes.append(size)
        size *= 2
    if len(chunk_sizes) < 2:
        return None

    xs: list[float] = []
    ys: list[float] = []
    for size in chunk_sizes:
        num_chunks = n // size
        if num_chunks < 1:
            continue
        rs_values: list[float] = []
        for c in range(num_chunks):
            chunk = list(returns[c * size : (c + 1) * size])
            mu = sum(chunk) / size
            deviations = [chunk[i] - mu for i in range(size)]
            cumdev = []
            running = 0.0
            for d in deviations:
                running += d
                cumdev.append(running)
            rng = max(cumdev) - min(cumdev)
            var = sum(d * d for d in deviations) / size
            if var <= 0.0:
                continue
            s = math.sqrt(var)
            if s <= 0.0 or rng <= 0.0:
                continue
            rs_values.append(rng / s)
        if not rs_values:
            continue
        mean_rs = sum(rs_values) / len(rs_values)
        if mean_rs <= 0.0:
            continue
        xs.append(math.log(float(size)))
        ys.append(math.log(mean_rs))

    if len(xs) < 2:
        return None
    # OLS slope.
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0.0:
        return None
    h = num / den
    # Clamp to [0, 1] defensively.
    if not math.isfinite(h):
        return None
    return max(0.0, min(1.0, h))


def _adx_proxy(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Optional[float]:
    """Kaufman-style efficiency ratio over the most-recent ``period``
    close-to-close bars, scaled to 0-100.

    ER = |close[-1] - close[-period-1]| / sum(|close[i] - close[i-1]|)
    Close to 100 = near-pure trend; close to 0 = pure noise /
    perfect mean-reverter. ``highs`` / ``lows`` are accepted for
    schema parity with OHLC fetchers but are not used here.
    """

    del highs, lows
    n = len(closes)
    if n < period + 1 or period < 2:
        return None
    recent = [float(x) for x in closes[-(period + 1) :]]
    if len(recent) < 2 or not all(math.isfinite(x) and x > 0.0 for x in recent):
        return None
    net = abs(recent[-1] - recent[0])
    total = 0.0
    for prev, cur in zip(recent[:-1], recent[1:]):
        total += abs(cur - prev)
    if total <= 0.0:
        return None
    er = net / total
    return max(0.0, min(1.0, er)) * 100.0


def _cum_logret_20d(returns: Sequence[float]) -> Optional[float]:
    if len(returns) < 2:
        return None
    window = returns[-20:] if len(returns) >= 20 else returns
    return sum(window)


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


def _trend_score(
    ac1: Optional[float],
    hurst: Optional[float],
    vr_20: Optional[float],
    adx_proxy: Optional[float],
    config: TickerRegimeConfig,
) -> float:
    """Weighted count of trend-supporting features (max 5.0).

    ``adx_proxy`` (Kaufman efficiency ratio) is weighted **2.0** because
    it is the most principled single-feature trend detector: for
    drift-plus-iid-noise series, AC(1), Hurst, and VR(q) can all sit
    near random-walk values (~0, 0.5, 1.0 respectively) while the
    efficiency ratio climbs toward 1.0 with the drift/noise ratio. The
    other three contribute +1.0 each, so a noisy-but-clearly-drifting
    ticker scores at least 2.0 from ADX alone.
    """

    score = 0.0
    if ac1 is not None and ac1 >= config.ac1_trend:
        score += 1.0
    if hurst is not None and hurst >= config.hurst_trend:
        score += 1.0
    if vr_20 is not None and vr_20 >= config.vr_trend:
        score += 1.0
    if adx_proxy is not None and adx_proxy >= config.adx_trend:
        score += 2.0
    return score


def _mean_revert_score(
    ac1: Optional[float],
    hurst: Optional[float],
    vr_5: Optional[float],
    config: TickerRegimeConfig,
) -> float:
    score = 0.0
    if ac1 is not None and ac1 <= config.ac1_mean_revert:
        score += 1.0
    if hurst is not None and hurst <= config.hurst_mean_revert:
        score += 1.0
    if vr_5 is not None and vr_5 <= config.vr_mean_revert:
        score += 1.0
    return score


def _composite_label(
    adx_proxy: Optional[float],
    trend_score: float,
    mean_revert_score: float,
    cum_logret_20d: Optional[float],
    config: TickerRegimeConfig,
) -> tuple[str, int]:
    """Combine ADX (Kaufman efficiency ratio), score counters, and the
    20-day cumulative log-return into a composite label.

    Gating rules (deliberately asymmetric, since efficiency ratio is
    the most principled single-feature trend detector):

        1. ``adx_proxy >= adx_trend`` AND non-zero ``cum_logret_20d`` ->
           ``trend_up`` / ``trend_down``. A strong directional signal is
           sufficient evidence of a trend even when AC(1) / Hurst /
           VR(q) hover near random-walk values (which they do for
           drift-plus-iid-noise series).
        2. ``mean_revert_score >= 2`` AND ``mean_revert_score >
           trend_score`` AND ``adx_proxy`` is weak ->
           ``mean_revert``. ADX has to be below ``adx_trend`` here to
           avoid mislabeling a drift + serial-anticorrelation combo.
        3. Both scores zero AND ``adx_proxy`` absent / below trend
           floor -> ``neutral`` (insufficient evidence either way).
        4. Otherwise -> ``choppy``.
    """

    adx_gate = adx_proxy is not None and adx_proxy >= config.adx_trend
    if adx_gate and cum_logret_20d is not None and cum_logret_20d > 0.0:
        return TICKER_REGIME_TREND_UP, 1
    if adx_gate and cum_logret_20d is not None and cum_logret_20d < 0.0:
        return TICKER_REGIME_TREND_DOWN, -1
    if (
        mean_revert_score >= 2.0
        and mean_revert_score > trend_score
        and (adx_proxy is None or adx_proxy < config.adx_trend)
    ):
        return TICKER_REGIME_MEAN_REVERT, 0
    if trend_score == 0.0 and mean_revert_score == 0.0 and not adx_gate:
        return TICKER_REGIME_NEUTRAL, 0
    return TICKER_REGIME_CHOPPY, 0


# ---------------------------------------------------------------------------
# Snapshot id + entry point
# ---------------------------------------------------------------------------


def compute_snapshot_id(as_of_date: date, ticker: str) -> str:
    if not isinstance(as_of_date, date):
        raise TypeError("as_of_date must be a datetime.date")
    if not ticker:
        raise ValueError("ticker must be non-empty")
    payload = f"{as_of_date.isoformat()}|{ticker.strip().upper()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _neutral_output(
    inp: TickerRegimeInput,
    *,
    bars_used: int,
    last_close: Optional[float],
    reason: str,
) -> TickerRegimeOutput:
    return TickerRegimeOutput(
        snapshot_id=compute_snapshot_id(inp.as_of_date, inp.series.ticker),
        as_of_date=inp.as_of_date,
        ticker=inp.series.ticker.strip().upper(),
        asset_class=inp.series.asset_class,
        last_close=last_close,
        sigma_20d=None,
        ac1=None,
        vr_5=None,
        vr_20=None,
        hurst=None,
        adx_proxy=None,
        trend_score=0.0,
        mean_revert_score=0.0,
        ticker_regime_numeric=0,
        ticker_regime_label=TICKER_REGIME_NEUTRAL,
        bars_used=bars_used,
        bars_missing=max(0, inp.config.min_bars - bars_used),
        coverage_score=0.0,
        payload={"reason": reason},
    )


def compute_ticker_regime(inp: TickerRegimeInput) -> TickerRegimeOutput:
    """Classify a single ticker's recent daily OHLCV into a regime label.

    Pure / deterministic. See module docstring for the feature set.
    """

    if not isinstance(inp, TickerRegimeInput):
        raise TypeError("inp must be a TickerRegimeInput")
    series = inp.series
    config = inp.config
    ticker = (series.ticker or "").strip().upper()
    if not ticker:
        raise ValueError("ticker must be non-empty")

    clean_closes = _sanitize_closes(series.closes)
    last_close = clean_closes[-1] if clean_closes else None
    bars_used = len(clean_closes)

    if bars_used < config.min_bars:
        return _neutral_output(
            inp, bars_used=bars_used, last_close=last_close,
            reason="insufficient_bars",
        )

    returns = _log_returns(clean_closes)
    if _variance(returns) in (None, 0.0):
        return _neutral_output(
            inp, bars_used=bars_used, last_close=last_close,
            reason="zero_variance",
        )

    ac1 = _ac1(returns)
    vr_5 = _variance_ratio(returns, 5)
    vr_20 = _variance_ratio(returns, 20)
    hurst = _hurst_rs(returns)
    adx_proxy = _adx_proxy(series.highs, series.lows, clean_closes, config.atr_period)
    sigma_20d = _realised_vol(returns[-20:] if len(returns) >= 20 else returns)
    cum_logret_20d = _cum_logret_20d(returns)

    trend_score = _trend_score(ac1, hurst, vr_20, adx_proxy, config)
    mean_revert_score = _mean_revert_score(ac1, hurst, vr_5, config)
    label, numeric = _composite_label(
        adx_proxy, trend_score, mean_revert_score, cum_logret_20d, config
    )

    # Coverage: how many of the 6 scalars are available (ac1, vr_5,
    # vr_20, hurst, adx_proxy, sigma_20d).
    scalars = (ac1, vr_5, vr_20, hurst, adx_proxy, sigma_20d)
    present = sum(1 for s in scalars if s is not None)
    coverage_score = present / float(len(scalars))

    payload: dict[str, Any] = {
        "returns_count": len(returns),
        "cum_logret_20d": cum_logret_20d,
        "config": {
            "min_bars": config.min_bars,
            "ac1_trend": config.ac1_trend,
            "ac1_mean_revert": config.ac1_mean_revert,
            "hurst_trend": config.hurst_trend,
            "hurst_mean_revert": config.hurst_mean_revert,
            "vr_trend": config.vr_trend,
            "vr_mean_revert": config.vr_mean_revert,
            "adx_trend": config.adx_trend,
            "atr_period": config.atr_period,
        },
    }

    return TickerRegimeOutput(
        snapshot_id=compute_snapshot_id(inp.as_of_date, ticker),
        as_of_date=inp.as_of_date,
        ticker=ticker,
        asset_class=series.asset_class,
        last_close=last_close,
        sigma_20d=sigma_20d,
        ac1=ac1,
        vr_5=vr_5,
        vr_20=vr_20,
        hurst=hurst,
        adx_proxy=adx_proxy,
        trend_score=trend_score,
        mean_revert_score=mean_revert_score,
        ticker_regime_numeric=numeric,
        ticker_regime_label=label,
        bars_used=bars_used,
        bars_missing=0,
        coverage_score=coverage_score,
        payload=payload,
    )


__all__ = [
    "TICKER_REGIME_TREND_UP",
    "TICKER_REGIME_TREND_DOWN",
    "TICKER_REGIME_MEAN_REVERT",
    "TICKER_REGIME_CHOPPY",
    "TICKER_REGIME_NEUTRAL",
    "TickerRegimeConfig",
    "OHLCVSeries",
    "TickerRegimeInput",
    "TickerRegimeOutput",
    "compute_snapshot_id",
    "compute_ticker_regime",
]
