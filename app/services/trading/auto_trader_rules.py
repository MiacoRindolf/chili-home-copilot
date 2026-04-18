"""Pure rule gates for AutoTrader v1 (testable without DB side effects)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from ...models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, Trade


@dataclass
class RuleGateContext:
    """Inputs needed for rule evaluation (caller supplies quote + settings snapshot)."""

    current_price: float
    autotrader_open_count: int
    realized_loss_today_usd: float  # negative sum of closed autotrader PnL today (0 if none)


def alert_confidence_from_score(alert: BreakoutAlert) -> float:
    """Match dispatch_alert mapping: min(0.95, 0.55 + 0.5 * composite)."""
    comp = float(alert.score_at_alert or 0.0)
    return min(0.95, 0.55 + 0.5 * comp)


def projected_profit_pct(entry: Optional[float], target: Optional[float]) -> Optional[float]:
    if entry is None or target is None:
        return None
    e = float(entry)
    t = float(target)
    if e <= 0:
        return None
    return round((t - e) / e * 100.0, 4)


def passes_rule_gate(
    db: Session,
    alert: BreakoutAlert,
    *,
    settings: Any,
    ctx: RuleGateContext,
    for_new_entry: bool,
) -> Tuple[bool, str, dict[str, Any]]:
    """Return (ok, reason, snapshot_dict).

    When *for_new_entry* is True, enforces check_new_trade_allowed and max concurrent.
    When False (scale-in path), caller should enforce synergy / notional separately.
    """
    snap: dict[str, Any] = {
        "ticker": alert.ticker,
        "alert_id": alert.id,
        "for_new_entry": for_new_entry,
    }

    if getattr(settings, "chili_autotrader_rth_only", True):
        from .pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        allow_ext = bool(getattr(settings, "chili_autotrader_allow_extended_hours", False))
        session_open = (
            us_stock_extended_session_open() if allow_ext else us_stock_session_open()
        )
        if not session_open:
            return False, (
                "outside_extended_hours" if allow_ext else "outside_rth"
            ), snap

    if (alert.asset_type or "").lower() != "stock":
        return False, "not_stock", snap

    conf = alert_confidence_from_score(alert)
    snap["confidence"] = conf
    floor = float(getattr(settings, "chili_autotrader_confidence_floor", 0.7))
    if conf < floor:
        return False, "confidence_below_floor", snap

    entry = alert.entry_price
    target = alert.target_price
    ppp = projected_profit_pct(entry, target)
    snap["projected_profit_pct"] = ppp
    min_pp = float(getattr(settings, "chili_autotrader_min_projected_profit_pct", 12.0))
    if ppp is None:
        return False, "missing_entry_or_target", snap
    if ppp < min_pp:
        return False, "projected_profit_below_min", snap

    ref = float(entry) if entry is not None else float(alert.price_at_alert or 0)
    if ref <= 0:
        return False, "bad_reference_price", snap

    px = float(ctx.current_price)
    snap["current_price"] = px
    max_px = float(getattr(settings, "chili_autotrader_max_symbol_price_usd", 50.0))
    if px > max_px:
        return False, "symbol_price_above_cap", snap

    slip_pct = float(getattr(settings, "chili_autotrader_max_entry_slippage_pct", 1.0))
    slip = abs(px - ref) / ref * 100.0
    snap["entry_slippage_pct"] = round(slip, 4)
    if slip > slip_pct:
        return False, "missed_entry_slippage", snap

    # Long viability: stop below entry, target above entry
    if alert.stop_loss is not None:
        sl = float(alert.stop_loss)
        if sl >= ref or sl >= px:
            return False, "stop_not_below_entry", snap
    if target is not None and float(target) <= ref:
        return False, "target_not_above_entry", snap

    cap_loss = float(getattr(settings, "chili_autotrader_daily_loss_cap_usd", 150.0))
    snap["realized_loss_today_usd"] = ctx.realized_loss_today_usd
    if cap_loss > 0 and ctx.realized_loss_today_usd <= -cap_loss:
        return False, "daily_loss_cap_already_hit", snap

    if for_new_entry:
        max_c = int(getattr(settings, "chili_autotrader_max_concurrent", 3))
        snap["autotrader_open_count"] = ctx.autotrader_open_count
        if ctx.autotrader_open_count >= max_c:
            return False, "max_concurrent_autotrader", snap

        uid = alert.user_id
        if uid is None:
            return False, "missing_user_id_on_alert", snap

        from .portfolio_risk import check_new_trade_allowed

        cap = float(getattr(settings, "chili_autotrader_assumed_capital_usd", 25_000.0))
        ok, reason = check_new_trade_allowed(db, uid, alert.ticker.upper(), capital=cap)
        snap["portfolio_check"] = {"ok": ok, "reason": reason}
        if not ok:
            return False, f"portfolio_blocked:{reason}", snap

    return True, "ok", snap


def count_autotrader_v1_open(db: Session, user_id: Optional[int], *, paper_mode: bool = False) -> int:
    if paper_mode:
        q = db.query(PaperTrade).filter(PaperTrade.status == "open")
        if user_id is not None:
            q = q.filter(PaperTrade.user_id == user_id)
        n = 0
        for row in q.all():
            sj = row.signal_json or {}
            if sj.get("auto_trader_v1"):
                n += 1
        return n
    q = db.query(Trade).filter(
        Trade.auto_trader_version == "v1",
        Trade.status == "open",
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    return int(q.count())


def autotrader_paper_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum PaperTrade.pnl for autotrader-tagged rows closed today (US/Eastern)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    q = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        PaperTrade.exit_date.isnot(None),
        PaperTrade.exit_date >= start_utc,
        PaperTrade.exit_date < end_utc,
    )
    if user_id is not None:
        q = q.filter(PaperTrade.user_id == user_id)
    total = 0.0
    for row in q.all():
        sj = row.signal_json or {}
        if not sj.get("auto_trader_v1"):
            continue
        if row.pnl is not None:
            total += float(row.pnl)
    return total


def autotrader_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum Trade.pnl for autotrader v1 positions closed on current US/Eastern calendar day."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    q = (
        db.query(Trade)
        .filter(
            Trade.auto_trader_version == "v1",
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= start_utc,
            Trade.exit_date < end_utc,
        )
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    rows = q.all()
    total = 0.0
    for t in rows:
        if t.pnl is not None:
            total += float(t.pnl)
    return total


def breakout_alert_already_processed(db: Session, breakout_alert_id: int) -> bool:
    return (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == breakout_alert_id)
        .first()
        is not None
    )
