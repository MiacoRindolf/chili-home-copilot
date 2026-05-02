"""Calibrated value lookups against fast_signal_decay (F6.5).

Helpers used by exit_manager / gates / stop_engine to read empirical
forward-return statistics instead of hardcoded magic numbers. Every
helper returns ``None`` when there's insufficient data, so each
caller can fall back to the existing constant -- F6 is NOT a hard
dependency.

Conventions:
  - "Sufficient data" means ``sample_count >= MIN_SAMPLES_FOR_CALIB``
    on the bucket row in question. Below that, the running statistics
    are too noisy to trust.
  - Sharpe-like ranking uses ``mean_return / stdev`` where stdev is
    derived from Welford's M2 column: ``stdev = sqrt(m2 / (n-1))``.
  - All fractions: ``mean_return`` and ``stdev`` are unitless return
    fractions (e.g., 0.001 = 10 bps).
  - Trading cost: ~100 bps round-trip (Coinbase ~40 bps taker × 2 +
    spread). Configurable via env so it can be tightened once we have
    a tighter measurement.

Caching: each helper opens a short-lived SQLAlchemy connection per
call. The brief said reads should be zero-cost in the hot path; the
ix_fsd_lookup composite index makes each query a single seek. If
this becomes a hot spot we can layer an in-process TTL cache, but
exit_manager bootstraps once per position (~hourly) and the gate
runs once per alert (~10/min) so the round-trip cost is irrelevant
today.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .decay_miner import HORIZONS_S, score_bucket

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

# Below this sample count a bucket is too thin to trust; caller falls
# back to the cold-start constant. 30 is a defensible "law of large
# numbers" floor that the brief suggested.
MIN_SAMPLES_FOR_CALIB = int(
    os.environ.get("CHILI_FAST_PATH_DECAY_MIN_SAMPLES", "30")
)

# Round-trip trading cost as a fraction. Used by ``is_score_tradeable``.
# Coinbase Advanced Trade taker fee is ~0.4% (4 bps wait, 40 bps);
# round-trip is ~80 bps, plus a 5-10 bps spread eats another 5 bps,
# so 100 bps is a reasonable headline. Override via env if a tighter
# number is justified.
TRADING_COST_FRAC = float(
    os.environ.get("CHILI_FAST_PATH_TRADING_COST_FRAC", "0.01")
)

# A score bucket is "tradeable" if the empirical mean return at its
# best (highest-Sharpe) horizon beats this multiple of trading cost.
# 2× is a defensible margin: any signal that just barely covers cost
# isn't worth the variance.
TRADEABLE_COST_MULT = float(
    os.environ.get("CHILI_FAST_PATH_TRADEABLE_COST_MULT", "2.0")
)

# F6.5: hardware/network reality, not a strategy choice. Coinbase live
# placement round-trip (place + broker confirm + exit + broker confirm)
# is ~200-500ms typical. A calibrated max_hold_s shorter than this is
# empirically saying "this signal isn't tradeable at our latency
# profile" -- the cleaner expression is "we don't try to hold for less
# than the floor; if calibration argues for that, fall through to the
# floor and let the position prove or disprove its edge over a
# survivable horizon." Override via env if hardware reality changes.
CALIB_EXEC_FLOOR_S = int(
    os.environ.get("CHILI_FAST_PATH_CALIB_EXEC_FLOOR_S", "10")
)

# F6.5: dedup tracker for the floor-substitution INFO log. Logs once
# per (ticker, alert_type, score_bucket) per process so we can see
# the floor operating without spamming. Cleared on process restart.
_FLOOR_LOG_SEEN: set[tuple[str, str, str]] = set()


# ── Internal: row -> stats helpers ───────────────────────────────────


def _stdev(m2: float, n: int) -> float:
    """Welford stdev = sqrt(M2 / (n-1)). Returns 0 for n<=1."""
    if n is None or n <= 1:
        return 0.0
    return math.sqrt(max(0.0, float(m2) / float(n - 1)))


def _sharpe_like(mean_return: float, stdev: float) -> float | None:
    """Mean / stdev ratio; None if stdev is zero (degenerate)."""
    if stdev <= 0.0:
        return None
    return float(mean_return) / float(stdev)


def _fetch_bucket_rows(
    engine: Engine, *, ticker: str, alert_type: str, bucket: str,
) -> list[dict[str, Any]]:
    """All horizon rows for one (ticker, alert_type, bucket) tuple."""
    sql = text("""
        SELECT horizon_s, sample_count, mean_return, m2_return,
               realized_validation_count, realized_validation_residual
        FROM fast_signal_decay
        WHERE ticker = :t
          AND alert_type = :at
          AND score_bucket = :sb
        ORDER BY horizon_s ASC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "t": ticker, "at": alert_type, "sb": bucket,
        }).mappings().all()
    return [dict(r) for r in rows]


