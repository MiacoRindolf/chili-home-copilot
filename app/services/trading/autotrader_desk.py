"""Autopilot desk overrides for AutoTrader v1 (pause / live orders) via ``trading_brain_runtime_modes``.

Env flags remain the server-wide master; the desk row refines behavior when
``CHILI_AUTOTRADER_ENABLED`` is true:

* ``mode``: ``active`` (run) vs ``paused`` — pauses new entries only; the
  monitor loop still manages stop/target exits for open v1 positions.
* ``payload_json.live_orders``: when set, overrides ``chili_autotrader_live_enabled``
  for the orchestrator's paper vs Robinhood branch.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import BrainRuntimeMode, PaperTrade, ScanPattern, Trade
from .autopilot_scope import (
    classify_live_autopilot_trade_scope,
    live_autopilot_trade_filter,
)

logger = logging.getLogger(__name__)

AUTOTRADER_DESK_SLICE = "autotrader_v1_desk"


def _get_desk_row(db: Session) -> BrainRuntimeMode | None:
    return (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == AUTOTRADER_DESK_SLICE)
        .first()
    )


def effective_autotrader_runtime(db: Session) -> dict[str, Any]:
    """Return flags merged from env + desk row for tick / UI."""
    row = _get_desk_row(db)
    pj = dict(row.payload_json or {}) if row else {}
    paused = bool(row and (row.mode or "").lower() == "paused")
    live_env = bool(getattr(settings, "chili_autotrader_live_enabled", False))
    if "live_orders" in pj:
        live_effective = bool(pj["live_orders"])
    else:
        live_effective = live_env
    return {
        "paused": paused,
        "live_orders_effective": live_effective,
        "live_orders_env": live_env,
        "desk_live_override": "live_orders" in pj,
        "tick_allowed": bool(getattr(settings, "chili_autotrader_enabled", False)) and not paused,
        "monitor_entries_allowed": bool(getattr(settings, "chili_autotrader_enabled", False)),
        "payload": pj,
    }


def set_desk_paused(db: Session, paused: bool, *, updated_by: str = "autopilot_ui") -> None:
    row = _get_desk_row(db)
    mode = "paused" if paused else "active"
    if row is None:
        row = BrainRuntimeMode(
            slice_name=AUTOTRADER_DESK_SLICE,
            mode=mode,
            updated_by=updated_by,
            reason="autotrader_desk_pause" if paused else "autotrader_desk_resume",
            payload_json={},
        )
        db.add(row)
    else:
        row.mode = mode
        row.updated_by = updated_by
        row.reason = "autotrader_desk_pause" if paused else "autotrader_desk_resume"
    db.commit()
    logger.info("[autotrader_desk] mode=%s by=%s", mode, updated_by)


def set_desk_live_orders(db: Session, live_orders: bool | None, *, updated_by: str = "autopilot_ui") -> None:
    """Persist live_orders override; pass ``None`` to clear override (use env)."""
    row = _get_desk_row(db)
    if row is None:
        if live_orders is None:
            return
        pj = {"live_orders": bool(live_orders)}
        row = BrainRuntimeMode(
            slice_name=AUTOTRADER_DESK_SLICE,
            mode="active",
            updated_by=updated_by,
            reason="autotrader_desk_live",
            payload_json=pj,
        )
        db.add(row)
    else:
        pj = dict(row.payload_json or {})
        if live_orders is None:
            pj.pop("live_orders", None)
        else:
            pj["live_orders"] = bool(live_orders)
        row.payload_json = pj
        row.updated_by = updated_by
        row.reason = "autotrader_desk_live"
    db.commit()
    logger.info("[autotrader_desk] live_orders=%s by=%s", live_orders, updated_by)


def _rh_quote_prices_for_trades(trades: list[Trade]) -> dict[str, float]:
    """Batched Robinhood quote lookup for live stock tickers. Safe if adapter off."""
    if not trades:
        return {}
    try:
        from .venue.robinhood_spot import RobinhoodSpotAdapter

        adapter = RobinhoodSpotAdapter()
        if not adapter.is_enabled():
            return {}
        tickers = sorted({(t.ticker or "").upper() for t in trades if t.ticker})
        if not tickers:
            return {}
        return adapter.get_quote_prices_batch(tickers)
    except Exception:
        logger.debug("[autotrader_desk] RH batched quote failed", exc_info=True)
        return {}


def _fallback_quote(ticker: str) -> float | None:
    try:
        from .market_data import fetch_quote

        q = fetch_quote(ticker) or {}
        p = q.get("price") or q.get("last_price")
        return float(p) if p is not None else None
    except Exception:
        return None


def _compute_unrealized(
    *,
    entry_price: float | None,
    current_price: float | None,
    quantity: float | None,
    direction: str | None,
) -> tuple[float | None, float | None]:
    """(pnl_usd, pnl_pct) — long/short aware. Returns (None, None) when unknown."""
    try:
        if entry_price is None or current_price is None or not quantity:
            return (None, None)
        entry = float(entry_price)
        curr = float(current_price)
        qty = float(quantity)
        if entry <= 0 or qty <= 0:
            return (None, None)
        side = (direction or "long").lower()
        per_unit = (curr - entry) if side != "short" else (entry - curr)
        pnl_usd = per_unit * qty
        pnl_pct = (per_unit / entry) * 100.0
        return (round(pnl_usd, 4), round(pnl_pct, 4))
    except (TypeError, ValueError):
        return (None, None)


def list_pattern_linked_open_positions(db: Session, user_id: int) -> dict[str, Any]:
    """Open trades and paper rows surfaced on the Autopilot desk.

    Each row is enriched with:

    * ``overrides: {monitor_paused, synergy_excluded}`` — per-position desk
      controls (AutoTrader v1 only).
    * ``opened_today_et`` — PDT soft-warn badge.
    * ``current_price`` / ``unrealized_pnl_usd`` / ``unrealized_pnl_pct`` — live
      metrics for the simulation-style card.
    * ``quote_source`` — ``"robinhood"`` for live stock rows (RH's own feed,
      same venue as fills), ``"market_data"`` fallback, ``"unavailable"`` when
      no quote could be resolved.
    """
    from .auto_trader_position_overrides import (
        _opened_today_et,
        list_position_overrides,
    )

    trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "open",
            live_autopilot_trade_filter(),
        )
        .order_by(Trade.id.desc())
        .all()
    )

    papers = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == user_id,
            PaperTrade.status == "open",
            PaperTrade.scan_pattern_id.isnot(None),
        )
        .order_by(PaperTrade.id.desc())
        .all()
    )

    # D1 (no-adopt model): every pattern-linked open row is auto-managed by the
    # monitor; Pause / Exclude / Close are per-position opt-outs. Include every
    # listed row when fetching overrides.
    override_pairs: list[tuple[str, int]] = []
    override_pairs.extend(("trade", int(t.id)) for t in trades)
    override_pairs.extend(("paper", int(pt.id)) for pt in papers)
    overrides_map = list_position_overrides(db, override_pairs)

    # Batched Robinhood quote for all live stock rows (one round-trip).
    rh_prices = _rh_quote_prices_for_trades(trades)

    out_trades: list[dict[str, Any]] = []
    for t in trades:
        monitor_scope = classify_live_autopilot_trade_scope(t)
        pat_name = None
        if t.scan_pattern_id:
            p = db.get(ScanPattern, int(t.scan_pattern_id))
            if p:
                pat_name = p.name
        is_atv1 = (t.auto_trader_version or "") == "v1"
        ov = overrides_map.get(("trade", int(t.id)))
        opened_today = bool(t.entry_date and _opened_today_et(t.entry_date))
        tkr_u = (t.ticker or "").upper()
        current_price = rh_prices.get(tkr_u)
        quote_source = "robinhood" if current_price is not None else None
        if current_price is None:
            current_price = _fallback_quote(t.ticker)
            quote_source = "market_data" if current_price is not None else "unavailable"
        pnl_usd, pnl_pct = _compute_unrealized(
            entry_price=float(t.entry_price),
            current_price=current_price,
            quantity=float(t.quantity or 0),
            direction=t.direction,
        )
        out_trades.append(
            {
                "kind": "trade",
                "id": t.id,
                "ticker": t.ticker,
                "direction": t.direction,
                "entry_price": float(t.entry_price),
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                "quantity": float(t.quantity or 0),
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "scan_pattern_id": t.scan_pattern_id,
                "pattern_name": pat_name,
                "monitor_scope": monitor_scope,
                "related_alert_id": t.related_alert_id,
                "broker_source": t.broker_source,
                "asset_type": "stock",
                "auto_trader_v1": is_atv1,
                "scale_in_count": int(t.scale_in_count or 0),
                "tags": t.tags,
                "overrides": ov or {"monitor_paused": False, "synergy_excluded": False},
                "opened_today_et": opened_today,
                "controls_supported": True,
                "close_supported": True,
                "current_price": float(current_price) if current_price is not None else None,
                "unrealized_pnl_usd": pnl_usd,
                "unrealized_pnl_pct": pnl_pct,
                "quote_source": quote_source,
            }
        )

    out_paper: list[dict[str, Any]] = []
    for pt in papers:
        pat_name = None
        if pt.scan_pattern_id:
            p = db.get(ScanPattern, int(pt.scan_pattern_id))
            if p:
                pat_name = p.name
        sj = pt.signal_json or {}
        is_atv1 = bool(sj.get("auto_trader_v1"))
        ov = overrides_map.get(("paper", int(pt.id)))
        opened_today = bool(pt.entry_date and _opened_today_et(pt.entry_date))
        # Paper: simulation — generic provider is the right source of truth.
        current_price = _fallback_quote(pt.ticker)
        quote_source = "market_data" if current_price is not None else "unavailable"
        pnl_usd, pnl_pct = _compute_unrealized(
            entry_price=float(pt.entry_price),
            current_price=current_price,
            quantity=float(pt.quantity or 0),
            direction=pt.direction,
        )
        out_paper.append(
            {
                "kind": "paper",
                "id": pt.id,
                "ticker": pt.ticker,
                "direction": pt.direction,
                "entry_price": float(pt.entry_price),
                "entry_date": pt.entry_date.isoformat() if pt.entry_date else None,
                "quantity": int(pt.quantity or 0),
                "stop_price": float(pt.stop_price) if pt.stop_price is not None else None,
                "target_price": float(pt.target_price) if pt.target_price is not None else None,
                "scan_pattern_id": pt.scan_pattern_id,
                "pattern_name": pat_name,
                "auto_trader_v1": is_atv1,
                "overrides": ov or {"monitor_paused": False, "synergy_excluded": False},
                "opened_today_et": opened_today,
                "controls_supported": True,
                "close_supported": True,
                "current_price": float(current_price) if current_price is not None else None,
                "unrealized_pnl_usd": pnl_usd,
                "unrealized_pnl_pct": pnl_pct,
                "quote_source": quote_source,
            }
        )

    return {"trades": out_trades, "paper_trades": out_paper}
