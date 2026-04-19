"""Portfolio: watchlist CRUD, trade CRUD, P&L analytics, portfolio summary."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ...models.trading import (
    BreakoutAlert, JournalEntry, PatternMonitorDecision, ScanPattern,
    Trade, TradingInsight, WatchlistItem,
)
from .management_scope import MANAGEMENT_SCOPE_MANUAL
from .market_data import fetch_quote, get_indicator_snapshot, is_crypto

logger = logging.getLogger(__name__)


# ── Watchlist CRUD ────────────────────────────────────────────────────

def get_watchlist(db: Session, user_id: int | None) -> list[WatchlistItem]:
    return db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id
    ).order_by(WatchlistItem.added_at.desc()).all()


def get_effective_watchlist(db: Session, user_id: int | None) -> list[dict]:
    """Unified watchlist: manual adds + open broker/exchange positions (deduplicated)."""
    manual_items = get_watchlist(db, user_id)
    seen: set[str] = set()
    result: list[dict] = []

    for w in manual_items:
        tk = w.ticker.upper()
        if tk in seen:
            continue
        seen.add(tk)
        result.append({
            "id": w.id,
            "ticker": tk,
            "added_at": w.added_at.isoformat(),
            "source": "manual",
        })

    broker_positions = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "open",
            Trade.broker_source.isnot(None),
            Trade.broker_source != "manual",
        )
        .order_by(Trade.entry_date.desc())
        .all()
    )
    for t in broker_positions:
        tk = t.ticker.upper()
        if tk in seen:
            continue
        seen.add(tk)
        result.append({
            "id": -t.id,
            "ticker": tk,
            "added_at": t.entry_date.isoformat(),
            "source": t.broker_source,
        })

    return result


def add_to_watchlist(db: Session, user_id: int | None, ticker: str) -> WatchlistItem:
    existing = db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id,
        WatchlistItem.ticker == ticker.upper(),
    ).first()
    if existing:
        return existing
    item = WatchlistItem(user_id=user_id, ticker=ticker.upper())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def remove_from_watchlist(db: Session, user_id: int | None, ticker: str) -> bool:
    item = db.query(WatchlistItem).filter(
        WatchlistItem.user_id == user_id,
        WatchlistItem.ticker == ticker.upper(),
    ).first()
    if not item:
        return False
    db.delete(item)
    db.commit()
    return True


# ── Trade CRUD ────────────────────────────────────────────────────────

def create_trade(db: Session, user_id: int | None, **kwargs) -> Trade:
    # Risk gate: check portfolio limits before creating any trade
    _ticker = kwargs.get("ticker", "")
    try:
        from .portfolio_risk import check_new_trade_allowed
        _allowed, _reason = check_new_trade_allowed(db, user_id, _ticker)
        if not _allowed:
            raise ValueError(f"Trade blocked by risk management: {_reason}")
    except ImportError:
        pass
    except ValueError:
        raise
    except Exception as e:
        logger.warning("[portfolio] risk check error (allowing trade): %s", e)

    kwargs.setdefault("management_scope", MANAGEMENT_SCOPE_MANUAL)
    trade = Trade(user_id=user_id, **kwargs)
    if trade.entry_date is None:
        trade.entry_date = datetime.utcnow()
    if not trade.indicator_snapshot and trade.ticker:
        try:
            from .scanner import _score_ticker
            result = _score_ticker(trade.ticker)
            if result and result.get("stop_loss") and result.get("take_profit"):
                trade.indicator_snapshot = json.dumps({
                    "stop_loss": result["stop_loss"],
                    "take_profit": result["take_profit"],
                    "score": result.get("score"),
                    "signal": result.get("signal"),
                })
        except Exception:
            pass
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def close_trade(
    db: Session, trade_id: int, user_id: int | None,
    exit_price: float, exit_date: datetime | None = None, notes: str | None = None,
    reference_exit_price: float | None = None,
) -> Trade | None:
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == user_id,
    ).first()
    if not trade or trade.status != "open":
        return None

    trade.exit_price = exit_price
    trade.exit_date = exit_date or datetime.utcnow()
    trade.status = "closed"
    trade.pnl = _calc_pnl(trade)
    if notes:
        trade.notes = (trade.notes or "") + f"\n{notes}"

    try:
        from .tca_service import apply_tca_on_trade_close, resolve_exit_reference_price

        trade.tca_reference_exit_price = resolve_exit_reference_price(
            trade.ticker,
            explicit=reference_exit_price,
            fill_fallback=float(exit_price),
        )
        apply_tca_on_trade_close(trade)
    except Exception:
        pass

    try:
        snap = get_indicator_snapshot(trade.ticker)
        trade.indicator_snapshot = json.dumps(snap)
    except Exception:
        pass

    try:
        from .brain_work.execution_hooks import on_live_trade_closed

        on_live_trade_closed(db, trade, source="portfolio_close")
    except Exception:
        pass

    db.commit()
    db.refresh(trade)
    return trade


def get_trades(
    db: Session, user_id: int | None,
    status: str | None = None, limit: int = 50,
) -> list[Trade]:
    q = db.query(Trade).filter(Trade.user_id == user_id)
    if status:
        q = q.filter(Trade.status == status)
    return q.order_by(Trade.entry_date.desc()).limit(limit).all()


def delete_trade(db: Session, trade_id: int, user_id: int | None) -> str | None:
    """Delete a trade and clear journal references. Returns None on success, or error code: 'not_found' / 'forbidden'."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        return "not_found"
    if trade.user_id != user_id:
        return "forbidden"
    db.query(JournalEntry).filter(JournalEntry.trade_id == trade_id).update(
        {JournalEntry.trade_id: None}
    )
    db.delete(trade)
    db.commit()
    return None


