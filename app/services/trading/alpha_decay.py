"""Alpha decay monitoring for live/promoted patterns.

Tracks rolling performance of patterns that are generating signals.
Auto-demotes patterns whose win-rate or return has decayed below
their historical OOS benchmarks.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, PaperTrade, ScanPattern, Trade
from .return_math import paper_trade_return_pct, trade_return_pct

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
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def _mean_known_pnl(evidence: list[dict[str, Any]]) -> float | None:
    values = [float(e["pnl"]) for e in evidence if e.get("pnl") is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _rolling_evidence_stats(evidence: list[dict[str, Any]]) -> tuple[float, float, float | None]:
    wins = 0
    ret_sum = 0.0
    pnl_sum = 0.0
    pnl_count = 0
    for e in evidence:
        if e["win"]:
            wins += 1
        ret_sum += e["pnl_pct"]
        pnl = e.get("pnl")
        if pnl is not None:
            pnl_sum += float(pnl)
            pnl_count += 1
    n = len(evidence)
    avg_pnl = (pnl_sum / pnl_count) if pnl_count else None
    return wins / n, ret_sum / n, avg_pnl


_TRADE_EVIDENCE_FIELDS = (
    "scan_pattern_id",
    "pnl",
    "entry_price",
    "quantity",
    "asset_kind",
    "tags",
    "indicator_snapshot",
    "exit_price",
    "direction",
)
_PAPER_EVIDENCE_FIELDS = (
    "scan_pattern_id",
    "pnl",
    "entry_price",
    "quantity",
    "signal_json",
    "exit_price",
    "direction",
    "pnl_pct",
)
_TRADE_HALF_LIFE_FIELDS = ("exit_date",) + _TRADE_EVIDENCE_FIELDS[1:]
_PAPER_HALF_LIFE_FIELDS = ("exit_date",) + _PAPER_EVIDENCE_FIELDS[1:]


def _row_namespace(row: Any, field_names: tuple[str, ...]) -> SimpleNamespace:
    if isinstance(row, (tuple, list)):
        return SimpleNamespace(**dict(zip(field_names, row)))
    return SimpleNamespace(**{name: getattr(row, name, None) for name in field_names})


def _trade_evidence_from_row(row: Any) -> tuple[int | None, dict[str, Any] | None]:
    trade = _row_namespace(row, _TRADE_EVIDENCE_FIELDS)
    rec = _return_evidence_record(
        pnl_pct=trade_return_pct(trade),
        pnl=trade.pnl,
        source="live",
    )
    return trade.scan_pattern_id, rec


def _paper_evidence_from_row(row: Any) -> tuple[int | None, dict[str, Any] | None]:
    paper_trade = _row_namespace(row, _PAPER_EVIDENCE_FIELDS)
    rec = _return_evidence_record(
        pnl_pct=paper_trade_return_pct(paper_trade),
        pnl=paper_trade.pnl,
        source="paper",
    )
    return paper_trade.scan_pattern_id, rec


def _trade_half_life_point_from_row(row: Any) -> dict[str, Any] | None:
    trade = _row_namespace(row, _TRADE_HALF_LIFE_FIELDS)
    pnl_pct = trade_return_pct(trade)
    if pnl_pct is None or trade.exit_date is None:
        return None
    return {"exit_date": trade.exit_date, "return_pct": pnl_pct}


def _paper_half_life_point_from_row(row: Any) -> dict[str, Any] | None:
    paper_trade = _row_namespace(row, _PAPER_HALF_LIFE_FIELDS)
    pnl_pct = paper_trade_return_pct(paper_trade)
    if pnl_pct is None or paper_trade.exit_date is None:
        return None
    return {"exit_date": paper_trade.exit_date, "return_pct": pnl_pct}


def _payoff_ratio_protects_from_wr_decay(pattern: Any) -> bool:
    """Return True when realized payoff evidence should block WR-only decay.

    The Tier A evaluation-function fix added payoff-ratio protection to
    learning.py demote paths, but alpha_decay still demoted skew-driven
    patterns on low win-rate alone. Keep this protection intentionally
    narrow: it only applies to WR-only decay. If recent average return is
    below the configured return floor, the pattern is actually losing and
    should still be allowed to decay.
    """
    floor = float(_settings_get("chili_pattern_demote_payoff_ratio_floor", 1.5))
    min_n = int(_settings_get("chili_pattern_demote_payoff_ratio_min_n", 5))
    payoff_ratio = getattr(pattern, "payoff_ratio", None)
    payoff_n = getattr(pattern, "payoff_ratio_n", None)
    if payoff_ratio is None or payoff_n is None:
        return False
    try:
        return int(payoff_n) >= min_n and float(payoff_ratio) >= floor
    except (TypeError, ValueError):
        return False


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

    trade_q = db.query(
        Trade.scan_pattern_id,
        Trade.pnl,
        Trade.entry_price,
        Trade.quantity,
        Trade.asset_kind,
        Trade.tags,
        Trade.indicator_snapshot,
        Trade.exit_price,
        Trade.direction,
    ).filter(
        Trade.status == "closed",
        Trade.scan_pattern_id.in_(sp_ids),
        Trade.exit_date >= cutoff,
    )
    if user_id:
        trade_q = trade_q.filter(Trade.user_id == user_id)
    recent_trades = trade_q.all()

    paper_q = db.query(
        PaperTrade.scan_pattern_id,
        PaperTrade.pnl,
        PaperTrade.entry_price,
        PaperTrade.quantity,
        PaperTrade.signal_json,
        PaperTrade.exit_price,
        PaperTrade.direction,
        PaperTrade.pnl_pct,
    ).filter(
        PaperTrade.status == "closed",
        PaperTrade.scan_pattern_id.in_(sp_ids),
        PaperTrade.exit_date >= cutoff,
    )
    if user_id:
        paper_q = paper_q.filter(PaperTrade.user_id == user_id)
    recent_paper = paper_q.all()

    evidence_by_sp: dict[int, list[dict]] = {}
    for t in recent_trades:
        sp_id, rec = _trade_evidence_from_row(t)
        if sp_id is None or rec is None:
            continue
        evidence_by_sp.setdefault(int(sp_id), []).append(rec)
    for pt in recent_paper:
        sp_id, rec = _paper_evidence_from_row(pt)
        if sp_id is None or rec is None:
            continue
        evidence_by_sp.setdefault(int(sp_id), []).append(rec)

    decayed: list[dict[str, Any]] = []
    healthy: list[int] = []

    for pat in live_patterns:
        evidence = evidence_by_sp.get(pat.id, [])
        if len(evidence) < MIN_TRADES_FOR_DECAY_CHECK:
            continue

        # Use percent returns for decay comparison (dollar PnL varies with position size)
        live_wr, live_avg_ret_pct, live_avg_ret_dollar = _rolling_evidence_stats(evidence)

        # FIX E-1 (2026-04-29 audit): no hardcoded 0.50 fallback. Prefer
        # OOS WR if known, else realized WR. If both are None, fall back to
        # the population realized WR (dynamic prior). If even THAT is
        # unknown, abstain from the decay check for this pattern -- never
        # synthesize a coin-flip.
        from .dynamic_priors import population_win_rate
        if pat.oos_win_rate is not None:
            oos_wr = float(pat.oos_win_rate)
        elif pat.win_rate is not None:
            oos_wr = float(pat.win_rate)
        else:
            _pop = population_win_rate(db)
            if _pop is None:
                # No data anywhere -- skip this pattern's decay check.
                continue
            oos_wr = _pop

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
        db.query(
            Trade.exit_date,
            Trade.pnl,
            Trade.entry_price,
            Trade.quantity,
            Trade.asset_kind,
            Trade.tags,
            Trade.indicator_snapshot,
            Trade.exit_price,
            Trade.direction,
        )
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
        db.query(
            PaperTrade.exit_date,
            PaperTrade.pnl,
            PaperTrade.entry_price,
            PaperTrade.quantity,
            PaperTrade.signal_json,
            PaperTrade.exit_price,
            PaperTrade.direction,
            PaperTrade.pnl_pct,
        )
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
        point = _trade_half_life_point_from_row(t)
        if point is None:
            continue
        all_evidence.append(point)
    for pt in paper_trades:
        point = _paper_half_life_point_from_row(pt)
        if point is None:
            continue
        all_evidence.append(point)
    all_evidence.sort(key=lambda x: x["exit_date"])
    trades = all_evidence

    if len(trades) < 10:
        return None

    window = 5
    wr_points: list[tuple[float, float]] = []
    first_date = trades[0]["exit_date"]

    for i in range(window, len(trades)):
        chunk = trades[i - window:i]
        wins = sum(1 for t in chunk if t["return_pct"] > 0.0)
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
