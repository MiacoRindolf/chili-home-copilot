"""Daily playbook generation for the trading brain.

Produces a structured playbook each morning:
- Current market regime assessment
- Ranked trade ideas from promoted patterns
- Risk budget allocation
- Key levels and watchlist
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern
from .management_envelopes import summarize_closed_envelope_performance

logger = logging.getLogger(__name__)


def generate_daily_playbook(
    db: Session,
    user_id: int | None = None,
    capital: float = 100_000.0,
) -> dict[str, Any]:
    """Generate today's trading playbook."""
    from .market_data import get_market_regime
    from .portfolio_risk import (
        get_portfolio_risk_snapshot,
        get_risk_limits,
        is_breaker_tripped,
        get_breaker_status,
    )
    from .lifecycle import get_lifecycle_summary

    playbook: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
    }

    # 1. Regime assessment
    try:
        regime = get_market_regime()
        playbook["regime"] = {
            "composite": regime.get("regime", "unknown"),
            "spy_direction": regime.get("spy_direction", "flat"),
            "spy_momentum_5d": regime.get("spy_momentum_5d", 0),
            "vix": regime.get("vix"),
            "vix_regime": regime.get("vix_regime", "unknown"),
            "guidance": _regime_guidance(regime),
        }
    except Exception as e:
        logger.warning("[playbook] Regime fetch failed: %s", e)
        playbook["regime"] = {"composite": "unknown", "guidance": "Unable to assess regime"}

    # 2. Risk budget
    limits = get_risk_limits()
    budget = get_portfolio_risk_snapshot(db, user_id, capital, limits)
    breaker = get_breaker_status()
    playbook["risk"] = {
        "open_positions": budget.open_positions,
        "total_heat_pct": budget.total_heat_pct,
        "available_heat_pct": budget.available_heat_pct,
        "can_open_new": budget.can_open_new,
        "breaker_tripped": breaker.get("tripped", False),
        "breaker_reason": breaker.get("reason"),
        "max_new_trades_today": _max_new_trades(budget, limits),
    }

    # 3. Pattern lifecycle summary
    playbook["lifecycle"] = get_lifecycle_summary(db)

    # 4. Ranked trade ideas from promoted patterns
    playbook["ideas"] = _generate_trade_ideas(db, user_id, capital)

    # 5. Recent performance context
    playbook["recent_performance"] = _recent_performance(db, user_id, capital)

    # 6. Watchlist — patterns close to promotion
    playbook["watchlist"] = _near_promotion_watchlist(db)

    playbook["ok"] = True

    # Persist playbook to DB
    try:
        _persist_playbook(db, user_id, playbook)
    except Exception as e:
        logger.debug("[playbook] Failed to persist: %s", e)

    return playbook


def _persist_playbook(db: Session, user_id: int | None, playbook: dict[str, Any]) -> None:
    """Save the daily playbook to the database for history."""
    from sqlalchemy import text
    today = datetime.utcnow().strftime("%Y-%m-%d")
    regime = (playbook.get("regime") or {}).get("composite", "unknown")
    guidance = (playbook.get("regime") or {}).get("guidance", "")
    max_new = (playbook.get("risk") or {}).get("max_new_trades_today", 0)

    try:
        db.execute(text("""
            INSERT INTO trading_daily_playbooks (user_id, playbook_date, regime, regime_guidance, max_new_trades,
                ideas_json, watchlist_json, risk_snapshot_json, performance_json)
            VALUES (:uid, :d, :reg, :guide, :max_t, :ideas, :watch, :risk, :perf)
            ON CONFLICT (user_id, playbook_date) DO UPDATE SET
                regime = EXCLUDED.regime,
                regime_guidance = EXCLUDED.regime_guidance,
                max_new_trades = EXCLUDED.max_new_trades,
                ideas_json = EXCLUDED.ideas_json,
                watchlist_json = EXCLUDED.watchlist_json,
                risk_snapshot_json = EXCLUDED.risk_snapshot_json,
                performance_json = EXCLUDED.performance_json
        """), {
            "uid": user_id, "d": today, "reg": regime, "guide": guidance,
            "max_t": max_new,
            "ideas": json.dumps(playbook.get("ideas", [])),
            "watch": json.dumps(playbook.get("watchlist", [])),
            "risk": json.dumps(playbook.get("risk", {})),
            "perf": json.dumps(playbook.get("recent_performance", {})),
        })
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _regime_guidance(regime: dict[str, Any]) -> str:
    """Generate regime-based trading guidance."""
    composite = regime.get("regime", "unknown")
    vix = regime.get("vix") or 0
    spy_dir = regime.get("spy_direction", "flat")

    if composite == "risk_on":
        return (
            f"Risk-on environment. SPY {spy_dir}, VIX {vix:.1f}. "
            "Full allocation OK. Favor momentum and breakout setups."
        )
    elif composite == "risk_off":
        return (
            f"Risk-off environment. SPY {spy_dir}, VIX {vix:.1f}. "
            "Reduce position sizes by 50%. Favor mean-reversion and defensive setups. "
            "Avoid breakout entries in this regime."
        )
    else:
        return (
            f"Cautious environment. SPY {spy_dir}, VIX {vix:.1f}. "
            "Normal allocation. Be selective with entries, tighten stops."
        )