def assign_scan_pattern_to_trade(
    db: Session,
    trade_id: int,
    user_id: int | None,
    scan_pattern_id: int | None,
) -> tuple[Trade | None, str | None]:
    """Attach or clear a ScanPattern on an open trade for pattern monitor attribution.

    When assigning, creates a synthetic BreakoutAlert so pattern_position_monitor
    can resolve trade plans (engine requires related_alert_id today).

    Returns (trade, None) on success, (None, error_code) on failure.
    """
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user_id).first()
    if not trade:
        return None, "not_found"
    if trade.status != "open":
        return None, "not_open"

    if scan_pattern_id is None:
        trade.scan_pattern_id = None
        trade.related_alert_id = None
        db.commit()
        db.refresh(trade)
        return trade, None

    pattern = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
    if not pattern:
        return None, "pattern_not_found"

    rules = pattern.rules_json
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except (json.JSONDecodeError, TypeError):
            rules = {}
    if not isinstance(rules, dict) or not rules.get("conditions"):
        return None, "pattern_invalid"

    ticker_crypto = is_crypto(trade.ticker)
    ac = (pattern.asset_class or "all").lower()
    if ac == "crypto" and not ticker_crypto:
        return None, "asset_mismatch"
    if ac == "stock" and ticker_crypto:
        return None, "asset_mismatch"

    if trade.scan_pattern_id == scan_pattern_id and trade.related_alert_id:
        db.refresh(trade)
        return trade, None

    price_at = float(trade.entry_price)
    try:
        q = fetch_quote(trade.ticker)
        if q:
            p = q.get("price") or q.get("last")
            if p:
                price_at = float(p)
    except Exception:
        logger.debug("[portfolio] assign_pattern quote failed for %s", trade.ticker, exc_info=True)

    asset_type = "crypto" if ticker_crypto else "stock"
    score = float(pattern.confidence) if pattern.confidence is not None else 0.5

    alert = BreakoutAlert(
        ticker=trade.ticker.upper(),
        asset_type=asset_type,
        alert_tier="user_assigned",
        score_at_alert=score,
        price_at_alert=price_at,
        entry_price=float(trade.entry_price),
        stop_loss=float(trade.stop_loss) if trade.stop_loss is not None else None,
        target_price=float(trade.take_profit) if trade.take_profit is not None else None,
        outcome="pending",
        user_id=user_id,
        scan_pattern_id=pattern.id,
        timeframe=pattern.timeframe or "1d",
        outcome_notes="Synthetic alert: user assigned scan pattern to open position",
    )
    db.add(alert)
    db.flush()
    trade.scan_pattern_id = pattern.id
    trade.related_alert_id = alert.id
    db.commit()
    db.refresh(trade)
    return trade, None


