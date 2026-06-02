"""Autopilot desk overrides for AutoTrader v1 (pause / live orders) via ``trading_brain_runtime_modes``.

Env flags remain the server-wide master; the desk row refines behavior when
``CHILI_AUTOTRADER_ENABLED`` is true:

* ``mode``: ``active`` (run) vs ``paused`` — pauses new entries only; the
  monitor loop still manages stop/target exits for open v1 positions.
* ``payload_json.live_orders``: when set, overrides ``chili_autotrader_live_enabled``
  for the orchestrator's paper vs Robinhood branch.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import BrainRuntimeMode, PaperTrade, ScanPattern, Trade
from .autopilot_scope import (
    classify_live_autopilot_trade_scope,
    is_option_trade,
    live_autopilot_trade_filter,
)
from .broker_position_truth import filter_broker_stale_open_trades

logger = logging.getLogger(__name__)

AUTOTRADER_DESK_SLICE = "autotrader_v1_desk"
AUTOTRADER_DESK_RUNTIME_UNAVAILABLE_REASON = "desk_runtime_unavailable"
AUTOTRADER_DESK_RUNTIME_TIMEOUT_MIN_MS = 250
AUTOTRADER_DESK_RUNTIME_TIMEOUT_MAX_MS = 2_000
AUTOTRADER_DESK_RUNTIME_TIMEOUT_FRACTION = 0.10


def _get_desk_row(db: Session) -> BrainRuntimeMode | None:
    return (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == AUTOTRADER_DESK_SLICE)
        .first()
    )


def _runtime_gate_statement_timeout_ms() -> int:
    tick_interval_s = int(
        max(1, int(getattr(settings, "chili_autotrader_tick_interval_seconds", 10) or 10))
    )
    tick_budget_s = int(
        max(1, int(getattr(settings, "chili_autotrader_tick_max_seconds", tick_interval_s) or tick_interval_s))
    )
    cadence_budget_ms = int(min(tick_interval_s, tick_budget_s) * 1000)
    timeout_ms = int(cadence_budget_ms * AUTOTRADER_DESK_RUNTIME_TIMEOUT_FRACTION)
    return max(
        AUTOTRADER_DESK_RUNTIME_TIMEOUT_MIN_MS,
        min(AUTOTRADER_DESK_RUNTIME_TIMEOUT_MAX_MS, timeout_ms),
    )


def _apply_runtime_gate_statement_timeout(db: Session) -> int | None:
    try:
        bind = db.get_bind()
    except Exception:
        bind = None
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect != "postgresql":
        return None
    timeout_ms = _runtime_gate_statement_timeout_ms()
    db.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}ms'"))
    return timeout_ms


def _reset_runtime_gate_statement_timeout(db: Session, timeout_ms: int | None) -> None:
    if timeout_ms is None:
        return
    db.execute(text("SET LOCAL statement_timeout = DEFAULT"))


def _fail_closed_runtime_snapshot(
    *,
    live_env: bool,
    enabled: bool,
    timeout_ms: int | None,
    error: Exception,
) -> dict[str, Any]:
    return {
        "paused": True,
        "live_orders_effective": False,
        "live_orders_env": live_env,
        "desk_live_override": False,
        "tick_allowed": False,
        "monitor_entries_allowed": enabled,
        "payload": {},
        "runtime_gate_fail_closed": True,
        "runtime_gate_reason": AUTOTRADER_DESK_RUNTIME_UNAVAILABLE_REASON,
        "runtime_gate_timeout_ms": timeout_ms,
        "runtime_gate_error": type(error).__name__,
    }


def effective_autotrader_runtime(db: Session) -> dict[str, Any]:
    """Return flags merged from env + desk row for tick / UI."""
    live_env = bool(getattr(settings, "chili_autotrader_live_enabled", False))
    enabled = bool(getattr(settings, "chili_autotrader_enabled", False))
    timeout_ms: int | None = None
    try:
        timeout_ms = _apply_runtime_gate_statement_timeout(db)
        row = _get_desk_row(db)
        _reset_runtime_gate_statement_timeout(db, timeout_ms)
    except Exception as exc:
        logger.warning(
            "[autotrader_desk] runtime gate failed closed reason=%s error=%s timeout_ms=%s",
            AUTOTRADER_DESK_RUNTIME_UNAVAILABLE_REASON,
            type(exc).__name__,
            timeout_ms,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug("[autotrader_desk] runtime fail-closed rollback failed", exc_info=True)
        return _fail_closed_runtime_snapshot(
            live_env=live_env,
            enabled=enabled,
            timeout_ms=timeout_ms,
            error=exc,
        )
    pj = dict(row.payload_json or {}) if row else {}
    paused = bool(row and (row.mode or "").lower() == "paused")
    if "live_orders" in pj:
        live_effective = bool(pj["live_orders"])
    else:
        live_effective = live_env
    return {
        "paused": paused,
        "live_orders_effective": live_effective,
        "live_orders_env": live_env,
        "desk_live_override": "live_orders" in pj,
        "tick_allowed": enabled and not paused,
        "monitor_entries_allowed": enabled,
        "payload": pj,
        "runtime_gate_fail_closed": False,
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


def _safe_quote_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _paper_signal_json(pt: PaperTrade) -> dict[str, Any]:
    signal = getattr(pt, "signal_json", None)
    if isinstance(signal, dict):
        return dict(signal)
    if isinstance(signal, str):
        try:
            parsed = json.loads(signal)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _broker_quote_price_for_trade(trade: Trade) -> tuple[float | None, str]:
    """Broker-source quote for a live trade; never cross-feed venues."""
    broker_source = (trade.broker_source or "").strip().lower()
    if not broker_source and not is_option_trade(trade):
        return None, "unavailable"
    try:
        from .broker_quotes import broker_quote_for_trade

        quote = broker_quote_for_trade(trade, purpose="display")
        px = _safe_quote_float(quote.get("price"))
        source = str(quote.get("source") or f"{broker_source}_unavailable")
        if px is not None:
            return px, source
        return None, source
    except Exception:
        logger.debug(
            "[autotrader_desk] broker quote failed broker=%s ticker=%s",
            broker_source,
            trade.ticker,
            exc_info=True,
        )
    return None, f"{broker_source or 'broker'}_unavailable"


def _fallback_quote(ticker: str) -> float | None:
    try:
        from .market_data import fetch_quote

        q = fetch_quote(ticker) or {}
        p = q.get("price") or q.get("last_price")
        return _safe_quote_float(p)
    except Exception:
        return None


def _compute_unrealized(
    *,
    entry_price: float | None,
    current_price: float | None,
    quantity: float | None,
    direction: str | None,
    multiplier: float = 1.0,
) -> tuple[float | None, float | None]:
    """(pnl_usd, pnl_pct) — long/short aware. Returns (None, None) when unknown."""
    try:
        if any(isinstance(v, bool) for v in (entry_price, current_price, quantity, multiplier)):
            return (None, None)
        if entry_price is None or current_price is None or not quantity:
            return (None, None)
        entry = float(entry_price)
        curr = float(current_price)
        qty = float(quantity)
        mult = float(multiplier or 1.0)
        if (
            not math.isfinite(entry)
            or not math.isfinite(curr)
            or not math.isfinite(qty)
            or not math.isfinite(mult)
            or entry <= 0
            or curr <= 0
            or qty <= 0
            or mult <= 0
        ):
            return (None, None)
        side = (direction or "long").lower()
        per_unit = (curr - entry) if side != "short" else (entry - curr)
        pnl_usd = per_unit * qty * mult
        pnl_pct = (per_unit / entry) * 100.0
        return (round(pnl_usd, 4), round(pnl_pct, 4))
    except (TypeError, ValueError):
        return (None, None)


def _trade_asset_type(trade: Trade) -> str:
    if is_option_trade(trade):
        return "options"
    ticker = (trade.ticker or "").strip().upper()
    if ticker.endswith("-USD"):
        return "crypto"
    return "stock"


def list_pattern_linked_open_positions(db: Session, user_id: int) -> dict[str, Any]:
    """Open trades and paper rows surfaced on the Autopilot desk.

    Each row is enriched with:

    * ``overrides: {monitor_paused, synergy_excluded}`` — per-position desk
      controls (AutoTrader v1 only).
    * ``opened_today_et`` — PDT soft-warn badge.
    * ``current_price`` / ``unrealized_pnl_usd`` / ``unrealized_pnl_pct`` — live
      metrics for the simulation-style card.
    * ``quote_source`` - broker name for live broker-backed rows, with
      ``"<broker>_stale"`` / ``"<broker>_unavailable"`` when the broker feed
      cannot provide a fresh executable quote. Legacy/manual rows may still use
      ``"market_data"``.
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
    trades, suppressed_stale_trades = filter_broker_stale_open_trades(db, trades)

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
        trade_is_option = is_option_trade(t)
        current_price, quote_source = _broker_quote_price_for_trade(t)
        if current_price is None and not trade_is_option and not (t.broker_source or "").strip():
            current_price = _fallback_quote(t.ticker)
            quote_source = "market_data" if current_price is not None else "unavailable"
        pnl_usd, pnl_pct = _compute_unrealized(
            entry_price=float(t.entry_price),
            current_price=current_price,
            quantity=float(t.quantity or 0),
            direction=t.direction,
            multiplier=100.0 if trade_is_option else 1.0,
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
                "asset_type": _trade_asset_type(t),
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
        sj = _paper_signal_json(pt)
        is_atv1 = bool(sj.get("auto_trader_v1"))
        ov = overrides_map.get(("paper", int(pt.id)))
        opened_today = bool(pt.entry_date and _opened_today_et(pt.entry_date))
        # Paper options still live in premium space; stock/crypto paper rows use spot.
        try:
            from .paper_trading import _is_option_paper_trade, _paper_current_mark_price

            paper_is_option = _is_option_paper_trade(pt)
        except Exception:
            paper_is_option = False
        if paper_is_option:
            try:
                current_price = _safe_quote_float(
                    _paper_current_mark_price(pt, purpose="display")  # type: ignore[name-defined]
                )
            except Exception:
                current_price = None
            quote_source = (
                "robinhood_options"
                if current_price is not None
                else "option_premium_unavailable"
            )
        else:
            current_price = _fallback_quote(pt.ticker)
            quote_source = "market_data" if current_price is not None else "unavailable"
        pnl_usd, pnl_pct = _compute_unrealized(
            entry_price=float(pt.entry_price),
            current_price=current_price,
            quantity=float(pt.quantity or 0),
            direction=pt.direction,
            multiplier=100.0 if paper_is_option else 1.0,
        )
        paper_asset_type = "options" if paper_is_option else (
            "crypto" if (pt.ticker or "").strip().upper().endswith("-USD") else "stock"
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
                "asset_type": paper_asset_type,
                "contract_multiplier": 100.0 if paper_is_option else None,
                "current_price": float(current_price) if current_price is not None else None,
                "unrealized_pnl_usd": pnl_usd,
                "unrealized_pnl_pct": pnl_pct,
                "quote_source": quote_source,
            }
        )

    return {
        "trades": out_trades,
        "paper_trades": out_paper,
        "suppressed_stale_trades": suppressed_stale_trades,
    }
