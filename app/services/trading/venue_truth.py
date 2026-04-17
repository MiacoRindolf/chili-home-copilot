"""Venue-truth telemetry (Phase F, shadow-safe DB writer).

Compares expected vs realized execution costs on every fill / close event
and emits a ``[venue_truth_ops]`` one-liner. Results land in
``trading_venue_truth_log`` and feed the ``/brain/venue-truth/diagnostics``
endpoint.

Mode gate: ``settings.brain_venue_truth_mode`` in {off, shadow, authoritative}.
In this phase we only ever ship ``shadow``; release-blocker script fires
if anyone sneaks in ``authoritative`` logs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.venue_truth_ops_log import (
    format_venue_truth_ops_line,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "authoritative")


# ── Mode helpers ───────────────────────────────────────────────────────

def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_venue_truth_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_venue_truth_ops_log_enabled", True))


# ── Dataclass ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FillObservation:
    ticker: str
    side: str
    notional_usd: float
    expected_spread_bps: float | None = None
    realized_spread_bps: float | None = None
    expected_slippage_bps: float | None = None
    realized_slippage_bps: float | None = None
    expected_cost_fraction: float | None = None
    realized_cost_fraction: float | None = None
    trade_id: int | None = None
    paper_bool: bool = True


def _cost_gap_bps(obs: FillObservation) -> float | None:
    """Signed gap (realized - expected) in bps of notional.

    Positive = we paid more than expected. Computed from cost fractions
    when available so it is directly comparable to the impact cap.
    """
    ef = obs.expected_cost_fraction
    rf = obs.realized_cost_fraction
    if ef is None and rf is None:
        return None
    ef = ef or 0.0
    rf = rf or 0.0
    return (rf - ef) * 10_000.0


# ── Write API ──────────────────────────────────────────────────────────

def record_fill_observation(
    db: Session,
    obs: FillObservation,
    *,
    mode_override: str | None = None,
) -> bool:
    """Write one row to ``trading_venue_truth_log`` + emit an ops line.

    Returns True when a row was written, False when mode is ``off``.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return False

    sql = text("""
        INSERT INTO trading_venue_truth_log (
            trade_id, ticker, side, notional_usd,
            expected_spread_bps, realized_spread_bps,
            expected_slippage_bps, realized_slippage_bps,
            expected_cost_fraction, realized_cost_fraction,
            paper_bool, mode, created_at
        )
        VALUES (
            :trade_id, :ticker, :side, :notional_usd,
            :expected_spread_bps, :realized_spread_bps,
            :expected_slippage_bps, :realized_slippage_bps,
            :expected_cost_fraction, :realized_cost_fraction,
            :paper_bool, :mode, NOW()
        )
    """)

    db.execute(sql, {
        "trade_id": obs.trade_id,
        "ticker": obs.ticker,
        "side": obs.side,
        "notional_usd": float(obs.notional_usd),
        "expected_spread_bps": obs.expected_spread_bps,
        "realized_spread_bps": obs.realized_spread_bps,
        "expected_slippage_bps": obs.expected_slippage_bps,
        "realized_slippage_bps": obs.realized_slippage_bps,
        "expected_cost_fraction": obs.expected_cost_fraction,
        "realized_cost_fraction": obs.realized_cost_fraction,
        "paper_bool": bool(obs.paper_bool),
        "mode": mode,
    })
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_venue_truth_ops_line(
                event="fill_observation",
                mode=mode,
                ticker=obs.ticker,
                side=obs.side,
                trade_id=obs.trade_id,
                notional_usd=obs.notional_usd,
                expected_spread_bps=obs.expected_spread_bps,
                realized_spread_bps=obs.realized_spread_bps,
                expected_slippage_bps=obs.expected_slippage_bps,
                realized_slippage_bps=obs.realized_slippage_bps,
                expected_cost_fraction=obs.expected_cost_fraction,
                realized_cost_fraction=obs.realized_cost_fraction,
                cost_gap_bps=_cost_gap_bps(obs),
                paper_bool=obs.paper_bool,
            )
        )
    return True


# ── Diagnostics ────────────────────────────────────────────────────────

def venue_truth_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
    top_n: int = 10,
) -> dict[str, Any]:
    """Summary for ``/brain/venue-truth/diagnostics``.

    Shape (frozen):
      * mode, lookback_hours, observations_total
      * mean_expected_cost_fraction, mean_realized_cost_fraction
      * mean_gap_bps, p90_gap_bps
      * worst_tickers [{ticker, mean_gap_bps, observations}]
    """
    mode = _effective_mode()
    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))

    totals_row = db.execute(text("""
        SELECT
            COUNT(*) AS n,
            AVG(expected_cost_fraction) AS mean_exp,
            AVG(realized_cost_fraction) AS mean_real
        FROM trading_venue_truth_log
        WHERE created_at >= :cutoff
    """), {"cutoff": cutoff}).fetchone()

    observations_total = int(totals_row[0]) if totals_row and totals_row[0] else 0
    mean_expected = float(totals_row[1]) if totals_row and totals_row[1] is not None else None
    mean_realized = float(totals_row[2]) if totals_row and totals_row[2] is not None else None

    gap_rows = db.execute(text("""
        SELECT
            (COALESCE(realized_cost_fraction, 0) - COALESCE(expected_cost_fraction, 0)) * 10000 AS gap_bps
        FROM trading_venue_truth_log
        WHERE created_at >= :cutoff
          AND (expected_cost_fraction IS NOT NULL OR realized_cost_fraction IS NOT NULL)
        ORDER BY created_at
    """), {"cutoff": cutoff}).fetchall()

    gaps = [float(r[0]) for r in gap_rows if r[0] is not None]
    if gaps:
        mean_gap = sum(gaps) / len(gaps)
        sorted_g = sorted(gaps)
        idx90 = min(int(len(sorted_g) * 0.9), len(sorted_g) - 1)
        p90_gap = sorted_g[idx90]
    else:
        mean_gap = None
        p90_gap = None

    worst_rows = db.execute(text("""
        SELECT
            ticker,
            AVG((COALESCE(realized_cost_fraction, 0) - COALESCE(expected_cost_fraction, 0)) * 10000) AS mean_gap_bps,
            COUNT(*) AS observations
        FROM trading_venue_truth_log
        WHERE created_at >= :cutoff
          AND (expected_cost_fraction IS NOT NULL OR realized_cost_fraction IS NOT NULL)
        GROUP BY ticker
        ORDER BY mean_gap_bps DESC NULLS LAST
        LIMIT :lim
    """), {"cutoff": cutoff, "lim": int(top_n)}).fetchall()

    worst_tickers = [
        {
            "ticker": r[0],
            "mean_gap_bps": float(r[1]) if r[1] is not None else None,
            "observations": int(r[2]),
        }
        for r in worst_rows
    ]

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "observations_total": observations_total,
        "mean_expected_cost_fraction": mean_expected,
        "mean_realized_cost_fraction": mean_realized,
        "mean_gap_bps": mean_gap,
        "p90_gap_bps": p90_gap,
        "worst_tickers": worst_tickers,
    }


__all__ = [
    "FillObservation",
    "mode_is_active",
    "record_fill_observation",
    "venue_truth_summary",
]