def _trade_ticker_key(ticker: str | None) -> str:
    raw = (ticker or "").strip().upper()
    if raw.startswith("$"):
        raw = raw[1:]
    return raw


def get_recent_pattern_imminent_alerts_for_user(
    db: Session,
    user_id: int | None,
    ticker: str,
    *,
    limit: int = 2,
) -> list[BreakoutAlert]:
    """User-scoped pattern_imminent rows for a symbol (Analyze reminder + attach UI)."""
    if user_id is None:
        return []
    tu = _trade_ticker_key(ticker)
    if not tu:
        return []
    return (
        db.query(BreakoutAlert)
        .filter(
            BreakoutAlert.ticker == tu,
            BreakoutAlert.alert_tier == "pattern_imminent",
            or_(BreakoutAlert.user_id == user_id, BreakoutAlert.user_id.is_(None)),
        )
        .order_by(BreakoutAlert.alerted_at.desc())
        .limit(limit)
        .all()
    )


def attach_breakout_alert_to_open_trade(
    db: Session,
    alert_id: int,
    user_id: int | None,
) -> tuple[Trade | None, str | None]:
    """Link an existing pattern-imminent BreakoutAlert to the user's open trade on that ticker.

    Use when broker auto-link skipped the trade (non-Robinhood source) or the user opened
    the position after the alert. Enables Monitor pattern health for that position.
    """
    if user_id is None:
        return None, "forbidden"

    alert = db.get(BreakoutAlert, alert_id)
    if not alert:
        return None, "alert_not_found"
    if alert.user_id is not None and alert.user_id != user_id:
        return None, "forbidden"

    alert_tk = _trade_ticker_key(alert.ticker)
    candidates = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .order_by(Trade.entry_date.desc())
        .all()
    )
    trade = None
    for tr in candidates:
        if _trade_ticker_key(tr.ticker) == alert_tk:
            trade = tr
            break
    if not trade:
        return None, "no_open_trade"

    trade.related_alert_id = alert.id
    if alert.scan_pattern_id and not trade.scan_pattern_id:
        trade.scan_pattern_id = alert.scan_pattern_id
    if trade.stop_loss is None and alert.stop_loss is not None:
        trade.stop_loss = float(alert.stop_loss)
    if trade.take_profit is None and alert.target_price is not None:
        trade.take_profit = float(alert.target_price)

    db.commit()
    db.refresh(trade)
    return trade, None


_VERDICT_TO_MONITOR_ACTION = {
    "hold": "hold",
    "buy": "hold",
    "add": "hold",
    "sell": "exit_now",
    "exit": "exit_now",
    "trim": "tighten_stop",
}


