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
from .return_math import (
    paper_trade_realized_pnl,
    paper_trade_return_pct,
    trade_realized_pnl,
    trade_return_pct,
)

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


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _win_rate_or_none(value: Any) -> float | None:
    out = _finite_float(value)
    if out is None or out < 0.0 or out > 1.0:
        return None
    return out


def _positive_int_or_default(value: Any, default: int) -> int:
    default_int = int(default)
    if default_int <= 0:
        default_int = 1
    out = _finite_float(value)
    if out is None or out <= 0:
        return default_int
    out_int = int(out)
    return out_int if out_int > 0 else default_int


def _probability_or_default(value: Any, default: float) -> float:
    out = _win_rate_or_none(value)
    return float(default) if out is None else out


def _finite_float_or_default(value: Any, default: float) -> float:
    out = _finite_float(value)
    return float(default) if out is None else out


def _return_evidence_record(
    *,
    pnl_pct: Any,
    pnl: Any,
    source: str,
) -> dict[str, Any] | None:
    ret = _finite_float(pnl_pct)
    if ret is None:
        return None
    return {
        "pnl": _finite_float(pnl),
        "pnl_pct": ret,
        "win": ret > 0.0,
        "source": source,
    }


def _trade_realized_pnl_with_raw_fallback(trade: Any) -> float | None:
    pnl = trade_realized_pnl(trade)
    if pnl is not None:
        return pnl
    return _finite_float(getattr(trade, "pnl", None))


def _paper_realized_pnl_with_raw_fallback(paper_trade: Any) -> float | None:
    pnl = paper_trade_realized_pnl(paper_trade)
    if pnl is not None:
        return pnl
    return _finite_float(getattr(paper_trade, "pnl", None))


def _half_life_evidence_record(
    *,
    exit_date: Any,
    return_pct: Any,
) -> dict[str, Any] | None:
    ret = _finite_float(return_pct)
    if ret is None or not isinstance(exit_date, datetime):
        return None
    try:
        exit_ts = exit_date.timestamp()
    except Exception:
        return None
    if not math.isfinite(exit_ts):
        return None
    return {"exit_ts": exit_ts, "return_pct": ret}


