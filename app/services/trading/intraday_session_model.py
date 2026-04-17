"""Phase L.22 — intraday session regime snapshot (pure functions).

Classifies how a US equity session unfolded intraday using SPY 5-minute
bars. Captures features invisible to daily-bar-only regimes (L.17-L.21):

1. **Opening range (OR)** — the first ``or_minutes`` of the RTH session
   (default 30 min = 6 × 5-min bars). Its height (``or_range_pct``) is
   one of the most robust equity alpha primitives.
2. **Midday compression** — range of the 12:00-14:00 ET window relative
   to the OR range. Tight midday often precedes afternoon trends.
3. **Power hour (PH)** — the last ``power_minutes`` (default 30 min =
   6 × 5-min bars). Its range + volume often decides whether a trend
   continues into close or reverses.
4. **Gap open magnitude** vs **session behaviour** — classifies
   gap-and-go (close on gap side) vs gap-fade (close against gap).
5. **Intraday realised vol** — annualised sqrt of sum-of-squared 5-min
   log-returns scaled by 252 × 78 bars/session.

Composite label (shadow-only in L.22.1):

- ``session_trending_up`` (+1) / ``session_trending_down`` (-1)
- ``session_range_bound`` (0)
- ``session_reversal`` (+2)
- ``session_gap_and_go`` (+3 up / -3 down)
- ``session_gap_fade`` (-3 up / +3 down) — gap direction inverted
- ``session_compressed`` (0)
- ``session_neutral`` (0) — insufficient bars / degenerate data

The pure model has **no side effects**: no DB, no network, no logging,
no config reads, no import of ``settings``. Callers wrap it with a
service-layer writer that handles OHLCV fetching, mode gating, and
persistence to ``trading_intraday_session_snapshots``.

Determinism
-----------

``compute_snapshot_id(as_of_date)`` returns
``sha256('intraday_session:' + iso)[:16]``. Two sweeps for the same
``as_of_date`` produce the same ``snapshot_id``.

Timezone note
-------------

``IntradayBar.ts_minute`` is an integer encoding **minutes since
midnight in US/Eastern**. The service layer is responsible for
normalising incoming pandas timestamps into this ET minute-of-day
representation before calling the pure model. The pure model itself
treats ``ts_minute`` purely as a minute counter relative to the RTH
open (``rth_open_minute = 9*60 + 30 = 570``) and does no timezone
math.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_TRENDING_UP = "session_trending_up"
SESSION_TRENDING_DOWN = "session_trending_down"
SESSION_RANGE_BOUND = "session_range_bound"
SESSION_REVERSAL = "session_reversal"
SESSION_GAP_AND_GO = "session_gap_and_go"
SESSION_GAP_FADE = "session_gap_fade"
SESSION_COMPRESSED = "session_compressed"
SESSION_NEUTRAL = "session_neutral"

VALID_SESSION_LABELS = frozenset(
    [
        SESSION_TRENDING_UP,
        SESSION_TRENDING_DOWN,
        SESSION_RANGE_BOUND,
        SESSION_REVERSAL,
        SESSION_GAP_AND_GO,
        SESSION_GAP_FADE,
        SESSION_COMPRESSED,
        SESSION_NEUTRAL,
    ]
)

# Regular trading hours (ET): 09:30 -> 16:00
RTH_OPEN_MINUTE: int = 9 * 60 + 30  # 570
RTH_CLOSE_MINUTE: int = 16 * 60  # 960
RTH_BARS_5MIN: int = (RTH_CLOSE_MINUTE - RTH_OPEN_MINUTE) // 5  # 78

# Midday window (12:00-14:00 ET)
MIDDAY_START_MINUTE: int = 12 * 60  # 720
MIDDAY_END_MINUTE: int = 14 * 60  # 840

# Trading days per year for annualisation
TRADING_DAYS_PER_YEAR: int = 252


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntradayBar:
    """One 5-minute bar.

    ``ts_minute`` is an integer minute-of-day in US/Eastern (e.g. 570 =
    09:30 ET, 825 = 13:45 ET). The bar is taken to *start* at
    ``ts_minute`` and cover the next ``bar_minutes`` minutes.
    """

    ts_minute: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class IntradaySessionConfig:
    """Tunable thresholds for the session classifier.

    Defaults are calibrated against SPY historical behaviour and match
    ``app.config.brain_intraday_session_*`` settings.
    """

    bar_minutes: int = 5
    or_minutes: int = 30
    power_minutes: int = 30
    midday_start_minute: int = MIDDAY_START_MINUTE
    midday_end_minute: int = MIDDAY_END_MINUTE

    min_bars: int = 40
    min_coverage_score: float = 0.5

    # Thresholds
    or_range_low: float = 0.003  # OR < 0.3% = compressed open
    or_range_high: float = 0.012  # OR > 1.2% = wide open
    midday_compression_cut: float = 0.5  # midday_range / or_range
    gap_magnitude_go: float = 0.005  # |gap| >= 0.5% qualifies as "big"
    gap_magnitude_fade: float = 0.005
    trending_close_threshold: float = 0.006  # (close-open)/open
    reversal_close_threshold: float = 0.003  # |close - or_mid| / open


@dataclass(frozen=True)
class IntradaySessionInput:
    as_of_date: date
    bars: Sequence[IntradayBar]
    prev_close: Optional[float] = None
    source_symbol: str = "SPY"
    config: IntradaySessionConfig = field(default_factory=IntradaySessionConfig)


@dataclass(frozen=True)
class IntradaySessionOutput:
    snapshot_id: str
    as_of_date: date
    source_symbol: str

    # anchors
    open_price: Optional[float]
    close_price: Optional[float]
    session_high: Optional[float]
    session_low: Optional[float]
    session_range_pct: Optional[float]

    # gap
    prev_close: Optional[float]
    gap_open: Optional[float]
    gap_open_pct: Optional[float]

    # OR
    or_high: Optional[float]
    or_low: Optional[float]
    or_range_pct: Optional[float]
    or_volume_ratio: Optional[float]

    # midday
    midday_range_pct: Optional[float]
    midday_compression_ratio: Optional[float]

    # power hour
    ph_range_pct: Optional[float]
    ph_volume_ratio: Optional[float]
    close_vs_or_mid_pct: Optional[float]

    # vol
    intraday_rv: Optional[float]

    # composite
    session_numeric: int
    session_label: str

    # coverage
    bars_observed: int
    coverage_score: float

    # echo
    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Deterministic id
# ---------------------------------------------------------------------------


def compute_snapshot_id(as_of: date) -> str:
    """Stable 16-char SHA-256 hex digest for ``intraday_session:<iso>``."""
    h = hashlib.sha256(f"intraday_session:{as_of.isoformat()}".encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def _rth_bars(bars: Sequence[IntradayBar]) -> List[IntradayBar]:
    """Filter to regular trading hours and sort by ``ts_minute`` ascending."""
    out = [b for b in bars if RTH_OPEN_MINUTE <= b.ts_minute < RTH_CLOSE_MINUTE]
    out.sort(key=lambda b: b.ts_minute)
    return out


def _safe_pct(num: Optional[float], denom: Optional[float]) -> Optional[float]:
    if num is None or denom is None:
        return None
    if not math.isfinite(num) or not math.isfinite(denom):
        return None
    if denom == 0.0:
        return None
    return num / denom


def _session_anchors(
    bars: Sequence[IntradayBar],
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return ``(open, close, high, low)`` over the filtered bars."""
    if not bars:
        return None, None, None, None
    open_ = bars[0].open
    close_ = bars[-1].close
    high_ = max(b.high for b in bars)
    low_ = min(b.low for b in bars)
    return open_, close_, high_, low_