def apply_trade_plan_levels(
    db: Session,
    trade_id: int,
    user_id: int | None,
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    take_profit_trim: float | None = None,
    note: str | None = None,
    verdict: str | None = None,
    confidence: float | None = None,
    price_at_decision: float | None = None,
) -> tuple[Trade | None, str | None]:
    """Set stop / take-profit on an open trade so Monitor and brokers reflect an AI or manual plan.

    Updates linked BreakoutAlert levels when present. Optional trim target is appended to notes.
    When *verdict* is provided (from AI analyze), creates a PatternMonitorDecision so the
    Monitor tab immediately shows the decision.
    """
    if stop_loss is None and take_profit is None:
        return None, "no_levels"

    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user_id).first()
    if not trade:
        return None, "not_found"
    if trade.status != "open":
        return None, "not_open"

    old_stop = trade.stop_loss
    old_target = trade.take_profit

    if stop_loss is not None:
        trade.stop_loss = float(stop_loss)
    if take_profit is not None:
        trade.take_profit = float(take_profit)

    note_lines = []
    if take_profit_trim is not None:
        note_lines.append(f"Trim target (partial exit): ${float(take_profit_trim):.4g}")
    if note:
        note_lines.append(note.strip())
    if note_lines:
        extra = " | ".join(note_lines)
        trade.notes = (trade.notes + "\n" if trade.notes else "") + f"[plan] {extra}"

    if trade.related_alert_id:
        alert = db.get(BreakoutAlert, trade.related_alert_id)
        if alert:
            if stop_loss is not None:
                alert.stop_loss = float(stop_loss)
            if take_profit is not None:
                alert.target_price = float(take_profit)

    if verdict:
        action = _VERDICT_TO_MONITOR_ACTION.get(verdict.lower(), "hold")
        label = note or verdict
        decision = PatternMonitorDecision(
            trade_id=trade.id,
            breakout_alert_id=trade.related_alert_id,
            scan_pattern_id=None,
            health_score=float(confidence) if confidence is not None else 0.5,
            health_delta=None,
            conditions_snapshot={
                "source": "ai_analyze",
                "verdict": verdict,
                "label": label,
            },
            action=action,
            old_stop=old_stop,
            new_stop=stop_loss,
            old_target=old_target,
            new_target=take_profit,
            llm_confidence=float(confidence) if confidence is not None else None,
            llm_reasoning=label,
            mechanical_action=None,
            mechanical_stop=None,
            mechanical_target=None,
            decision_source="ai_analyze",
            price_at_decision=float(price_at_decision) if price_at_decision else None,
        )
        db.add(decision)

    db.commit()
    db.refresh(trade)
    return trade, None


def _calc_pnl(trade: Trade) -> float:
    if trade.exit_price is None:
        return 0.0
    diff = trade.exit_price - trade.entry_price
    if trade.direction == "short":
        diff = -diff
    return round(diff * trade.quantity, 2)


# ── P&L Analytics ─────────────────────────────────────────────────────

def _compute_stats_from_trades(closed: list[Trade]) -> dict[str, Any]:
    """Compute aggregate stats from a list of closed trades."""
    if not closed:
        return {"total_trades": 0}
    pnls = [t.pnl or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    total_pnl = sum(pnls)
    cumulative = []
    running = 0.0
    for p in pnls:
        running += p
        cumulative.append(round(running, 2))
    max_dd = 0.0
    peak = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(pnls) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2) if closed else 0,
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": cumulative,
    }


def get_trade_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate performance stats from closed trades."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).all()
    return _compute_stats_from_trades(closed)


