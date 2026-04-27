"""Feature compute for the perps lane.

Per-symbol features:
  - basis_bps          : (perp - spot) / spot * 10000
  - basis_z_score      : current basis z-scored over 30d trailing
  - funding_8h_rate    : current period funding (e.g. 0.0001 = 0.01%)
  - funding_annualized : funding_8h_rate * (3 funding events/day) * 365
  - oi_pct_change_24h  : (current_oi - oi_24h_ago) / oi_24h_ago
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def basis_bps(perp_price: float, spot_price: float) -> Optional[float]:
    if spot_price <= 0 or perp_price <= 0:
        return None
    return (perp_price - spot_price) / spot_price * 10_000.0


def basis_z_score(db: Session, symbol: str, current_basis_bps: float) -> Optional[float]:
    """Z-score current basis vs trailing 30d window."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        rows = db.execute(
            text(
                "SELECT basis_bps FROM perp_basis "
                "WHERE symbol = :s AND ts >= :c "
                "ORDER BY ts DESC LIMIT 720"  # ~30 days at hourly
            ),
            {"s": symbol.upper(), "c": cutoff},
        ).fetchall()
    except Exception as e:
        logger.debug("[perps.features] basis_z_score query failed: %s", e)
        return None
    values = [float(r[0]) for r in rows or [] if r[0] is not None]
    if len(values) < 30:
        return None
    try:
        mu = mean(values)
        sd = stdev(values) if len(values) > 1 else 0.0
        if sd <= 1e-9:
            return 0.0
        return (current_basis_bps - mu) / sd
    except Exception:
        return None


def funding_annualized(funding_8h_rate: float, intervals_per_day: int = 3) -> float:
    """Convert per-period funding rate to annualized.

    e.g. 0.0001 (0.01% per 8h) * 3 * 365 = 0.1095 = 10.95% annualized.
    """
    return funding_8h_rate * intervals_per_day * 365


def oi_pct_change_24h(db: Session, symbol: str, venue: str = "binance") -> Optional[float]:
    """Percent change in OI over the last 24 hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        # Recent (last hour)
        recent = db.execute(
            text(
                "SELECT open_interest_usd FROM perp_oi "
                "WHERE symbol = :s AND venue = :v "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"s": symbol.upper(), "v": venue},
        ).fetchone()
        # 24h-ago (closest to cutoff)
        prior = db.execute(
            text(
                "SELECT open_interest_usd FROM perp_oi "
                "WHERE symbol = :s AND venue = :v AND ts <= :c "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"s": symbol.upper(), "v": venue, "c": cutoff},
        ).fetchone()
    except Exception as e:
        logger.debug("[perps.features] oi 24h change query failed: %s", e)
        return None
    if not recent or not prior or recent[0] is None or prior[0] is None:
        return None
    if float(prior[0]) == 0:
        return None
    return (float(recent[0]) - float(prior[0])) / float(prior[0])
