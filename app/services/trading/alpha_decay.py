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

from ...models.trading import BacktestResult, PaperTrade, ScanPattern, Trade

logger = logging.getLogger(__name__)

DEFAULT_ROLLING_WINDOW_DAYS = 30
MIN_TRADES_FOR_DECAY_CHECK = 5
DECAY_WR_GAP = 0.12            # demote if live WR is >12pp below OOS WR
DECAY_RETURN_FLOOR_PCT = -1.0  # demote if rolling avg return < -1%

REGIME_DECAY_ADJUSTMENTS = {
    "risk_off": {"wr_gap": 0.08, "return_floor": -0.5, "window_days": 15},
    "risk_on": {"wr_gap": 0.15, "return_floor": -1.5, "window_days": 45},
    "cautious": {"wr_gap": 0.12, "return_floor": -1.0, "window_days": 30},
}


def check_alpha_decay(
    db: Session,
    user_id: int | None = None,
    *,
    window_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
    wr_gap: float = DECAY_WR_GAP,
    return_floor: float = DECAY_RETURN_FLOOR_PCT,
    auto_demote: bool = True,
    regime_adaptive: bool = True,
) -> dict[str, Any]:
    """Check all live/promoted patterns for alpha decay.

    Combines evidence from both Trade and PaperTrade rows. Adjusts
    decay thresholds based on current market regime when regime_adaptive=True.
    """
    from .lifecycle import transition_on_decay

    if regime_adaptive:
        try:
            from .market_data import get_market_regime
            regime = get_market_regime()
            composite = regime.get("regime", "cautious")
            adj = REGIME_DECAY_ADJUSTMENTS.get(composite, {})
            window_days = adj.get("window_days", window_days)
            wr_gap = adj.get("wr_gap", wr_gap)
            return_floor = adj.get("return_floor", return_floor)
        except Exception:
            pass

    cutoff = datetime.utcnow() - timedelta(days=window_days)

    pattern_q = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        ScanPattern.lifecycle_stage.in_(("live", "promoted")),
    )
    if user_id:
        pattern_q = pattern_q.filter(ScanPattern.user_id == user_id)
    live_patterns = pattern_q.all()
    if not live_patterns:
        return {"ok": True, "checked": 0, "decayed": []}

    sp_ids = [p.id for p in live_patterns]

    trade_q = db.query(Trade).filter(
        Trade.status == "closed",
        Trade.scan_pattern_id.in_(sp_ids),
        Trade.exit_date >= cutoff,
    )
    if user_id:
        trade_q = trade_q.filter(Trade.user_id == user_id)
    recent_trades = trade_q.all()

    paper_q = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        PaperTrade.scan_pattern_id.in_(sp_ids),
        PaperTrade.exit_date >= cutoff,
    )
    if user_id:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    recent_paper = paper_q.all()

    evidence_by_sp: dict[int, list[dict]] = {}
    for t in recent_trades:
        # Compute pnl_pct from entry/exit since Trade has no pnl_pct column
        pnl_pct = 0.0
        if t.entry_price and t.entry_price > 0 and t.exit_price:
            pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
        evidence_by_sp.setdefault(t.scan_pattern_id, []).append(
            {"pnl": t.pnl or 0, "pnl_pct": pnl_pct, "source": "live"}
        )
    for pt in recent_paper:
        pnl_pct = 0.0
        if pt.entry_price and pt.entry_price > 0 and pt.exit_price:
            pnl_pct = ((pt.exit_price - pt.entry_price) / pt.entry_price) * 100
        evidence_by_sp.setdefault(pt.scan_pattern_id, []).append(
            {"pnl": pt.pnl or 0, "pnl_pct": pnl_pct, "source": "paper"}
        )

    decayed: list[dict[str, Any]] = []
    healthy: list[int] = []

    for pat in live_patterns:
        evidence = evidence_by_sp.get(pat.id, [])
        if len(evidence) < MIN_TRADES_FOR_DECAY_CHECK:
            continue

        live_wins = sum(1 for e in evidence if e["pnl"] > 0)
        live_wr = live_wins / len(evidence)
        # Use percent returns for decay comparison (dollar PnL varies with position size)
        live_avg_ret_pct = sum(e["pnl_pct"] for e in evidence) / len(evidence)
        live_avg_ret_dollar = sum(e["pnl"] for e in evidence) / len(evidence)

        oos_wr = pat.oos_win_rate or pat.win_rate or 0.50

        is_decayed = False
        reason_parts = []

        if live_wr < oos_wr - wr_gap:
            is_decayed = True
            src_counts = {"live": 0, "paper": 0}
            for e in evidence:
                src_counts[e["source"]] = src_counts.get(e["source"], 0) + 1
            reason_parts.append(
                f"WR decay: live {live_wr*100:.1f}% vs OOS {oos_wr*100:.1f}% "
                f"({src_counts['live']} real + {src_counts['paper']} paper trades)"
            )

        # Compare using percent returns (return_floor is in percent, e.g. -1.0 = -1%)
        if live_avg_ret_pct < return_floor:
            is_decayed = True
            reason_parts.append(
                f"Avg return {live_avg_ret_pct:.2f}% < floor {return_floor}%"
            )

        if is_decayed:
            reason = "; ".join(reason_parts)
            decayed.append({
                "pattern_id": pat.id,
                "pattern_name": pat.name,
                "live_wr": round(live_wr, 3),
                "oos_wr": round(oos_wr, 3),
                "live_avg_return_pct": round(live_avg_ret_pct, 2),
                "live_avg_return_dollar": round(live_avg_ret_dollar, 2),
                "trades": len(evidence),
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
        "[alpha_decay] Checked %d patterns: %d healthy, %d decayed (regime-adjusted=%s)",
        len(live_patterns), len(healthy), len(decayed), regime_adaptive,
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
    Includes both live trades and paper trades for a complete picture.
    Returns None if insufficient data.
    """
    trade_q = (
        db.query(Trade)
        .filter(
            Trade.scan_pattern_id == pattern_id,
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
        )
    )
    if user_id:
        trade_q = trade_q.filter(Trade.user_id == user_id)
    live_trades = trade_q.order_by(Trade.exit_date.asc()).all()

    paper_q = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.scan_pattern_id == pattern_id,
            PaperTrade.status == "closed",
            PaperTrade.exit_date.isnot(None),
        )
    )
    if user_id:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    paper_trades = paper_q.order_by(PaperTrade.exit_date.asc()).all()

    # Merge and sort by exit date
    all_evidence = []
    for t in live_trades:
        all_evidence.append({"exit_date": t.exit_date, "pnl": t.pnl or 0})
    for pt in paper_trades:
        all_evidence.append({"exit_date": pt.exit_date, "pnl": pt.pnl or 0})
    all_evidence.sort(key=lambda x: x["exit_date"])
    trades = all_evidence

    if len(trades) < 10:
        return None

    window = 5
    wr_points: list[tuple[float, float]] = []
    first_date = trades[0]["exit_date"]

    for i in range(window, len(trades)):
        chunk = trades[i - window:i]
        wins = sum(1 for t in chunk if (t["pnl"] or 0) > 0)
        wr = wins / window
        days_elapsed = (chunk[-1]["exit_date"] - first_date).total_seconds() / 86400
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