def get_trade_stats_by_source(db: Session, user_id: int | None) -> dict[str, Any]:
    """Stats split by broker source: all, real (Robinhood), paper (manual/null)."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).all()
    real = [t for t in closed if t.broker_source == "robinhood"]
    paper = [t for t in closed if t.broker_source != "robinhood"]
    return {
        "all": _compute_stats_from_trades(closed),
        "real": _compute_stats_from_trades(real),
        "paper": _compute_stats_from_trades(paper),
    }


def get_daily_pnl(
    db: Session, user_id: int | None,
    start_date: datetime, end_date: datetime,
    *,
    include_day_trades: bool = True,
) -> list[dict[str, Any]]:
    """Daily P&L and trade count for calendar. Groups by exit_date (UTC date).

    *include_day_trades*: when False, omit per-day ``trades`` lists (smaller JSON for Brain
    performance widget, which only charts date + pnl).
    """
    from collections import defaultdict

    closed = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date.isnot(None),
        Trade.exit_date >= start_date,
        Trade.exit_date < end_date,
    ).order_by(Trade.exit_date.asc()).all()

    by_date: dict[str, list[Trade]] = defaultdict(list)
    for t in closed:
        day = t.exit_date.date().isoformat() if t.exit_date else None
        if day:
            by_date[day].append(t)

    result = []
    for day in sorted(by_date.keys()):
        trades = by_date[day]
        pnl = sum(t.pnl or 0.0 for t in trades)
        row: dict[str, Any] = {
            "date": day,
            "trade_count": len(trades),
            "pnl": round(pnl, 2),
        }
        if include_day_trades:
            row["trades"] = [
                {"id": t.id, "ticker": t.ticker, "direction": t.direction, "pnl": round(t.pnl or 0, 2)}
                for t in trades
            ]
        result.append(row)
    return result


def _compute_live_metrics_from_daily(daily: list[dict[str, Any]]) -> dict[str, Any]:
    if not daily:
        return {"sharpe_annualized": None, "max_drawdown_pct": 0.0}
    returns = [float(row.get("pnl", 0.0)) for row in daily]
    mean_ret = sum(returns) / len(returns)
    var_ret = (
        sum((r - mean_ret) ** 2 for r in returns) / max(len(returns) - 1, 1)
        if len(returns) > 1
        else 0.0
    )
    std_ret = math.sqrt(var_ret) if var_ret > 0 else 0.0
    sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else None

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        if peak > 0:
            dd = (equity - peak) / peak * 100
            max_dd = min(max_dd, dd)
    return {
        "sharpe_annualized": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd, 2),
    }


def _upsert_performance_daily(db: Session, user_id: int | None, as_of: datetime) -> None:
    """Persist one row in trading_brain_performance_daily for the UTC calendar date."""
    from ...models.trading import ScanPattern

    day_start = datetime(as_of.year, as_of.month, as_of.day)
    day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    day_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= day_start,
            Trade.exit_date <= day_end,
        )
        .all()
    )
    pnls = [float(t.pnl or 0.0) for t in day_trades]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    trade_count = len(day_trades)
    win_rate = (wins / trade_count * 100.0) if trade_count else None
    avg_pnl = (total_pnl / trade_count) if trade_count else None
    max_win = max(pnls) if pnls else None
    max_loss = min(pnls) if pnls else None

    base_patterns = db.query(ScanPattern).filter(ScanPattern.user_id == user_id)
    patterns_active = base_patterns.filter(ScanPattern.active.is_(True)).count()
    patterns_promoted = base_patterns.filter(ScanPattern.promotion_status == "promoted").count()

    db.execute(
        text(
            """
            INSERT INTO trading_brain_performance_daily (
                user_id, perf_date, total_pnl, trade_count, win_count, loss_count,
                win_rate, avg_pnl, max_win, max_loss, patterns_active, patterns_promoted, signals_generated
            ) VALUES (
                :user_id, :perf_date, :total_pnl, :trade_count, :win_count, :loss_count,
                :win_rate, :avg_pnl, :max_win, :max_loss, :patterns_active, :patterns_promoted, :signals_generated
            )
            ON CONFLICT (user_id, perf_date)
            DO UPDATE SET
                total_pnl = EXCLUDED.total_pnl,
                trade_count = EXCLUDED.trade_count,
                win_count = EXCLUDED.win_count,
                loss_count = EXCLUDED.loss_count,
                win_rate = EXCLUDED.win_rate,
                avg_pnl = EXCLUDED.avg_pnl,
                max_win = EXCLUDED.max_win,
                max_loss = EXCLUDED.max_loss,
                patterns_active = EXCLUDED.patterns_active,
                patterns_promoted = EXCLUDED.patterns_promoted,
                signals_generated = EXCLUDED.signals_generated
            """
        ),
        {
            "user_id": user_id,
            "perf_date": day_start.date(),
            "total_pnl": round(total_pnl, 2),
            "trade_count": trade_count,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "avg_pnl": round(avg_pnl, 3) if avg_pnl is not None else None,
            "max_win": round(max_win, 3) if max_win is not None else None,
            "max_loss": round(max_loss, 3) if max_loss is not None else None,
            "patterns_active": patterns_active,
            "patterns_promoted": patterns_promoted,
            "signals_generated": 0,
        },
    )
    db.commit()


def get_performance_dashboard(
    db: Session, user_id: int | None,
) -> dict[str, Any]:
    """Comprehensive performance dashboard data for the Brain UI."""
    from ...models.trading import ScanPattern
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    try:
        _upsert_performance_daily(db, user_id, now)
    except Exception:
        logger.debug("[portfolio] failed to upsert trading_brain_performance_daily", exc_info=True)

    stats = get_trade_stats(db, user_id)
    by_source = get_trade_stats_by_source(db, user_id)

    # 30-day daily P&L (omit per-day trade rows — Brain UI only needs date + pnl for the sparkline)
    daily = get_daily_pnl(
        db, user_id, now - timedelta(days=30), now, include_day_trades=False
    )
    live_metrics = _compute_live_metrics_from_daily(daily)

    # Per-pattern attribution via scan_pattern_id
    closed_with_sp = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.scan_pattern_id.isnot(None),
    ).all()
    sp_pnl: dict[int, list[float]] = {}
    for t in closed_with_sp:
        sp_pnl.setdefault(t.scan_pattern_id, []).append(t.pnl or 0)
    sp_ids = list(sp_pnl.keys())
    sp_names = {}
    if sp_ids:
        for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
            sp_names[sp.id] = sp.name
    attribution = sorted([
        {
            "pattern_id": sp_id,
            "pattern_name": sp_names.get(sp_id, f"Pattern #{sp_id}"),
            "trades": len(pnls),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else 0,
        }
        for sp_id, pnls in sp_pnl.items()
    ], key=lambda x: x["total_pnl"], reverse=True)

    # Period comparisons
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    recent_week = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
        Trade.exit_date >= week_ago,
    ).all()
    recent_month = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
        Trade.exit_date >= month_ago,
    ).all()

    return {
        "ok": True,
        "overall": stats,
        "by_source": by_source,
        "daily_pnl": daily,
        "live_metrics": live_metrics,
        "attribution": attribution[:20],
        "week": {
            "trades": len(recent_week),
            "pnl": round(sum(t.pnl or 0 for t in recent_week), 2),
            "win_rate": round(sum(1 for t in recent_week if (t.pnl or 0) > 0) / max(1, len(recent_week)) * 100, 1),
        },
        "month": {
            "trades": len(recent_month),
            "pnl": round(sum(t.pnl or 0 for t in recent_month), 2),
            "win_rate": round(sum(1 for t in recent_month if (t.pnl or 0) > 0) / max(1, len(recent_month)) * 100, 1),
        },
    }


def get_trading_dashboard_overview(
    db: Session, user_id: int | None,
) -> dict[str, Any]:
    """Consolidated trading dashboard combining performance, risk, regime, and execution quality.

    Aggregates data from multiple services into a single payload suitable
    for a unified monitoring dashboard.
    """
    from datetime import datetime, timedelta
    from ...models.trading import ScanPattern

    overview: dict[str, Any] = {"ok": True}

    # Performance summary (reuse existing)
    try:
        overview["performance"] = get_performance_dashboard(db, user_id)
    except Exception:
        overview["performance"] = None

    # Equity curve + drawdown
    try:
        now = datetime.utcnow()
        daily = get_daily_pnl(db, user_id, now - timedelta(days=90), now, include_day_trades=False)
        cumulative = 0.0
        peak = 0.0
        equity_curve: list[dict[str, Any]] = []
        drawdown_series: list[dict[str, Any]] = []
        for day in daily:
            cumulative += day.get("pnl", 0)
            peak = max(peak, cumulative)
            dd = (cumulative - peak) / max(peak, 1.0) * 100 if peak > 0 else 0
            equity_curve.append({"date": day["date"], "equity": round(cumulative, 2)})
            drawdown_series.append({"date": day["date"], "drawdown_pct": round(dd, 2)})
        overview["equity_curve"] = equity_curve
        overview["drawdown_series"] = drawdown_series
    except Exception:
        overview["equity_curve"] = []
        overview["drawdown_series"] = []

    # Pattern lifecycle counts
    try:
        stages = db.query(
            ScanPattern.lifecycle_stage,
        ).filter(ScanPattern.active == True).all()
        stage_counts: dict[str, int] = {}
        for (stage,) in stages:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        overview["pattern_lifecycle"] = stage_counts
    except Exception:
        overview["pattern_lifecycle"] = {}

    # Execution quality
    try:
        from .execution_quality import compute_execution_stats, suggest_adaptive_spread

        overview["execution_quality"] = compute_execution_stats(db, user_id)
        overview["execution_spread_suggestion"] = suggest_adaptive_spread(db, user_id)
    except Exception:
        overview["execution_quality"] = None
        overview["execution_spread_suggestion"] = None

    # Current market regime
    try:
        from .market_data import get_market_regime
        overview["regime_current"] = get_market_regime()
    except Exception:
        overview["regime_current"] = None

    # Risk snapshot
    try:
        from .portfolio_risk import get_portfolio_risk_snapshot
        import dataclasses
        budget = get_portfolio_risk_snapshot(db, user_id)
        overview["risk_snapshot"] = dataclasses.asdict(budget)
    except Exception:
        overview["risk_snapshot"] = None

    return overview


def get_trade_stats_by_pattern(
    db: Session, user_id: int | None, min_trades: int = 1,
) -> list[dict[str, Any]]:
    """Per-pattern performance stats from closed trades that have pattern_tags."""
    closed = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.pattern_tags.isnot(None),
    ).all()

    tag_trades: dict[str, list[float]] = {}
    for t in closed:
        pnl = t.pnl or 0.0
        for tag in (t.pattern_tags or "").split(","):
            tag = tag.strip()
            if tag:
                tag_trades.setdefault(tag, []).append(pnl)

    results = []
    for pattern, pnls in tag_trades.items():
        if len(pnls) < min_trades:
            continue
        wins = sum(1 for p in pnls if p > 0)
        results.append({
            "pattern": pattern,
            "trades": len(pnls),
            "wins": wins,
            "losses": len(pnls) - wins,
            "win_rate": round(wins / len(pnls) * 100, 1),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "total_pnl": round(sum(pnls), 2),
        })

    results.sort(key=lambda x: x["trades"], reverse=True)
    return results


# ── AI Insights CRUD ──────────────────────────────────────────────────

def get_insights(db: Session, user_id: int | None, limit: int = 20) -> list[TradingInsight]:
    return db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).order_by(TradingInsight.confidence.desc()).limit(limit).all()


def _pattern_label(desc: str) -> str:
    """Extract the stable label prefix before dynamic stats (e.g. ' -> avg')."""
    idx = desc.find(" -> ")
    return (desc[:idx] if idx != -1 else desc).strip()


def _pattern_keywords(desc: str) -> set[str]:
    """Extract meaningful keywords from a pattern description for dedup matching."""
    import re
    stop = {"the", "and", "for", "with", "avg", "win", "samples", "signal", "->"}
    words = set(re.findall(r"[a-z_]+(?:\d+)?", desc.lower()))
    return words - stop


def _insight_text_with_family(description: str, family: str | None) -> str:
    if not family:
        return description
    prefix = f"[family:{family}] "
    s = (description or "").strip()
    if s.startswith("[family:"):
        return description
    return prefix + description


def preload_active_insights(db: Session, user_id: int | None) -> list["TradingInsight"]:
    """Pre-fetch active insights once so callers can reuse across multiple save_insight calls."""
    return db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).all()


def _fix_insight_sequence(db: Session) -> None:
    """Reset trading_insights_id_seq to MAX(id) — recovers from sequence drift after restores."""
    try:
        from sqlalchemy import text as _text
        db.execute(_text(
            "SELECT setval('trading_insights_id_seq', COALESCE((SELECT MAX(id) FROM trading_insights), 1))"
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def save_insight(
    db: Session, user_id: int | None,
    pattern: str, confidence: float = 0.5,
    wins: int = 0, losses: int = 0,
    scan_pattern_id: int | None = None,
    hypothesis_family: str | None = None,
    _existing_cache: list["TradingInsight"] | None = None,
) -> TradingInsight:
    from .learning import log_learning_event
    from .pattern_resolution import get_legacy_unlinked_scan_pattern_id
    from datetime import datetime

    explicit_scan_pattern_id = scan_pattern_id
    if scan_pattern_id is None:
        scan_pattern_id = get_legacy_unlinked_scan_pattern_id(db)

    pattern = _insight_text_with_family(pattern, hypothesis_family)

    new_label = _pattern_label(pattern)
    new_kw = _pattern_keywords(new_label)
    existing = _existing_cache if _existing_cache is not None else preload_active_insights(db, user_id)
    for ins in existing:
        old_label = _pattern_label(ins.pattern_description)
        old_kw = _pattern_keywords(old_label)
        if not old_kw or not new_kw:
            continue
        overlap = len(new_kw & old_kw) / max(1, len(new_kw | old_kw))
        if overlap >= 0.5:
            old_conf = ins.confidence
            ins.confidence = round(min(0.95, ins.confidence * 0.7 + confidence * 0.3), 3)
            ins.evidence_count += 1
            ins.win_count = (ins.win_count or 0) + wins
            ins.loss_count = (ins.loss_count or 0) + losses
            ins.last_seen = datetime.utcnow()
            ins.pattern_description = pattern
            if hypothesis_family:
                ins.hypothesis_family = hypothesis_family
            if explicit_scan_pattern_id is not None:
                ins.scan_pattern_id = explicit_scan_pattern_id
            db.commit()
            if abs(ins.confidence - old_conf) > 0.005:
                log_learning_event(
                    db, user_id, "update",
                    f"Pattern reinforced ({old_conf:.0%}->{ins.confidence:.0%}): {ins.pattern_description[:100]}",
                    confidence_before=old_conf,
                    confidence_after=ins.confidence,
                    related_insight_id=ins.id,
                )
            return ins

    insight = TradingInsight(
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        pattern_description=pattern,
        hypothesis_family=hypothesis_family,
        confidence=confidence,
        win_count=wins,
        loss_count=losses,
    )
    db.add(insight)
    try:
        db.commit()
    except Exception as _commit_exc:
        db.rollback()
        _fix_insight_sequence(db)
        db.add(insight)
        db.commit()
    db.refresh(insight)
    log_learning_event(
        db, user_id, "discovery",
        f"New pattern: {pattern[:120]}",
        confidence_after=confidence,
        related_insight_id=insight.id,
    )
    return insight


# ── Portfolio Summary ─────────────────────────────────────────────────

def get_portfolio_summary(db: Session, user_id: int | None) -> dict[str, Any]:
    """Full portfolio overview: open positions, equity curve, benchmark."""
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "open",
    ).all()
    closed_trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    ).order_by(Trade.exit_date.asc()).all()

    positions = []
    total_invested = 0.0
    total_current = 0.0
    allocation: dict[str, float] = {}

    for t in open_trades:
        quote = fetch_quote(t.ticker)
        current_price = quote.get("price", t.entry_price) if quote else t.entry_price
        unrealized = (current_price - t.entry_price) * t.quantity
        if t.direction == "short":
            unrealized = -unrealized
        position_value = current_price * t.quantity

        positions.append({
            "id": t.id, "ticker": t.ticker, "direction": t.direction,
            "entry_price": t.entry_price, "current_price": current_price,
            "quantity": t.quantity, "unrealized_pnl": round(unrealized, 2),
            "unrealized_pct": round(unrealized / (t.entry_price * t.quantity) * 100, 2) if t.entry_price > 0 else 0,
        })
        total_invested += t.entry_price * t.quantity
        total_current += position_value
        allocation[t.ticker] = allocation.get(t.ticker, 0) + position_value

    realized_pnl = 0.0
    equity_curve = []
    for t in closed_trades:
        realized_pnl += t.pnl or 0
        if t.exit_date:
            equity_curve.append({
                "time": int(t.exit_date.timestamp()),
                "value": round(realized_pnl, 2),
            })

    total_unrealized = round(total_current - total_invested, 2) if total_invested > 0 else 0

    alloc_pct = {}
    if total_current > 0:
        for ticker, val in allocation.items():
            alloc_pct[ticker] = round(val / total_current * 100, 1)

    stats = get_trade_stats(db, user_id)

    return {
        "positions": positions,
        "position_count": len(positions),
        "total_invested": round(total_invested, 2),
        "total_current": round(total_current, 2),
        "unrealized_pnl": total_unrealized,
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": round(realized_pnl + total_unrealized, 2),
        "allocation": alloc_pct,
        "equity_curve": equity_curve,
        "stats": stats,
    }
