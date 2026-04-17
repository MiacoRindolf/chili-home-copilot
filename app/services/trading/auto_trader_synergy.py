"""Scale-in (synergy) decision and shared stop/target recompute for AutoTrader v1."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, Trade


@dataclass
class ScaleInPlan:
    trade: Trade
    add_notional_usd: float
    new_stop: float
    new_target: float
    new_avg_entry: float
    added_quantity: float


def find_open_autotrader_paper(
    db: Session,
    *,
    user_id: Optional[int],
    ticker: str,
) -> PaperTrade | None:
    """Open paper row tagged by AutoTrader v1 (signal_json.auto_trader_v1)."""
    q = db.query(PaperTrade).filter(
        PaperTrade.ticker == ticker.upper(),
        PaperTrade.status == "open",
    )
    if user_id is not None:
        q = q.filter(PaperTrade.user_id == user_id)
    for row in q.all():
        sj = row.signal_json or {}
        if sj.get("auto_trader_v1"):
            return row
    return None


def find_open_autotrader_trade(
    db: Session,
    *,
    user_id: Optional[int],
    ticker: str,
) -> Trade | None:
    q = db.query(Trade).filter(
        Trade.ticker == ticker.upper(),
        Trade.status == "open",
        Trade.auto_trader_version == "v1",
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    return q.order_by(Trade.id.desc()).first()


def maybe_scale_in(
    db: Session,
    *,
    user_id: Optional[int],
    ticker: str,
    new_scan_pattern_id: Optional[int],
    new_stop: Optional[float],
    new_target: Optional[float],
    current_price: float,
    settings: Any,
) -> ScaleInPlan | None:
    """If an open v1 trade exists on ticker with different pattern and scale slot free, return plan."""
    if not getattr(settings, "chili_autotrader_synergy_enabled", False):
        return None
    if new_scan_pattern_id is None:
        return None

    t = find_open_autotrader_trade(db, user_id=user_id, ticker=ticker)
    if t is None:
        return None
    if int(t.scan_pattern_id or 0) == int(new_scan_pattern_id):
        return None
    if int(t.scale_in_count or 0) >= 1:
        return None

    # Respect desk per-position override: skip scale-in when excluded.
    try:
        from .auto_trader_position_overrides import get_position_overrides

        if get_position_overrides(db, "trade", int(t.id)).get("synergy_excluded"):
            return None
    except Exception:
        pass

    base = float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 300.0))
    add = float(getattr(settings, "chili_autotrader_synergy_scale_notional_usd", 150.0))
    if add <= 0 or current_price <= 0:
        return None
    max_total = base * 2.0  # plan cap: base + scale <= 2x base notional
    existing_notional = float(t.entry_price) * float(t.quantity)
    if existing_notional + add > max_total + 1e-6:
        return None

    old_stop = float(t.stop_loss or 0)
    old_tgt = float(t.take_profit or 0)
    ns = float(new_stop) if new_stop is not None else old_stop
    nt = float(new_target) if new_target is not None else old_tgt
    # Most conservative stop (lower for long), most optimistic target (higher)
    merged_stop = min(old_stop, ns) if old_stop > 0 and ns > 0 else (ns or old_stop)
    merged_target = max(old_tgt, nt) if old_tgt > 0 and nt > 0 else (nt or old_tgt)

    q0 = float(t.quantity)
    p0 = float(t.entry_price)
    add_q = add / float(current_price)
    if add_q <= 0:
        return None
    new_qty = q0 + add_q
    new_avg = (p0 * q0 + float(current_price) * add_q) / new_qty

    return ScaleInPlan(
        trade=t,
        add_notional_usd=add,
        new_stop=float(merged_stop),
        new_target=float(merged_target),
        new_avg_entry=float(new_avg),
        added_quantity=float(add_q),
    )