def _best_sharpe_row(
    rows: list[dict[str, Any]], *, min_samples: int = MIN_SAMPLES_FOR_CALIB,
) -> dict[str, Any] | None:
    """Highest mean/stdev row above ``min_samples``. None if none qualify.

    Sharpe is computed against absolute mean (we prefer signals with
    a non-zero edge in EITHER direction; gates handle direction-of-
    profit via their long/short logic). Ties broken by sample_count
    (more data wins).
    """
    best: dict[str, Any] | None = None
    best_score = -1.0
    for row in rows:
        n = int(row["sample_count"] or 0)
        if n < min_samples:
            continue
        mean = float(row["mean_return"] or 0.0)
        stdev = _stdev(row["m2_return"], n)
        if stdev <= 0.0:
            continue
        score = abs(mean) / stdev
        if score > best_score or (
            score == best_score and best is not None
            and n > int(best["sample_count"] or 0)
        ):
            best = row
            best_score = score
    return best


# ── Public helpers ───────────────────────────────────────────────────


def get_calibrated_max_hold_s(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
) -> int | None:
    """Calibrated max-hold time for one signal in seconds.

    Picks the highest-Sharpe horizon (above ``MIN_SAMPLES_FOR_CALIB``)
    as the empirical "where this signal stops being predictive."
    Holding longer than that is uncalibrated speculation.

    Returns None when the bucket has no qualifying horizon -- caller
    falls back to ``MAX_HOLD_S_DEFAULT`` (the current constant).
    """
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    raw_horizon = int(best["horizon_s"])
    if raw_horizon < CALIB_EXEC_FLOOR_S:
        # F6.5: latency floor. Don't schedule holds shorter than
        # round-trip placement latency -- the position can't actually
        # close in that window. Return the floor instead; let the
        # position survive long enough to prove or disprove its edge.
        key = (ticker, alert_type, bucket)
        if key not in _FLOOR_LOG_SEEN:
            _FLOOR_LOG_SEEN.add(key)
            logger.info(
                "[fast_path] calibration max_hold_s floored ticker=%s "
                "alert_type=%s bucket=%s calibrated=%ds floor=%ds "
                "(signal predictive horizon below execution latency)",
                ticker, alert_type, bucket, raw_horizon, CALIB_EXEC_FLOOR_S,
            )
        return CALIB_EXEC_FLOOR_S
    return raw_horizon


def is_score_tradeable(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
) -> bool | None:
    """Does this signal beat ``TRADEABLE_COST_MULT × TRADING_COST_FRAC``
    on its best horizon?

    Returns:
      - True  : mean > threshold AND has enough samples (calibrated tradeable)
      - False : has enough samples AND mean is at-or-below threshold
                (calibrated NOT tradeable -- don't trade this signal)
      - None  : insufficient data; caller falls back to MIN_SIGNAL_SCORE

    The threshold is direction-aware via ``alert_type`` suffix: long
    signals need positive mean above threshold; short signals need
    positive mean (since the table records the direction-flipped
    forward return).
    """
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    threshold = TRADEABLE_COST_MULT * TRADING_COST_FRAC
    return float(best["mean_return"] or 0.0) > threshold


def compute_calibrated_bracket(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
    entry: float,
    direction: str = "long",
) -> tuple[float, float] | None:
    """(stop_price, target_price) sized by the empirical stdev at
    the highest-Sharpe horizon.

    Stop  = entry - 2 × stdev × entry  (long)  /  entry + 2 × stdev × entry  (short)
    Target= entry + 3 × stdev × entry  (long)  /  entry - 3 × stdev × entry  (short)

    The 2:3 R-multiple matches stop_engine's swing-side convention.
    Returns None when insufficient data -- caller falls back to
    ``stop_engine.compute_initial_bracket`` (ATR-based).
    """
    if entry <= 0.0:
        return None
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    n = int(best["sample_count"] or 0)
    stdev = _stdev(best["m2_return"], n)
    if stdev <= 0.0:
        return None
    if (direction or "long").lower() == "short":
        stop = entry * (1.0 + 2.0 * stdev)
        target = entry * (1.0 - 3.0 * stdev)
    else:
        stop = entry * (1.0 - 2.0 * stdev)
        target = entry * (1.0 + 3.0 * stdev)
    if stop <= 0.0 or target <= 0.0:
        return None
    return float(stop), float(target)


__all__ = [
    "get_calibrated_max_hold_s",
    "is_score_tradeable",
    "compute_calibrated_bracket",
    "MIN_SAMPLES_FOR_CALIB",
    "TRADING_COST_FRAC",
    "TRADEABLE_COST_MULT",
    "CALIB_EXEC_FLOOR_S",
]