def _mean_known_pnl(evidence: list[dict[str, Any]]) -> float | None:
    values = [
        pnl
        for pnl in (_finite_float(e.get("pnl")) for e in evidence)
        if pnl is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _payoff_ratio_protects_from_wr_decay(pattern: Any) -> bool:
    """Return True when realized payoff evidence should block WR-only decay.

    The Tier A evaluation-function fix added payoff-ratio protection to
    learning.py demote paths, but alpha_decay still demoted skew-driven
    patterns on low win-rate alone. Keep this protection intentionally
    narrow: it only applies to WR-only decay. If recent average return is
    below the configured return floor, the pattern is actually losing and
    should still be allowed to decay.
    """
    floor = _finite_float(_settings_get("chili_pattern_demote_payoff_ratio_floor", 1.5))
    if floor is None:
        floor = 1.5
    min_n = _positive_int_or_default(
        _settings_get("chili_pattern_demote_payoff_ratio_min_n", 5),
        5,
    )
    payoff_ratio = _finite_float(getattr(pattern, "payoff_ratio", None))
    payoff_n = _finite_float(getattr(pattern, "payoff_ratio_n", None))
    if payoff_ratio is None or payoff_n is None:
        return False
    return payoff_n >= min_n and payoff_ratio >= floor


def _should_skip_decay_for_payoff(
    pattern: Any,
    *,
    wr_decay_fired: bool,
    return_decay_fired: bool,
) -> bool:
    return (
        wr_decay_fired
        and not return_decay_fired
        and _payoff_ratio_protects_from_wr_decay(pattern)
    )


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
    window_days = _positive_int_or_default(window_days, DEFAULT_ROLLING_WINDOW_DAYS)
    wr_gap = _probability_or_default(wr_gap, DECAY_WR_GAP)
    return_floor = _finite_float_or_default(return_floor, DECAY_RETURN_FLOOR_PCT)

    cutoff = datetime.utcnow() - timedelta(days=window_days)

    pattern_q = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        ScanPattern.lifecycle_stage.in_(("live", "promoted")),
    )
    if user_id is not None:
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
    if user_id is not None:
        trade_q = trade_q.filter(Trade.user_id == user_id)
    recent_trades = trade_q.all()

    paper_q = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        PaperTrade.scan_pattern_id.in_(sp_ids),
        PaperTrade.exit_date >= cutoff,
    )
    if user_id is not None:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    recent_paper = paper_q.all()

    evidence_by_sp: dict[int, list[dict]] = {}
    for t in recent_trades:
        pnl_pct = trade_return_pct(t)
        rec = _return_evidence_record(
            pnl_pct=pnl_pct,
            pnl=_trade_realized_pnl_with_raw_fallback(t),
            source="live",
        )
        if rec is None:
            continue
        evidence_by_sp.setdefault(t.scan_pattern_id, []).append(rec)
    for pt in recent_paper:
        pnl_pct = paper_trade_return_pct(pt)
        rec = _return_evidence_record(
            pnl_pct=pnl_pct,
            pnl=_paper_realized_pnl_with_raw_fallback(pt),
            source="paper",
        )
        if rec is None:
            continue
        evidence_by_sp.setdefault(pt.scan_pattern_id, []).append(rec)

    decayed: list[dict[str, Any]] = []
    healthy: list[int] = []

    for pat in live_patterns:
        evidence = evidence_by_sp.get(pat.id, [])
        if len(evidence) < MIN_TRADES_FOR_DECAY_CHECK:
            continue

        live_wins = sum(1 for e in evidence if e["win"])
        live_wr = live_wins / len(evidence)
        # Use percent returns for decay comparison (dollar PnL varies with position size)
        live_avg_ret_pct = sum(e["pnl_pct"] for e in evidence) / len(evidence)
        live_avg_ret_dollar = _mean_known_pnl(evidence)

        # FIX E-1 (2026-04-29 audit): no hardcoded 0.50 fallback. Prefer
        # OOS WR if known, else realized WR. If both are None, fall back to
        # the population realized WR (dynamic prior). If even THAT is
        # unknown, abstain from the decay check for this pattern -- never
        # synthesize a coin-flip.
        from .dynamic_priors import population_win_rate
        oos_wr = _win_rate_or_none(getattr(pat, "oos_win_rate", None))
        if oos_wr is None:
            oos_wr = _win_rate_or_none(getattr(pat, "win_rate", None))
        if oos_wr is None:
            oos_wr = _win_rate_or_none(population_win_rate(db, user_id=user_id))
        if oos_wr is None:
            # No valid benchmark anywhere -- skip this pattern's decay check.
            continue

        is_decayed = False
        reason_parts = []

        wr_decay_fired = False
        return_decay_fired = False

        if live_wr < oos_wr - wr_gap:
            wr_decay_fired = True
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
            return_decay_fired = True
            is_decayed = True
            reason_parts.append(
                f"Avg return {live_avg_ret_pct:.2f}% < floor {return_floor}%"
            )

        if (
            is_decayed
            and _should_skip_decay_for_payoff(
                pat,
                wr_decay_fired=wr_decay_fired,
                return_decay_fired=return_decay_fired,
            )
        ):
            healthy.append(pat.id)
            logger.info(
                "[alpha_decay] Protected skew pattern %s from WR-only decay "
                "(live_wr=%.3f oos_wr=%.3f avg_ret_pct=%.2f "
                "payoff_ratio=%s n=%s)",
                pat.id,
                live_wr,
                oos_wr,
                live_avg_ret_pct,
                getattr(pat, "payoff_ratio", None),
                getattr(pat, "payoff_ratio_n", None),
            )
            continue

        if is_decayed:
            reason = "; ".join(reason_parts)
            decayed.append({
                "pattern_id": pat.id,
                "pattern_name": pat.name,
                "live_wr": round(live_wr, 3),
                "oos_wr": round(oos_wr, 3),
                "live_avg_return_pct": round(live_avg_ret_pct, 2),
                "live_avg_return_dollar": (
                    round(live_avg_ret_dollar, 2)
                    if live_avg_ret_dollar is not None
                    else None
                ),
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
    if user_id is not None:
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
    if user_id is not None:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    paper_trades = paper_q.order_by(PaperTrade.exit_date.asc()).all()

    # Merge and sort by exit date
    all_evidence = []
    for t in live_trades:
        pnl_pct = trade_return_pct(t)
        rec = _half_life_evidence_record(
            exit_date=getattr(t, "exit_date", None),
            return_pct=pnl_pct,
        )
        if rec is None:
            continue
        all_evidence.append(rec)
    for pt in paper_trades:
        pnl_pct = paper_trade_return_pct(pt)
        rec = _half_life_evidence_record(
            exit_date=getattr(pt, "exit_date", None),
            return_pct=pnl_pct,
        )
        if rec is None:
            continue
        all_evidence.append(rec)
    all_evidence.sort(key=lambda x: x["exit_ts"])
    trades = all_evidence

    if len(trades) < 10:
        return None

    window = 5
    wr_points: list[tuple[float, float]] = []
    first_ts = trades[0]["exit_ts"]

    for i in range(window, len(trades)):
        chunk = trades[i - window:i]
        wins = sum(1 for t in chunk if t["return_pct"] > 0.0)
        wr = wins / window
        days_elapsed = (chunk[-1]["exit_ts"] - first_ts) / 86400
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