def _slice_by_minute(
    bars: Sequence[IntradayBar], start_minute: int, end_minute: int
) -> List[IntradayBar]:
    """Return bars whose ``ts_minute`` is in ``[start, end)``."""
    return [b for b in bars if start_minute <= b.ts_minute < end_minute]


def _range(bars: Sequence[IntradayBar]) -> Optional[float]:
    """High-low range in absolute price units; ``None`` if empty."""
    if not bars:
        return None
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    return hi - lo


def _sum_volume(bars: Sequence[IntradayBar]) -> float:
    return sum(b.volume for b in bars if math.isfinite(b.volume))


def _log_returns(closes: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(closes)):
        a = closes[i - 1]
        b = closes[i]
        if a is None or b is None or a <= 0 or b <= 0:
            continue
        if not math.isfinite(a) or not math.isfinite(b):
            continue
        out.append(math.log(b / a))
    return out


def _intraday_realised_vol(bars: Sequence[IntradayBar]) -> Optional[float]:
    """Annualised realised vol from 5-min log returns.

    Annualisation factor = sqrt(252 * 78) for 5-min RTH bars.
    """
    if len(bars) < 2:
        return None
    closes = [b.close for b in bars]
    rets = _log_returns(closes)
    if len(rets) < 2:
        return None
    try:
        sd = statistics.pstdev(rets)
    except statistics.StatisticsError:
        return None
    if sd <= 0.0 or not math.isfinite(sd):
        return None
    return sd * math.sqrt(TRADING_DAYS_PER_YEAR * RTH_BARS_5MIN)


