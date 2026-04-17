"""Phase L.21 - volatility term structure + cross-sectional dispersion
snapshot (pure functions).

Classifies three market-wide regime primitives that are missing from
L.17-L.20:

1. **VIX term structure** (VIXY / VIXM / VXZ slopes). Contango (far >
   near) = calm / risk-on; backwardation (near > far) = stress /
   risk-off.
2. **Cross-sectional dispersion** - how "alike" stocks are moving.
   Low dispersion = lockstep (single-name alpha is scarce); high
   dispersion = wide variance in daily returns (stock-picking alpha
   harvestable).
3. **Mean pairwise correlation** of daily returns across a capped slice
   of the universe. Complements dispersion: high absolute correlation
   = risk-off / macro-driven; low correlation = idiosyncratic regime.

Plus a sector-leadership churn score (Spearman rank correlation of
today's vs ``window``-days-ago 20d-return ranks across 11 sector
SPDRs; ``churn = 1 - rho**2``).

Composite labels (shadow-only in L.21.1):

- ``vol_regime_label`` in ``{vol_compressed, vol_normal,
  vol_expanded, vol_spike}``.
- ``dispersion_label`` in ``{dispersion_low, dispersion_normal,
  dispersion_high}``.
- ``correlation_label`` in ``{correlation_low, correlation_normal,
  correlation_spike}``.

The pure model has **no side effects**: no DB, no network, no
logging, no config reads, no import of ``settings``. Callers wrap it
with a service-layer writer that handles OHLCV fetching, universe
iteration, mode gating, and persistence to
``trading_vol_dispersion_snapshots``.

Determinism
-----------

``compute_snapshot_id(as_of_date)`` returns ``sha256(..)`` truncated
to 16 hex chars. Two sweeps for the same ``as_of_date`` produce the
same ``snapshot_id``.

Guards
------

- Zero-variance returns -> ``None`` for the affected scalar.
- Insufficient history (fewer than ``config.min_bars`` in any leg) ->
  ``coverage_score = 0.0`` and neutral labels across all three
  composites.
- Any division-by-zero or NaN degrades to ``None`` for that single
  scalar; the composite label degrades gracefully.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOL_REGIME_COMPRESSED = "vol_compressed"
VOL_REGIME_NORMAL = "vol_normal"
VOL_REGIME_EXPANDED = "vol_expanded"
VOL_REGIME_SPIKE = "vol_spike"

DISPERSION_LOW = "dispersion_low"
DISPERSION_NORMAL = "dispersion_normal"
DISPERSION_HIGH = "dispersion_high"

CORRELATION_LOW = "correlation_low"
CORRELATION_NORMAL = "correlation_normal"
CORRELATION_SPIKE = "correlation_spike"

_VALID_VOL_LABELS = frozenset(
    (
        VOL_REGIME_COMPRESSED,
        VOL_REGIME_NORMAL,
        VOL_REGIME_EXPANDED,
        VOL_REGIME_SPIKE,
    )
)
_VALID_DISPERSION_LABELS = frozenset(
    (DISPERSION_LOW, DISPERSION_NORMAL, DISPERSION_HIGH)
)
_VALID_CORRELATION_LABELS = frozenset(
    (CORRELATION_LOW, CORRELATION_NORMAL, CORRELATION_SPIKE)
)

_ANNUALISATION = math.sqrt(252.0)


# ---------------------------------------------------------------------------
# Config + IO dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolatilityDispersionConfig:
    """Configuration thresholds for the composite labels and sample caps."""

    # History floors
    min_bars: int = 60
    min_coverage_score: float = 0.5

    # Universe sampling caps
    universe_cap: int = 60
    corr_sample_size: int = 30

    # Vol regime (VIXY 1M spot)
    vixy_low: float = 14.0
    vixy_high: float = 22.0
    vixy_spike: float = 30.0

    # Realised-vol thresholds (annualised, e.g. 0.15 = 15%)
    realized_vol_low: float = 0.12
    realized_vol_high: float = 0.30

    # Cross-sectional return std bands (daily log-returns)
    cs_std_low: float = 0.012
    cs_std_high: float = 0.025

    # Mean absolute pairwise correlation bands
    corr_low: float = 0.35
    corr_high: float = 0.65


@dataclass(frozen=True)
class TermLeg:
    """A single VIX-term-structure / SPY leg.

    ``closes`` must be ordered oldest-first; the most recent bar is
    ``closes[-1]``. ``highs`` / ``lows`` are kept for future
    realised-range work but L.21.1 only reads ``closes``.
    """

    symbol: str
    closes: tuple[float, ...]
    highs: tuple[float, ...] = field(default_factory=tuple)
    lows: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class UniverseTicker:
    """A single universe-member return series.

    ``closes`` is ordered oldest-first. Log-returns are derived
    downstream; we retain closes (not returns) to keep the wire shape
    symmetric with ``TermLeg``.
    """

    symbol: str
    closes: tuple[float, ...]


@dataclass(frozen=True)
class VolatilityDispersionInput:
    """Complete input to :func:`compute_vol_dispersion`."""

    as_of_date: date
    # Term legs (expected keys: ``vixy``, ``vixm``, ``vxz``, ``spy``)
    term_legs: Mapping[str, TermLeg]
    # Sector SPDR legs (11 canonical sector symbols)
    sector_legs: Mapping[str, TermLeg]
    # Universe members for cross-sectional dispersion / correlation
    universe_tickers: Sequence[UniverseTicker]
    config: VolatilityDispersionConfig = field(
        default_factory=VolatilityDispersionConfig
    )


@dataclass(frozen=True)
class VolatilityDispersionOutput:
    """Full output of :func:`compute_vol_dispersion`."""

    as_of_date: date
    snapshot_id: str

    # VIX term structure
    vixy_close: Optional[float]
    vixm_close: Optional[float]
    vxz_close: Optional[float]
    vix_slope_4m_1m: Optional[float]
    vix_slope_7m_1m: Optional[float]

    # SPY realised vol
    spy_realized_vol_5d: Optional[float]
    spy_realized_vol_20d: Optional[float]
    spy_realized_vol_60d: Optional[float]
    vix_realized_gap: Optional[float]

    # Cross-sectional dispersion + correlation
    cross_section_return_std_5d: Optional[float]
    cross_section_return_std_20d: Optional[float]
    mean_abs_corr_20d: Optional[float]
    corr_sample_size: int

    # Sector leadership churn
    sector_leadership_churn_20d: Optional[float]

    # Composite labels
    vol_regime_label: str
    vol_regime_numeric: int
    dispersion_label: str
    dispersion_numeric: int
    correlation_label: str
    correlation_numeric: int

    # Coverage block
    universe_size: int
    tickers_missing: int
    coverage_score: float

    # Raw payload (config echo + per-leg last-closes for reproducibility)
    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _log_returns(closes: Sequence[float]) -> tuple[float, ...]:
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0.0 or curr <= 0.0:
            continue
        out.append(math.log(curr / prev))
    return tuple(out)


def _realised_vol(returns: Sequence[float], window: int) -> Optional[float]:
    """Annualised realised volatility over the trailing ``window`` bars."""
    if window < 2 or len(returns) < window:
        return None
    window_returns = returns[-window:]
    try:
        stdev = statistics.stdev(window_returns)
    except statistics.StatisticsError:
        return None
    if stdev == 0.0:
        return 0.0
    return round(stdev * _ANNUALISATION, 6)


def _last_close(leg: Optional[TermLeg]) -> Optional[float]:
    if leg is None or not leg.closes:
        return None
    last = leg.closes[-1]
    if last <= 0.0:
        return None
    return float(last)


def _term_slope(
    near: Optional[float], far: Optional[float]
) -> Optional[float]:
    """Far-minus-near. Positive = contango (calm); negative = backwardation."""
    if near is None or far is None:
        return None
    return round(far - near, 6)


# ---------------------------------------------------------------------------
# Cross-sectional dispersion
# ---------------------------------------------------------------------------


def _cross_section_return_std(
    universe_returns: Sequence[Sequence[float]],
    window: int,
) -> Optional[float]:
    """Mean over the last ``window`` bars of the cross-sectional stdev of
    per-ticker daily log-returns.

    Each element of ``universe_returns`` is the full log-return series
    for one ticker (oldest-first). At each of the last ``window`` bars
    we take the cross-section of returns across tickers that have a
    return defined at that bar, compute the stdev, then average.
    """
    if window < 1 or not universe_returns:
        return None

    # Find the minimum shared tail length
    min_len = min((len(r) for r in universe_returns), default=0)
    if min_len < window:
        return None

    # Take the last ``window`` bars from each ticker
    tails: list[Sequence[float]] = [r[-window:] for r in universe_returns]

    bar_stdevs: list[float] = []
    for bar_idx in range(window):
        bar_slice = [t[bar_idx] for t in tails]
        if len(bar_slice) < 2:
            continue
        try:
            s = statistics.stdev(bar_slice)
        except statistics.StatisticsError:
            continue
        bar_stdevs.append(s)

    if not bar_stdevs:
        return None

    return round(sum(bar_stdevs) / len(bar_stdevs), 6)


def _pairwise_pearson(
    a: Sequence[float], b: Sequence[float]
) -> Optional[float]:
    """Classical Pearson correlation. Returns ``None`` if either series has
    zero variance or they are different lengths.
    """
    if len(a) != len(b) or len(a) < 2:
        return None
    try:
        mean_a = statistics.fmean(a)
        mean_b = statistics.fmean(b)
    except statistics.StatisticsError:
        return None
    num = 0.0
    sq_a = 0.0
    sq_b = 0.0
    for x, y in zip(a, b):
        dx = x - mean_a
        dy = y - mean_b
        num += dx * dy
        sq_a += dx * dx
        sq_b += dy * dy
    denom = math.sqrt(sq_a * sq_b)
    if denom == 0.0:
        return None
    return num / denom


def _mean_abs_pairwise_corr(
    universe_returns: Sequence[Sequence[float]],
    window: int,
    sample_size: int,
) -> tuple[Optional[float], int]:
    """Mean of ``|pairwise correlation|`` over a sorted-ticker subset.

    Returns ``(mean_abs_corr, actual_sample_size)``. The caller is
    responsible for passing already-sorted tickers (sort by symbol)
    so reruns are deterministic.
    """
    if sample_size < 2 or window < 2:
        return None, 0

    # Filter to tickers with at least ``window`` returns
    eligible = [r for r in universe_returns if len(r) >= window]
    eligible = eligible[: int(sample_size)]
    n = len(eligible)
    if n < 2:
        return None, n

    tails = [tuple(r[-window:]) for r in eligible]

    pair_vals: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            rho = _pairwise_pearson(tails[i], tails[j])
            if rho is None:
                continue
            pair_vals.append(abs(rho))

    if not pair_vals:
        return None, n

    mean_val = sum(pair_vals) / len(pair_vals)
    return round(mean_val, 6), n


# ---------------------------------------------------------------------------
# Sector leadership churn (Spearman on 20d-return ranks)
# ---------------------------------------------------------------------------


def _spearman_from_ranks(
    ranks_today: Sequence[float], ranks_then: Sequence[float]
) -> Optional[float]:
    """Spearman rank correlation coefficient given two rank vectors of the
    same length. Uses the simplified ``1 - 6*sum(d**2)/(n*(n**2-1))``
    form which is valid when ranks are dense 1..N integers (no ties).
    """
    if len(ranks_today) != len(ranks_then) or len(ranks_today) < 2:
        return None
    n = len(ranks_today)
    diff_sq = 0.0
    for x, y in zip(ranks_today, ranks_then):
        d = x - y
        diff_sq += d * d
    denom = n * ((n * n) - 1)
    if denom == 0:
        return None
    return 1.0 - (6.0 * diff_sq / denom)


def _dense_ranks(values: Sequence[float]) -> list[int]:
    """1..N dense ranks (smallest value -> rank 1). Ties are broken by
    insertion order to keep determinism simple; the soak uses values
    that do not tie.
    """
    indexed = list(enumerate(values))
    indexed.sort(key=lambda pair: (pair[1], pair[0]))
    ranks = [0] * len(values)
    for rank, (orig_idx, _) in enumerate(indexed, start=1):
        ranks[orig_idx] = rank
    return ranks


def _sector_leadership_churn(
    sector_closes: Mapping[str, TermLeg],
    window: int,
) -> Optional[float]:
    """``1 - rho**2`` where ``rho`` is the Spearman correlation between
    today's and ``window``-days-ago ranks of 20-day percent return
    across the provided sector legs.
    """
    if window < 2:
        return None

    syms = sorted(sector_closes.keys())
    if len(syms) < 3:
        return None

    returns_today: list[float] = []
    returns_then: list[float] = []
    for sym in syms:
        leg = sector_closes[sym]
        closes = leg.closes
        if len(closes) < (window + 20):
            return None
        # Today's 20-day return
        today_close = closes[-1]
        ref_today = closes[-21]
        if today_close <= 0.0 or ref_today <= 0.0:
            return None
        returns_today.append((today_close / ref_today) - 1.0)
        # Ranks at t-window: compare close[t-window] to close[t-window-20]
        window_close = closes[-(window + 1)]
        ref_window = closes[-(window + 21)]
        if window_close <= 0.0 or ref_window <= 0.0:
            return None
        returns_then.append((window_close / ref_window) - 1.0)

    ranks_today = _dense_ranks(returns_today)
    ranks_then = _dense_ranks(returns_then)

    rho = _spearman_from_ranks(ranks_today, ranks_then)
    if rho is None:
        return None
    churn = 1.0 - (rho * rho)
    if churn < 0.0:
        churn = 0.0
    if churn > 1.0:
        churn = 1.0
    return round(churn, 6)


# ---------------------------------------------------------------------------
# Composite label logic
# ---------------------------------------------------------------------------


def _vol_regime_label(
    vixy_spot: Optional[float],
    slope_4m_1m: Optional[float],
    realized_vol_20d: Optional[float],
    config: VolatilityDispersionConfig,
) -> tuple[str, int]:
    """Classify volatility regime.

    Rules (evaluated in order):

    1. If VIXY spot is missing AND realised vol is missing -> neutral
       (``vol_normal``, numeric 0).
    2. VIXY at or above ``vixy_spike`` AND slope is negative
       (backwardation) -> ``vol_spike`` (numeric +2).
    3. VIXY at or above ``vixy_high`` OR realised-vol at or above
       ``realized_vol_high`` -> ``vol_expanded`` (numeric +1).
    4. VIXY below ``vixy_low`` AND slope positive (contango) AND
       realised-vol below ``realized_vol_low`` -> ``vol_compressed``
       (numeric -1).
    5. Otherwise -> ``vol_normal`` (numeric 0).
    """
    if vixy_spot is None and realized_vol_20d is None:
        return VOL_REGIME_NORMAL, 0

    spike_condition = (
        vixy_spot is not None
        and vixy_spot >= config.vixy_spike
        and slope_4m_1m is not None
        and slope_4m_1m < 0.0
    )
    if spike_condition:
        return VOL_REGIME_SPIKE, 2

    expanded_high_vixy = vixy_spot is not None and vixy_spot >= config.vixy_high
    expanded_high_rv = (
        realized_vol_20d is not None
        and realized_vol_20d >= config.realized_vol_high
    )
    if expanded_high_vixy or expanded_high_rv:
        return VOL_REGIME_EXPANDED, 1

    compressed_condition = (
        vixy_spot is not None
        and vixy_spot < config.vixy_low
        and slope_4m_1m is not None
        and slope_4m_1m > 0.0
        and realized_vol_20d is not None
        and realized_vol_20d < config.realized_vol_low
    )
    if compressed_condition:
        return VOL_REGIME_COMPRESSED, -1

    return VOL_REGIME_NORMAL, 0


def _dispersion_label(
    cs_std_20d: Optional[float], config: VolatilityDispersionConfig
) -> tuple[str, int]:
    if cs_std_20d is None:
        return DISPERSION_NORMAL, 0
    if cs_std_20d < config.cs_std_low:
        return DISPERSION_LOW, -1
    if cs_std_20d > config.cs_std_high:
        return DISPERSION_HIGH, 1
    return DISPERSION_NORMAL, 0


def _correlation_label(
    mean_abs_corr_20d: Optional[float],
    config: VolatilityDispersionConfig,
) -> tuple[str, int]:
    if mean_abs_corr_20d is None:
        return CORRELATION_NORMAL, 0
    if mean_abs_corr_20d < config.corr_low:
        return CORRELATION_LOW, -1
    if mean_abs_corr_20d > config.corr_high:
        return CORRELATION_SPIKE, 1
    return CORRELATION_NORMAL, 0


# ---------------------------------------------------------------------------
# snapshot_id + coverage
# ---------------------------------------------------------------------------


def compute_snapshot_id(as_of_date: date) -> str:
    """Stable 16-char SHA-256 keyed on the ``as_of_date`` only
    (market-wide snapshot)."""
    payload = f"vol_dispersion:{as_of_date.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:16]


def _coverage_score(
    term_legs_ok: int,
    term_legs_total: int,
    sector_legs_ok: int,
    sector_legs_total: int,
    universe_ok: int,
    universe_total: int,
) -> float:
    """Blend three coverage ratios (term / sector / universe) into a single
    ``[0.0, 1.0]`` score. Equal weight across the three buckets.
    """
    parts: list[float] = []
    if term_legs_total > 0:
        parts.append(term_legs_ok / term_legs_total)
    if sector_legs_total > 0:
        parts.append(sector_legs_ok / sector_legs_total)
    if universe_total > 0:
        parts.append(universe_ok / universe_total)
    if not parts:
        return 0.0
    return round(sum(parts) / len(parts), 6)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def _leg_has_min_bars(
    leg: Optional[TermLeg], min_bars: int
) -> bool:
    return leg is not None and len(leg.closes) >= min_bars


def compute_vol_dispersion(
    inp: VolatilityDispersionInput,
) -> VolatilityDispersionOutput:
    """Pure end-to-end computation.

    All heavy lifting (OHLCV fetch, universe load, DB write,
    ops logging, mode gating) lives in the service layer
    (:mod:`app.services.trading.vol_dispersion_service`).
    """
    cfg = inp.config

    # ---- Term legs --------------------------------------------------------
    vixy = inp.term_legs.get("vixy")
    vixm = inp.term_legs.get("vixm")
    vxz = inp.term_legs.get("vxz")
    spy = inp.term_legs.get("spy")

    term_legs_total = 4
    term_legs_ok = sum(
        1
        for leg in (vixy, vixm, vxz, spy)
        if _leg_has_min_bars(leg, cfg.min_bars)
    )

    vixy_close = _last_close(vixy)
    vixm_close = _last_close(vixm)
    vxz_close = _last_close(vxz)

    slope_4m_1m = _term_slope(vixy_close, vixm_close)
    slope_7m_1m = _term_slope(vixy_close, vxz_close)

    # SPY realised vol (5 / 20 / 60 day)
    spy_rv_5d: Optional[float] = None
    spy_rv_20d: Optional[float] = None
    spy_rv_60d: Optional[float] = None
    if _leg_has_min_bars(spy, cfg.min_bars):
        spy_returns = _log_returns(spy.closes)
        spy_rv_5d = _realised_vol(spy_returns, 5)
        spy_rv_20d = _realised_vol(spy_returns, 20)
        spy_rv_60d = _realised_vol(spy_returns, 60)

    # VIX - realised gap (annualised, VIXY in % points, realised_vol in
    # decimal fraction; convert realised to VIX-equivalent points by
    # multiplying by 100).
    vix_realized_gap: Optional[float] = None
    if vixy_close is not None and spy_rv_20d is not None:
        vix_realized_gap = round(vixy_close - (spy_rv_20d * 100.0), 6)

    # ---- Sector legs ------------------------------------------------------
    sector_ok = 0
    for sym, leg in inp.sector_legs.items():
        if _leg_has_min_bars(leg, cfg.min_bars):
            sector_ok += 1
    sector_total = len(inp.sector_legs)

    sector_churn: Optional[float] = None
    if sector_ok >= 3 and sector_ok == sector_total:
        # Only compute churn if we have enough history for every leg
        sector_churn = _sector_leadership_churn(inp.sector_legs, window=20)

    # ---- Universe dispersion + correlation --------------------------------
    sorted_universe = sorted(inp.universe_tickers, key=lambda u: u.symbol)
    capped_universe = sorted_universe[: cfg.universe_cap]

    universe_returns: list[tuple[float, ...]] = []
    for member in capped_universe:
        rets = _log_returns(member.closes)
        if len(rets) >= cfg.min_bars:
            universe_returns.append(rets)

    cs_std_5d = _cross_section_return_std(universe_returns, 5)
    cs_std_20d = _cross_section_return_std(universe_returns, 20)

    mean_abs_corr_20d, corr_sample_size = _mean_abs_pairwise_corr(
        universe_returns, window=20, sample_size=cfg.corr_sample_size
    )

    universe_total = len(capped_universe)
    universe_ok = len(universe_returns)

    # ---- Coverage + composite labels -------------------------------------
    coverage = _coverage_score(
        term_legs_ok,
        term_legs_total,
        sector_ok,
        sector_total,
        universe_ok,
        universe_total,
    )

    # Below min coverage -> force neutral labels
    if coverage < cfg.min_coverage_score:
        vol_label, vol_num = VOL_REGIME_NORMAL, 0
        disp_label, disp_num = DISPERSION_NORMAL, 0
        corr_label, corr_num = CORRELATION_NORMAL, 0
    else:
        vol_label, vol_num = _vol_regime_label(
            vixy_close, slope_4m_1m, spy_rv_20d, cfg
        )
        disp_label, disp_num = _dispersion_label(cs_std_20d, cfg)
        corr_label, corr_num = _correlation_label(mean_abs_corr_20d, cfg)

    snapshot_id = compute_snapshot_id(inp.as_of_date)

    payload: dict[str, Any] = {
        "config": {
            "min_bars": cfg.min_bars,
            "min_coverage_score": cfg.min_coverage_score,
            "universe_cap": cfg.universe_cap,
            "corr_sample_size": cfg.corr_sample_size,
            "vixy_low": cfg.vixy_low,
            "vixy_high": cfg.vixy_high,
            "vixy_spike": cfg.vixy_spike,
            "realized_vol_low": cfg.realized_vol_low,
            "realized_vol_high": cfg.realized_vol_high,
            "cs_std_low": cfg.cs_std_low,
            "cs_std_high": cfg.cs_std_high,
            "corr_low": cfg.corr_low,
            "corr_high": cfg.corr_high,
        },
        "coverage": {
            "term_legs_ok": term_legs_ok,
            "term_legs_total": term_legs_total,
            "sector_legs_ok": sector_ok,
            "sector_legs_total": sector_total,
            "universe_ok": universe_ok,
            "universe_total": universe_total,
            "coverage_score": coverage,
        },
        "term_legs": {
            sym: {"last_close": _last_close(leg), "bars": len(leg.closes)}
            for sym, leg in inp.term_legs.items()
        },
        "sector_legs": {
            sym: {"last_close": _last_close(leg), "bars": len(leg.closes)}
            for sym, leg in inp.sector_legs.items()
        },
    }

    return VolatilityDispersionOutput(
        as_of_date=inp.as_of_date,
        snapshot_id=snapshot_id,
        vixy_close=vixy_close,
        vixm_close=vixm_close,
        vxz_close=vxz_close,
        vix_slope_4m_1m=slope_4m_1m,
        vix_slope_7m_1m=slope_7m_1m,
        spy_realized_vol_5d=spy_rv_5d,
        spy_realized_vol_20d=spy_rv_20d,
        spy_realized_vol_60d=spy_rv_60d,
        vix_realized_gap=vix_realized_gap,
        cross_section_return_std_5d=cs_std_5d,
        cross_section_return_std_20d=cs_std_20d,
        mean_abs_corr_20d=mean_abs_corr_20d,
        corr_sample_size=corr_sample_size,
        sector_leadership_churn_20d=sector_churn,
        vol_regime_label=vol_label,
        vol_regime_numeric=vol_num,
        dispersion_label=disp_label,
        dispersion_numeric=disp_num,
        correlation_label=corr_label,
        correlation_numeric=corr_num,
        universe_size=universe_total,
        tickers_missing=max(0, universe_total - universe_ok),
        coverage_score=coverage,
        payload=payload,
    )


__all__ = [
    "VOL_REGIME_COMPRESSED",
    "VOL_REGIME_NORMAL",
    "VOL_REGIME_EXPANDED",
    "VOL_REGIME_SPIKE",
    "DISPERSION_LOW",
    "DISPERSION_NORMAL",
    "DISPERSION_HIGH",
    "CORRELATION_LOW",
    "CORRELATION_NORMAL",
    "CORRELATION_SPIKE",
    "VolatilityDispersionConfig",
    "TermLeg",
    "UniverseTicker",
    "VolatilityDispersionInput",
    "VolatilityDispersionOutput",
    "compute_snapshot_id",
    "compute_vol_dispersion",
]
