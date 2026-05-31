"""Canonical-aware evidence correction for ScanPattern win_rate /
avg_return_pct / trade_count, computed from closed live rows using
canonical time-decay semantics.

Used by ``learning.update_pattern_stats_from_closed_trades`` (the
load-bearing writer of the realized-EV gate's input fields) every
learning cycle. Pure functions; no DB writes, no scheduler dependency.
The caller is responsible for persisting outputs and writing audit rows.

Pre-fix (legacy aggregation in learning.py:4798-4892) wrote
``pattern.win_rate`` and ``pattern.avg_return_pct`` directly from
realized exit prices. No counterfactual correction; positions
held past their pattern's intended ``max_bars`` (= 81% of patterns per
the f-time-decay-unit-fix survey) leaked their too-late exit prices
into evidence. Post-fix (this module), the writer computes a
counterfactual close price at the bar that ``max_bars`` after entry
maps to and uses that for overheld trades.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeCorrection:
    """Per-trade canonical-aware correction outcome."""

    realized_return_pct: float
    corrected_return_pct: float
    overheld: bool
    counterfactual_available: bool
    realized_won: bool
    corrected_won: bool


@dataclass(frozen=True)
class PatternStats:
    """Aggregate stats over a pattern's closed trades, post-correction."""

    n: int
    win_rate: float        # in [0, 1]
    avg_return_pct: float  # mean trade return in percent
    overheld_n: int
    counterfactual_applied_n: int
    counterfactual_unavailable_n: int


def compute_trade_correction(
    *,
    entry_price: float,
    exit_price: float,
    entry_date: datetime,
    close_date: datetime,
    direction: str,
    ticker: str,
    pattern_timeframe: str,
    max_bars: int,
) -> TradeCorrection:
    """Per-trade canonical-aware correction. Pure; no DB.

    Returns the realized return either pre-corrected (when the trade was
    NOT held past ``max_bars`` -- legacy was already correct) or
    counterfactually corrected (when the trade WAS held past
    ``max_bars`` and OHLCV at the timeframe is available). When the
    trade was overheld but the counterfactual price is unavailable,
    falls back to realized to avoid biasing the sample by dropping
    these trades; the caller increments
    ``counterfactual_unavailable_count`` so the coverage gap is
    measurable.
    """
    from .timeframe_utils import timeframe_to_seconds

    sign = 1.0 if direction == "long" else -1.0
    realized_pct = (
        sign * (float(exit_price) - float(entry_price))
        / float(entry_price) * 100.0
    )
    realized_won = realized_pct > 0

    tf_seconds = timeframe_to_seconds(pattern_timeframe)
    held_seconds = (close_date - entry_date).total_seconds()
    held_bars = held_seconds / tf_seconds

    if held_bars <= max_bars:
        return TradeCorrection(
            realized_return_pct=realized_pct,
            corrected_return_pct=realized_pct,
            overheld=False,
            counterfactual_available=True,
            realized_won=realized_won,
            corrected_won=realized_won,
        )

    cf_price = _fetch_counterfactual_close(
        ticker, entry_date, pattern_timeframe, max_bars,
    )
    if cf_price is None:
        return TradeCorrection(
            realized_return_pct=realized_pct,
            corrected_return_pct=realized_pct,
            overheld=True,
            counterfactual_available=False,
            realized_won=realized_won,
            corrected_won=realized_won,
        )

    cf_pct = sign * (cf_price - float(entry_price)) / float(entry_price) * 100.0
    return TradeCorrection(
        realized_return_pct=realized_pct,
        corrected_return_pct=cf_pct,
        overheld=True,
        counterfactual_available=True,
        realized_won=realized_won,
        corrected_won=cf_pct > 0,
    )


def aggregate_pattern_stats(corrections: list[TradeCorrection]) -> PatternStats:
    """Aggregate per-trade corrections into the three ScanPattern fields."""
    n = len(corrections)
    if n == 0:
        return PatternStats(
            n=0, win_rate=0.0, avg_return_pct=0.0,
            overheld_n=0, counterfactual_applied_n=0,
            counterfactual_unavailable_n=0,
        )

    wins = sum(1 for c in corrections if c.corrected_won)
    win_rate = wins / n
    avg_return_pct = sum(c.corrected_return_pct for c in corrections) / n
    overheld_n = sum(1 for c in corrections if c.overheld)
    cf_applied = sum(
        1 for c in corrections if c.overheld and c.counterfactual_available
    )
    cf_unavail = sum(
        1 for c in corrections
        if c.overheld and not c.counterfactual_available
    )

    return PatternStats(
        n=n, win_rate=win_rate, avg_return_pct=avg_return_pct,
        overheld_n=overheld_n,
        counterfactual_applied_n=cf_applied,
        counterfactual_unavailable_n=cf_unavail,
    )


def _fetch_counterfactual_close(
    ticker: str,
    entry_date: datetime,
    pattern_timeframe: str,
    max_bars: int,
) -> float | None:
    """Fetch OHLCV at ``pattern_timeframe`` and return the close price of
    the bar at index ``max_bars`` from ``entry_date``.

    Returns ``None`` when:
      * OHLCV fetch fails (provider outage / circuit breaker).
      * The dataframe is empty.
      * The target timestamp falls outside the dataframe's range.

    The caller treats ``None`` as "counterfactual unavailable" and
    falls back to realized (sample-completeness over sample-purity).
    """
    from .market_data import fetch_ohlcv_df
    from .timeframe_utils import timeframe_to_seconds

    tf_seconds = timeframe_to_seconds(pattern_timeframe)
    period = _period_for_timeframe(pattern_timeframe, max_bars)

    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=pattern_timeframe)
    except Exception as e:
        logger.debug(
            "[evidence_correction] OHLCV fetch failed for %s @ %s: %s",
            ticker, pattern_timeframe, e,
        )
        return None

    if df is None or df.empty:
        return None

    target_ts = entry_date + timedelta(seconds=max_bars * tf_seconds)
    try:
        idx = df.index.searchsorted(target_ts, side="right") - 1
    except Exception:
        return None
    if idx < 0 or idx >= len(df):
        return None
    try:
        return float(df.iloc[idx]["Close"])
    except Exception:
        return None


def _period_for_timeframe(tf: str, max_bars: int) -> str:
    """Provider-friendly period string wide enough to include
    ``max_bars`` bars from the entry date. Errs on the side of "wider"
    so the bar at index ``max_bars`` is included; ``fetch_ohlcv_df``
    handles the per-provider fallback.
    """
    if tf in ("1m", "5m", "15m", "30m"):
        return "30d"
    if tf in ("1h", "2h", "4h"):
        return "1y"
    return "max"