# ---------------------------------------------------------------------------
# Composite classifier
# ---------------------------------------------------------------------------


def _classify_session(
    *,
    open_price: Optional[float],
    close_price: Optional[float],
    session_range_pct: Optional[float],
    or_range_pct: Optional[float],
    or_high: Optional[float],
    or_low: Optional[float],
    midday_compression_ratio: Optional[float],
    gap_open_pct: Optional[float],
    close_vs_or_mid_pct: Optional[float],
    bars_observed: int,
    cfg: IntradaySessionConfig,
) -> Tuple[str, int]:
    """Return ``(session_label, session_numeric)``.

    Decision order mirrors the plan's top-down specification. Missing
    inputs at any level degrade gracefully to ``session_neutral``.
    """
    if bars_observed < cfg.min_bars:
        return SESSION_NEUTRAL, 0

    if (
        open_price is None
        or close_price is None
        or open_price <= 0
        or not math.isfinite(open_price)
        or not math.isfinite(close_price)
    ):
        return SESSION_NEUTRAL, 0

    close_open_rel = (close_price - open_price) / open_price

    # 1. Gap-and-go
    if (
        gap_open_pct is not None
        and abs(gap_open_pct) >= cfg.gap_magnitude_go
        and session_range_pct is not None
        and session_range_pct >= cfg.or_range_high
    ):
        # Same-direction close requires close_open_rel sign matches gap sign
        if gap_open_pct > 0 and close_open_rel > 0:
            return SESSION_GAP_AND_GO, +3
        if gap_open_pct < 0 and close_open_rel < 0:
            return SESSION_GAP_AND_GO, -3

    # 2. Gap-fade
    if (
        gap_open_pct is not None
        and abs(gap_open_pct) >= cfg.gap_magnitude_fade
        and close_vs_or_mid_pct is not None
        and abs(close_vs_or_mid_pct) >= cfg.reversal_close_threshold
    ):
        # Opposite-direction close (close went against gap sign)
        if gap_open_pct > 0 and close_open_rel < 0:
            return SESSION_GAP_FADE, -3
        if gap_open_pct < 0 and close_open_rel > 0:
            return SESSION_GAP_FADE, +3

    # 3/4. Trending
    if close_open_rel >= cfg.trending_close_threshold:
        return SESSION_TRENDING_UP, +1
    if close_open_rel <= -cfg.trending_close_threshold:
        return SESSION_TRENDING_DOWN, -1

    # 5. Reversal — tight midday followed by close displaced from OR mid
    if (
        midday_compression_ratio is not None
        and midday_compression_ratio < cfg.midday_compression_cut
        and close_vs_or_mid_pct is not None
        and abs(close_vs_or_mid_pct) >= cfg.reversal_close_threshold
    ):
        return SESSION_REVERSAL, +2

    # 6. Compressed — tight OR and tight session range
    if (
        or_range_pct is not None
        and session_range_pct is not None
        and or_range_pct < cfg.or_range_low
        and session_range_pct < cfg.or_range_high
    ):
        return SESSION_COMPRESSED, 0

    # 7. Fallback
    return SESSION_RANGE_BOUND, 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def compute_intraday_session(
    inp: IntradaySessionInput,
) -> IntradaySessionOutput:
    """Compute one intraday-session snapshot.

    The function is deterministic and side-effect free. Degenerate
    inputs (e.g. empty bar list) produce ``session_neutral`` with
    ``coverage_score=0.0`` and ``None`` scalars where appropriate.
    """
    cfg = inp.config
    sid = compute_snapshot_id(inp.as_of_date)

    rth = _rth_bars(inp.bars)
    bars_observed = len(rth)

    # Session anchors
    open_, close_, high_, low_ = _session_anchors(rth)
    session_range_pct: Optional[float] = None
    if open_ is not None and high_ is not None and low_ is not None:
        session_range_pct = _safe_pct(high_ - low_, open_)

    # Gap features
    gap_open: Optional[float] = None
    gap_open_pct: Optional[float] = None
    if (
        inp.prev_close is not None
        and open_ is not None
        and math.isfinite(inp.prev_close)
        and math.isfinite(open_)
        and inp.prev_close > 0
    ):
        gap_open = open_ - inp.prev_close
        gap_open_pct = gap_open / inp.prev_close

    # Opening range
    or_start = RTH_OPEN_MINUTE
    or_end = RTH_OPEN_MINUTE + cfg.or_minutes
    or_bars = _slice_by_minute(rth, or_start, or_end)
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    or_range_pct: Optional[float] = None
    or_mid: Optional[float] = None
    if or_bars:
        or_high = max(b.high for b in or_bars)
        or_low = min(b.low for b in or_bars)
        or_mid = (or_high + or_low) / 2.0
        or_range_pct = _safe_pct(or_high - or_low, open_)

    # OR volume ratio: total OR volume vs per-bar median for the session
    or_volume_ratio: Optional[float] = None
    if or_bars and rth:
        or_vol_mean_per_bar = _sum_volume(or_bars) / max(len(or_bars), 1)
        session_vol_mean_per_bar = _sum_volume(rth) / max(len(rth), 1)
        if session_vol_mean_per_bar > 0:
            or_volume_ratio = or_vol_mean_per_bar / session_vol_mean_per_bar

    # Midday window
    midday_bars = _slice_by_minute(
        rth, cfg.midday_start_minute, cfg.midday_end_minute
    )
    midday_range_pct: Optional[float] = None
    midday_compression_ratio: Optional[float] = None
    if midday_bars and open_ is not None:
        mid_range = _range(midday_bars)
        midday_range_pct = _safe_pct(mid_range, open_)
        if (
            or_range_pct is not None
            and or_range_pct > 0
            and midday_range_pct is not None
        ):
            midday_compression_ratio = midday_range_pct / or_range_pct

    # Power hour: last cfg.power_minutes of the session
    ph_start = RTH_CLOSE_MINUTE - cfg.power_minutes
    ph_bars = _slice_by_minute(rth, ph_start, RTH_CLOSE_MINUTE)
    ph_range_pct: Optional[float] = None
    ph_volume_ratio: Optional[float] = None
    close_vs_or_mid_pct: Optional[float] = None
    if ph_bars and open_ is not None:
        ph_range = _range(ph_bars)
        ph_range_pct = _safe_pct(ph_range, open_)
        if rth:
            ph_vol_mean = _sum_volume(ph_bars) / max(len(ph_bars), 1)
            session_vol_mean = _sum_volume(rth) / max(len(rth), 1)
            if session_vol_mean > 0:
                ph_volume_ratio = ph_vol_mean / session_vol_mean
    if close_ is not None and or_mid is not None and open_ is not None and open_ > 0:
        close_vs_or_mid_pct = (close_ - or_mid) / open_

    # Intraday realised vol (annualised)
    intraday_rv = _intraday_realised_vol(rth)

    # Coverage
    coverage_score = 0.0
    if RTH_BARS_5MIN > 0:
        coverage_score = min(1.0, bars_observed / float(RTH_BARS_5MIN))

    # Composite
    session_label, session_numeric = _classify_session(
        open_price=open_,
        close_price=close_,
        session_range_pct=session_range_pct,
        or_range_pct=or_range_pct,
        or_high=or_high,
        or_low=or_low,
        midday_compression_ratio=midday_compression_ratio,
        gap_open_pct=gap_open_pct,
        close_vs_or_mid_pct=close_vs_or_mid_pct,
        bars_observed=bars_observed,
        cfg=cfg,
    )

    # Echo payload (config + derived)
    payload: dict[str, Any] = {
        "config": {
            "bar_minutes": cfg.bar_minutes,
            "or_minutes": cfg.or_minutes,
            "power_minutes": cfg.power_minutes,
            "midday_start_minute": cfg.midday_start_minute,
            "midday_end_minute": cfg.midday_end_minute,
            "min_bars": cfg.min_bars,
            "min_coverage_score": cfg.min_coverage_score,
            "or_range_low": cfg.or_range_low,
            "or_range_high": cfg.or_range_high,
            "midday_compression_cut": cfg.midday_compression_cut,
            "gap_magnitude_go": cfg.gap_magnitude_go,
            "gap_magnitude_fade": cfg.gap_magnitude_fade,
            "trending_close_threshold": cfg.trending_close_threshold,
            "reversal_close_threshold": cfg.reversal_close_threshold,
        },
        "source_symbol": inp.source_symbol,
        "rth_bars_5min": RTH_BARS_5MIN,
        "or_bars_observed": len(or_bars),
        "midday_bars_observed": len(midday_bars),
        "ph_bars_observed": len(ph_bars),
        "or_mid": or_mid,
    }

    return IntradaySessionOutput(
        snapshot_id=sid,
        as_of_date=inp.as_of_date,
        source_symbol=inp.source_symbol,
        open_price=open_,
        close_price=close_,
        session_high=high_,
        session_low=low_,
        session_range_pct=session_range_pct,
        prev_close=inp.prev_close,
        gap_open=gap_open,
        gap_open_pct=gap_open_pct,
        or_high=or_high,
        or_low=or_low,
        or_range_pct=or_range_pct,
        or_volume_ratio=or_volume_ratio,
        midday_range_pct=midday_range_pct,
        midday_compression_ratio=midday_compression_ratio,
        ph_range_pct=ph_range_pct,
        ph_volume_ratio=ph_volume_ratio,
        close_vs_or_mid_pct=close_vs_or_mid_pct,
        intraday_rv=intraday_rv,
        session_numeric=session_numeric,
        session_label=session_label,
        bars_observed=bars_observed,
        coverage_score=coverage_score,
        payload=payload,
    )


__all__ = [
    "SESSION_TRENDING_UP",
    "SESSION_TRENDING_DOWN",
    "SESSION_RANGE_BOUND",
    "SESSION_REVERSAL",
    "SESSION_GAP_AND_GO",
    "SESSION_GAP_FADE",
    "SESSION_COMPRESSED",
    "SESSION_NEUTRAL",
    "VALID_SESSION_LABELS",
    "RTH_OPEN_MINUTE",
    "RTH_CLOSE_MINUTE",
    "RTH_BARS_5MIN",
    "IntradayBar",
    "IntradaySessionConfig",
    "IntradaySessionInput",
    "IntradaySessionOutput",
    "compute_snapshot_id",
    "compute_intraday_session",
]