def _max_new_trades(budget, limits) -> int:
    """Estimate how many new trades the risk budget allows."""
    if not budget.can_open_new:
        return 0
    remaining_slots = max(0, limits.max_open_positions - budget.open_positions)
    heat_slots = int(budget.available_heat_pct / limits.max_risk_per_trade_pct) if limits.max_risk_per_trade_pct > 0 else 0
    return min(remaining_slots, heat_slots)


def _generate_trade_ideas(db: Session, user_id: int | None, capital: float) -> list[dict[str, Any]]:
    """Pull top promoted patterns and score them as trade ideas."""
    promoted = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
        .order_by(ScanPattern.confidence.desc())
        .limit(20)
        .all()
    )

    ideas = []
    for p in promoted:
        oos_wr = _safe_float(p.oos_win_rate) or _safe_float(p.win_rate)
        avg_ret = _safe_float(p.avg_return_pct)
        score = round((oos_wr / 100) * 0.6 + min(1.0, avg_ret / 5) * 0.4, 3)

        idea: dict[str, Any] = {
            "pattern_id": p.id,
            "pattern_name": p.name,
            "lifecycle_stage": p.lifecycle_stage,
            "confidence": round(_safe_float(p.confidence), 3),
            "oos_win_rate": round(oos_wr, 1),
            "avg_return_pct": round(avg_ret, 2),
            "idea_score": score,
            "timeframe": getattr(p, "timeframe", "1d") or "1d",
            "asset_class": getattr(p, "asset_class", "all") or "all",
        }

        # Parse conditions for display
        try:
            rules = json.loads(p.rules_json or "{}")
            conds = rules.get("conditions", [])
            idea["conditions_summary"] = ", ".join(
                f"{c.get('indicator','?')} {c.get('op','?')} {c.get('value', c.get('ref', '?'))}"
                for c in conds[:4]
            )
        except Exception:
            idea["conditions_summary"] = ""

        ideas.append(idea)

    ideas.sort(key=lambda x: x["idea_score"], reverse=True)
    return ideas[:10]


def _recent_performance(db: Session, user_id: int | None, capital: float) -> dict[str, Any]:
    """7-day and 30-day P&L summary."""
    now = datetime.utcnow()
    week = summarize_closed_envelope_performance(
        db,
        user_id=user_id,
        since=now - timedelta(days=7),
    )
    month = summarize_closed_envelope_performance(
        db,
        user_id=user_id,
        since=now - timedelta(days=30),
    )
    return {"week": week.to_payload(), "month": month.to_payload()}


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Return default if v is None, NaN, or inf."""
    if v is None:
        return default
    try:
        if math.isnan(v) or math.isinf(v):
            return default
    except TypeError:
        return default
    return float(v)


def _near_promotion_watchlist(db: Session) -> list[dict[str, Any]]:
    """Patterns that are backtested but not yet promoted — close to the gate."""
    candidates = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage == "backtested",
            ScanPattern.oos_win_rate.isnot(None),
        )
        .order_by(ScanPattern.oos_win_rate.desc())
        .limit(5)
        .all()
    )
    return [
        {
            "pattern_id": p.id,
            "name": p.name,
            "oos_win_rate": round(_safe_float(p.oos_win_rate), 1),
            "confidence": round(_safe_float(p.confidence), 3),
            "backtest_count": p.backtest_count or 0,
        }
        for p in candidates
    ]
