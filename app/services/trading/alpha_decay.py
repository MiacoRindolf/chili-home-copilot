"""Alpha decay monitoring for live/promoted patterns.

Tracks rolling performance of patterns that are generating signals.
Auto-demotes patterns whose win-rate or return has decayed below
their historical OOS benchmarks.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, ScanPattern, Trade

logger = logging.getLogger(__name__)

DEFAULT_ROLLING_WINDOW_DAYS = 30
MIN_TRADES_FOR_DECAY_CHECK = 5
DECAY_WR_GAP_PCT = 12.0       # demote if live WR is >12pp below OOS WR
DECAY_RETURN_FLOOR_PCT = -1.0  # demote if rolling avg return < -1%


def check_alpha_decay(
    db: Session,
    user_id: int | None = None,
    *,
    window_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
    wr_gap: float = DECAY_WR_GAP_PCT,
    return_floor: float = DECAY_RETURN_FLOOR_PCT,
    auto_demote: bool = True,
) -> dict[str, Any]:
    """Check all live/promoted patterns for alpha decay.

    Returns summary with list of decayed pattern ids.
    """
    from .lifecycle import transition_on_decay

    cutoff = datetime.utcnow() - timedelta(days=window_days)

    live_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("live", "promoted")),
        )
        .all()
    )
    if not live_patterns:
        return {"ok": True, "checked": 0, "decayed": []}

    sp_ids = [p.id for p in live_patterns]

    recent_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.scan_pattern_id.in_(sp_ids),
            Trade.exit_date >= cutoff,
        )
        .all()
    )

    trades_by_sp: dict[int, list[Trade]] = {}
    for t in recent_trades:
        trades_by_sp.setdefault(t.scan_pattern_id, []).append(t)

    decayed: list[dict[str, Any]] = []
    healthy: list[int] = []

    for pat in live_patterns:
        trades = trades_by_sp.get(pat.id, [])
        if len(trades) < MIN_TRADES_FOR_DECAY_CHECK:
            continue

        live_wins = sum(1 for t in trades if (t.pnl or 0) > 0)
        live_wr = live_wins / len(trades) * 100
        live_avg_ret = sum(t.pnl or 0 for t in trades) / len(trades)

        oos_wr = pat.oos_win_rate or pat.win_rate or 50.0

        is_decayed = False
        reason_parts = []

        if live_wr < oos_wr - wr_gap:
            is_decayed = True
            reason_parts.append(f"WR decay: live {live_wr:.1f}% vs OOS {oos_wr:.1f}%")

        if live_avg_ret < return_floor:
            is_decayed = True
            reason_parts.append(f"Avg return {live_avg_ret:.2f} < floor {return_floor}")

        if is_decayed:
            reason = "; ".join(reason_parts)
            decayed.append({
                "pattern_id": pat.id,
                "pattern_name": pat.name,
                "live_wr": round(live_wr, 1),
                "oos_wr": round(oos_wr, 1),
                "live_avg_return": round(live_avg_ret, 2),
                "trades": len(trades),
                "reason": reason,
            })
            if auto_demote:
                try:
                    transition_on_decay(db, pat, reason=reason)
                except Exception as e:
                    logger.warning("[alpha_decay] Failed to demote %s: %s", pat.name, e)
        else:
            healthy.append(pat.id)

    if decayed and auto_demote:
        db.commit()

    logger.info(
        "[alpha_decay] Checked %d patterns: %d healthy, %d decayed",
        len(live_patterns), len(healthy), len(decayed),
    )

    return {
        "ok": True,
        "checked": len(live_patterns),
        "healthy": len(healthy),
        "decayed": decayed,
    }


def estimate_half_life(
    db: Session,
    pattern_id: int,
    user_id: int | None = None,
) -> float | None:
    """Estimate the half-life of a pattern's alpha (in days).

    Uses exponential decay fit on rolling win-rate over time.
    Returns None if insufficient data.
    """
    trades = (
        db.query(Trade)
        .filter(
            Trade.scan_pattern_id == pattern_id,
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
        )
        .order_by(Trade.exit_date.asc())
        .all()
    )
    if len(trades) < 10:
        return None

    window = 5
    wr_points: list[tuple[float, float]] = []
    first_date = trades[0].exit_date

    for i in range(window, len(trades)):
        chunk = trades[i - window:i]
        wins = sum(1 for t in chunk if (t.pnl or 0) > 0)
        wr = wins / window
        days_elapsed = (chunk[-1].exit_date - first_date).total_seconds() / 86400
        if wr > 0:
            wr_points.append((days_elapsed, wr))

    if len(wr_points) < 3:
        return None

    # Simple log-linear regression: ln(wr) = a + b*t  =>  half_life = -ln(2)/b
    n = len(wr_points)
    sum_t = sum(p[0] for p in wr_points)
    sum_lnwr = sum(math.log(p[1]) for p in wr_points)
    sum_t2 = sum(p[0] ** 2 for p in wr_points)
    sum_t_lnwr = sum(p[0] * math.log(p[1]) for p in wr_points)

    denom = n * sum_t2 - sum_t ** 2
    if abs(denom) < 1e-12:
        return None

    b = (n * sum_t_lnwr - sum_t * sum_lnwr) / denom

    if b >= 0:
        return None  # no decay detected

    half_life = -math.log(2) / b
    return round(half_life, 1)
